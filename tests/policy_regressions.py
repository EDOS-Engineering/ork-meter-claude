"""Stable production session policy, immediate opt-outs and honest meter flags."""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_* when this file runs on its own

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/lib"))
sys.path.insert(0, str(ROOT / "tests"))
import entitled  # noqa: E402
entitled.install()
import features
import hook
import ledger
import policy
import session


class SessionPolicy(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payload = {"session_id": "policy-" + self.root.name, "scratchpad_dir": str(self.root / "scratch")}
        self.env = {"HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"), "CLAUDE_PLUGIN_ROOT": str(ROOT),
                    "CLAUDE_PLUGIN_DATA": os.environ["CLAUDE_PLUGIN_DATA"]}

    def context(self, env=None, event="PostToolUse"):
        ctx = hook.Context(dict(self.env, **(env or {})), dict(self.payload), event, time.monotonic() + 5)
        policy.bind(ctx)
        return ctx

    def test_first_pin_keeps_budgets_across_resume_clear_compaction_and_new_defaults(self):
        first = self.context({"ORK_METER_TRIM_BUDGET_BASH": "32KiB"}, "SessionStart")
        saved = Path(first.store().policy_path).read_bytes()
        for event in ("SessionStart", "PostCompact", "PostToolUse"):
            current = self.context({"ORK_METER_TRIM_BUDGET_BASH": "64KiB"}, event)
            current.store().reset()
            self.assertEqual(features.trim_budget(current.env, "Bash"), 32 * 1024)
            self.assertEqual(current.policy_sha256, first.policy_sha256)
            self.assertEqual(Path(current.store().policy_path).read_bytes(), saved)
        tunables = features.LEVERS["trim"]["tunables"]
        changed = dict(tunables, budget_bash=tunables["budget_bash"]._replace(default=48 * 1024))
        with patch.dict(features.LEVERS["trim"], {"tunables": changed}):
            current = self.context()
            self.assertEqual(features.trim_budget(current.env, "Bash"), 32 * 1024)

    def test_new_session_gets_new_settings_without_cross_session_state(self):
        first = self.context({"ORK_METER_TRIM_BUDGET_BASH": "32k"})
        self.payload = {"session_id": "other-" + self.root.name, "scratchpad_dir": str(self.root / "other")}
        second = self.context({"ORK_METER_TRIM_BUDGET_BASH": "64k"})
        self.assertEqual(features.trim_budget(second.env, "Bash"), 64 * 1024)
        self.assertNotEqual(first.policy_sha256, second.policy_sha256)

    def test_immediate_optout_updates_exact_platform_contract_and_actual_hash(self):
        first = self.context()
        disabled = self.context({"ORK_METER_TRIM": "off"})
        self.assertFalse(features.enabled(disabled.env, "trim"))
        self.assertFalse(features.enabled(disabled.env, "dedupe"))
        self.assertEqual(first.session_policy_sha256, disabled.session_policy_sha256)
        self.assertNotEqual(first.policy_sha256, disabled.policy_sha256)
        flags = json.loads(Path(features.features_path(disabled.env, self.payload["session_id"])).read_text())
        self.assertEqual(set(flags), {"plugin_version", "features", "updated_at"})
        self.assertNotIn("trim", flags["features"])
        disabled.record(ledger.loop("Bash", 3, False))
        self.assertEqual(ledger.load(disabled.store())[-1]["policy_sha256"], disabled.policy_sha256)
        restored = self.context()
        self.assertEqual(restored.policy_sha256, first.policy_sha256)
        flags = json.loads(Path(features.features_path(restored.env, self.payload["session_id"])).read_text())
        self.assertIn("trim", flags["features"])

    def test_disabled_session_cannot_be_enabled_by_changed_environment(self):
        # Missing SessionStart cannot let a disabled first tool hook later enable.
        self.context({"ORK_METER_TRIM": "off"}, "PostToolUse")
        current = self.context({"ORK_METER_TRIM": "on"})
        self.assertFalse(features.enabled(current.env, "trim"))

    def test_mode_changes_never_start_capture_or_enable_reduction(self):
        self.context({"ORK_METER_MODE": "reduce"})
        self.assertEqual(features.mode(self.context({"ORK_METER_MODE": "shadow"}).env), "off")
        self.assertEqual(features.mode(self.context({"ORK_METER_MODE": "invalid"}).env), "off")
        self.payload = {"session_id": "shadow-" + self.root.name, "scratchpad_dir": str(self.root / "shadow")}
        self.context({"ORK_METER_MODE": "shadow"})
        self.assertEqual(features.mode(self.context({"ORK_METER_MODE": "reduce"}).env), "off")
        self.assertEqual(features.mode(self.context({"ORK_METER_MODE": "shadow"}).env), "shadow")

    def test_corrupt_wrong_version_or_identity_policy_passes_through_without_repair(self):
        first = self.context()
        path = Path(first.store().policy_path)
        original = json.loads(path.read_text())
        bad = [b'{"truncated":', json.dumps(dict(original, session_id="wrong")).encode(),
               json.dumps(dict(original, plugin_version="999.0.0")).encode(),
               json.dumps(dict(original, policy_sha256="0" * 64)).encode()]
        for raw in bad:
            with self.subTest(raw=raw[:100]):
                path.write_bytes(raw)
                current = self.context()
                self.assertEqual(features.mode(current.env), "off")
                self.assertIsNone(current.policy_sha256)
                self.assertEqual(path.read_bytes(), raw)

    def repin(self, ctx, change):
        """Rewrite the pin as another build would have written it, with a valid digest."""
        path = Path(ctx.store().policy_path)
        value = json.loads(path.read_text())
        change(value["settings"])
        value["policy_sha256"] = policy.digest(value["plugin_version"], value["settings"])
        path.write_text(json.dumps(value))
        return value["policy_sha256"]

    def fallbacks(self, ctx):
        return [row for row in ledger.load(ctx.store()) if row.get("kind") == "policy_fallback"]

    def test_pin_from_a_build_with_fewer_levers_keeps_savings_and_never_starts_a_new_lever(self):
        # #105: an update that adds a setting must not turn running sessions to pass-through.
        first = self.context({"ORK_METER_CONTEXT_HYGIENE": "on", "ORK_METER_TRIM_BUDGET_BASH": "32k"})
        def older(settings):
            for name in ("ORK_METER_CONTEXT_HYGIENE", "ORK_METER_LOOP_GUARD", "ORK_METER_CONTEXT_NUDGE_TOKENS"):
                del settings[name]
        pinned = self.repin(first, older)
        current = self.context({"ORK_METER_CONTEXT_HYGIENE": "on", "ORK_METER_LOOP_GUARD": "on"})
        self.assertEqual(features.mode(current.env), "reduce")
        self.assertTrue(features.enabled(current.env, "trim"))
        self.assertEqual(features.trim_budget(current.env, "Bash"), 32 * 1024, "pinned values are kept")
        self.assertFalse(features.enabled(current.env, "context_hygiene"), "a missing opt-in switch reads off")
        self.assertFalse(features.enabled(current.env, "loop_guard"), "a missing opt-out switch reads off too")
        self.assertEqual(features.value(current.env, "context_hygiene", "tokens"), 120000, "a missing tunable reads its default")
        self.assertEqual(current.session_policy_sha256, pinned, "the pin's identity is unchanged")
        self.assertEqual(self.fallbacks(current), [])

    def test_switch_tunable_added_later_reads_off_even_when_its_default_is_on(self):
        # Review of #114: an on/off control declared as a tunable (unit "switch") inside an
        # existing lever must not start mid-session in sessions pinned before it, even if a
        # later build makes it default-on.
        tunables = features.LEVERS["trim"]["tunables"]
        added = dict(tunables, new_switch=features.Tunable("ORK_METER_NEW_SWITCH", 1, 0, "switch"))
        with patch.dict(features.LEVERS["trim"], {"tunables": tunables}):
            first = self.context()   # pinned by a build without the switch
        # A build with the switch computes its own lever-kind table at import.
        with patch.dict(features.LEVERS["trim"], {"tunables": added}), \
                patch.object(features, "SETTING_KINDS", features._setting_kinds()):
            self.assertEqual(features.policy_settings({})["ORK_METER_NEW_SWITCH"], "1", "a fresh session defaults it on")
            current = self.context()
            self.assertEqual(features.mode(current.env), "reduce")
            self.assertEqual(features.value(current.env, "trim", "new_switch"), 0, "an older session reads it off")
            self.assertEqual(current.session_policy_sha256, first.session_policy_sha256)
            self.assertEqual(self.fallbacks(current), [])

    def test_pin_from_a_build_with_more_levers_ignores_their_settings(self):
        first = self.context()
        pinned = self.repin(first, lambda settings: settings.update(ORK_METER_FUTURE_LEVER="on"))
        current = self.context()
        self.assertEqual(features.mode(current.env), "reduce")
        self.assertTrue(features.enabled(current.env, "trim"))
        self.assertNotIn("ORK_METER_FUTURE_LEVER", features.policy_settings(current.env))
        self.assertEqual(current.session_policy_sha256, pinned)

    def test_tampered_or_modeless_pin_still_passes_through_and_says_so_once(self):
        first = self.context()
        path = Path(first.store().policy_path)
        value = json.loads(path.read_text())
        tampered = dict(value, settings=dict(value["settings"], ORK_METER_TRIM_BUDGET_BASH="99999"))
        path.write_text(json.dumps(tampered))
        self.assertEqual(features.mode(self.context().env), "off", "settings changed without a valid digest")
        self.repin(first, lambda settings: settings.pop("ORK_METER_MODE"))
        current = self.context()
        self.assertEqual(features.mode(current.env), "off", "a pin without a mode is not adapted")
        rows = self.fallbacks(current)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["reason"], "ValueError")
        self.assertEqual(rows[0]["session_id"], self.payload["session_id"])
        self.assertTrue(set(ledger.COMMON_FIELDS) <= set(rows[0]), set(ledger.COMMON_FIELDS) - set(rows[0]))

    def test_pin_missing_a_shared_switch_turns_its_whole_lever_family_off(self):
        first = self.context()
        self.repin(first, lambda settings: settings.pop("ORK_METER_TRIM"))
        current = self.context()
        self.assertEqual(features.mode(current.env), "reduce")
        for lever in ("trim", "dedupe", "read_guard", "test_filter"):
            self.assertFalse(features.enabled(current.env, lever), lever)
        self.assertTrue(features.enabled(current.env, "loop_guard"))

    def test_initialization_failure_disables_current_hook_without_response_or_rerun(self):
        output = io.StringIO()
        payload = dict(self.payload, hook_event_name="PostToolUse", tool_name="Bash")
        with patch.dict(os.environ, self.env, clear=True), patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(output), patch.object(session, "initialize_json", side_effect=OSError("disk full")):
            hook.run(("PostToolUse",), lambda _: self.fail("must preserve original tool result"), feature="trim")
        self.assertEqual(output.getvalue(), "")

    def test_command_capture_uses_existing_pin_and_corruption_passes_through(self):
        first = self.context({"ORK_METER_TRIM_BUDGET_BASH": "32k"})
        effective = policy.for_store(first.store(), dict(self.env, ORK_METER_TRIM_BUDGET_BASH="64k"))
        self.assertEqual(features.trim_budget(effective, "Bash"), 32 * 1024)
        Path(first.store().policy_path).write_text("bad json")
        self.assertEqual(features.mode(policy.for_store(first.store(), self.env)), "off")

    def test_stale_or_future_feature_timestamp_is_refreshed(self):
        ctx = self.context()
        path = Path(features.features_path(ctx.env, self.payload["session_id"]))
        for timestamp in ("2000-01-01T00:00:00Z", "2999-01-01T00:00:00Z", None):
            with self.subTest(timestamp=timestamp):
                flags = json.loads(path.read_text())
                flags["updated_at"] = timestamp
                path.write_text(json.dumps(flags))
                self.context()
                self.assertNotEqual(json.loads(path.read_text())["updated_at"], timestamp)

    def test_savings_reports_saved_policy_separately_from_observed_effective_hashes(self):
        first = self.context()
        first.record(ledger.loop("Bash", 3, False))
        disabled = self.context({"ORK_METER_TRIM": "off"})
        disabled.record(ledger.loop("Bash", 6, False))
        before = Path(first.store().policy_path).read_bytes()
        command = [sys.executable, "-I", "-B", str(ROOT / "scripts/savings.py"), "--scratchpad",
                   self.payload["scratchpad_dir"], "--no-measure", "--json"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads(result.stdout)["policy"]
        self.assertEqual(recorded["status"], "pinned")
        self.assertEqual(recorded["session_policy_sha256"], first.session_policy_sha256)
        self.assertEqual(recorded["observed_policy_sha256"], sorted({first.policy_sha256, disabled.policy_sha256}))
        self.assertEqual(Path(first.store().policy_path).read_bytes(), before)

    def test_concurrent_production_processes_use_one_atomic_winner(self):
        payload = dict(self.payload, hook_event_name="SessionStart", source="startup")
        def run(value):
            env = dict(os.environ, **self.env, ORK_METER_TRIM_BUDGET_BASH=str(value * 1024))
            started = time.monotonic()
            result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "hooks/lib/run.py"),
                                     str(ROOT / "hooks/trim-session.py")], input=json.dumps(payload),
                                    capture_output=True, text=True, env=env, timeout=8)
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 0, result.stderr)
            if not result.stdout.strip():
                # A process past hook.EMIT_LIMIT drops its finished reply (fail-open,
                # #140); an overloaded runner can get there. A fast silent one is a bug.
                self.assertGreaterEqual(elapsed, hook.EMIT_LIMIT, "empty reply after %.2fs: %r" % (elapsed, result.stderr))
                return None
            return json.loads(result.stdout)["systemMessage"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            statuses = [status for status in pool.map(run, (16, 20, 24, 28, 32, 36)) if status is not None]
        self.assertTrue(statuses, "every process exceeded the emit limit")
        self.assertEqual(len(set(statuses)), 1, statuses)
        store = session.Store.for_payload(self.payload)
        saved = policy.read(store, self.payload["session_id"], self.env)
        self.assertTrue(all("Bash %dK" % (int(saved["settings"]["ORK_METER_TRIM_BUDGET_BASH"]) // 1024) in status for status in statuses))
        self.assertEqual(list(Path(store.path).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
