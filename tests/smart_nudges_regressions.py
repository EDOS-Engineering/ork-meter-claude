"""Issue #110: the context nudge's per-token default, decided from metadata
once per session, and /ork-meter:audit's LSP and 1-hour-cache advice.
All transcripts and projects are synthetic."""
import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402
import entitled  # noqa: E402
entitled.install()
import billing  # noqa: E402
import features  # noqa: E402
import hook  # noqa: E402
import policy  # noqa: E402
import project_env  # noqa: E402

HYGIENE = "ORK_METER_CONTEXT_HYGIENE"
SECRET = "sk-ant-PRIVATE-KEY-CANARY"
T0 = datetime.datetime(2026, 10, 14, 9, 0, tzinfo=datetime.timezone.utc)


def turn(mid, minutes, read, write, ttl, side=False, context_extra=5):
    split = {"ephemeral_1h_input_tokens": write if ttl == "1h" else 0,
             "ephemeral_5m_input_tokens": write if ttl == "5m" else 0}
    stamp = (T0 + datetime.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    return {"type": "assistant", "timestamp": stamp, "isSidechain": side,
            "message": {"id": mid, "model": "claude-opus-5-5", "role": "assistant",
                        "content": [{"type": "text", "text": "PRIVATE_REPLY_CANARY"}],
                        "usage": {"input_tokens": context_extra, "cache_read_input_tokens": read,
                                  "cache_creation_input_tokens": write, "output_tokens": 100,
                                  "cache_creation": split}}}


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "user", "message": {"content": "PRIVATE_PROMPT_CANARY"}}) + "\n")
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    return str(path)


class Base(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.project = self.root / "project"
        (self.home / ".claude").mkdir(parents=True)
        self.project.mkdir()
        (self.project / ".git").mkdir()   # the lever walk stops at the checkout's root
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()
        self.env = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.root / "state"),
                    "CLAUDE_PLUGIN_ROOT": str(ROOT), "CLAUDE_PLUGIN_DATA": os.environ["CLAUDE_PLUGIN_DATA"]}
        managed = patch.object(project_env, "MANAGED_FILES", ())
        managed.start()
        self.addCleanup(managed.stop)
        # detect's 0.25 s budget ends the transcript scan on a slow Windows
        # runner, and the next signal (a stray key) then answers: the budget
        # is product behaviour, not what these tests check.
        budget = patch.object(billing, "BUDGET_SECONDS", 30)
        budget.start()
        self.addCleanup(budget.stop)
        project_env._cache.clear()
        self.addCleanup(project_env._cache.clear)

    def payload(self, transcript="current.jsonl"):
        return {"cwd": str(self.project), "transcript_path": str(self.transcripts / transcript)}

    def settings(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        project_env._cache.clear()


class BillingDetection(Base):
    def test_observed_five_minute_cache_means_per_token_and_one_hour_subscription(self):
        write_rows(self.transcripts / "current.jsonl", [turn("a", 0, 0, 9000, "5m")])
        mode, basis = billing.detect(self.env, self.payload())
        self.assertEqual(mode, billing.PER_TOKEN)
        self.assertIn("5-minute", basis)
        write_rows(self.transcripts / "current.jsonl", [turn("a", 0, 0, 9000, "5m"), turn("b", 1, 9000, 500, "1h")])
        self.assertEqual(billing.detect(self.env, self.payload())[0], billing.SUBSCRIPTION, "the newest write decides")

    def test_subagent_writes_and_content_free_rows_are_not_evidence(self):
        write_rows(self.transcripts / "current.jsonl", [turn("a", 0, 0, 9000, "5m", side=True)])
        self.assertEqual(billing.detect(self.env, self.payload())[0], billing.UNKNOWN)

    def test_a_new_session_uses_recent_transcripts_in_its_folder_only(self):
        other = write_rows(self.transcripts / "earlier.jsonl", [turn("a", 0, 0, 9000, "5m")])
        mode, basis = billing.detect(self.env, self.payload())   # the current transcript doesn't exist yet
        self.assertEqual(mode, billing.PER_TOKEN)
        self.assertIn("recent transcript", basis)
        old = time.time() - billing.RECENT_SECONDS - 60
        os.utime(other, (old, old))
        self.assertEqual(billing.detect(self.env, self.payload())[0], billing.UNKNOWN)

    def test_a_hand_chosen_cache_lifetime_is_not_a_billing_signal(self):
        write_rows(self.transcripts / "current.jsonl", [turn("a", 0, 0, 9000, "1h")])
        self.assertEqual(billing.detect(dict(self.env, CLAUDE_CODE_PROMPT_CACHE_TTL="1h"), self.payload())[0], billing.UNKNOWN)
        self.settings(self.home / ".claude" / "settings.json", {"promptCacheTtl": "1h"})
        self.assertEqual(billing.detect(self.env, self.payload())[0], billing.UNKNOWN)

    def test_auth_configuration_by_name_only(self):
        mode, basis = billing.detect(dict(self.env, ANTHROPIC_API_KEY=SECRET), self.payload())
        self.assertEqual(mode, billing.PER_TOKEN)
        self.assertNotIn(SECRET, basis)
        self.assertEqual(billing.detect(dict(self.env, CLAUDE_CODE_USE_BEDROCK="1"), self.payload())[0], billing.PER_TOKEN)
        self.assertEqual(billing.detect(dict(self.env, CLAUDE_CODE_USE_BEDROCK="0"), self.payload())[0], billing.UNKNOWN)
        self.assertEqual(billing.detect(self.env, self.payload()), (billing.UNKNOWN, "no billing signal found"))
        self.settings(self.home / ".claude" / "settings.json", {"apiKeyHelper": "/bin/PRIVATE-helper"})
        mode, basis = billing.detect(self.env, self.payload())
        self.assertEqual(mode, billing.PER_TOKEN)
        self.assertNotIn("PRIVATE", basis)

    def test_observed_subscription_wins_over_a_stray_api_key(self):
        write_rows(self.transcripts / "current.jsonl", [turn("a", 0, 0, 9000, "1h")])
        self.assertEqual(billing.detect(dict(self.env, ANTHROPIC_API_KEY=SECRET), self.payload())[0], billing.SUBSCRIPTION)

    def test_an_exhausted_budget_falls_through_to_the_configured_signal(self):
        # What happened on the Windows runner: the scan ran out of time.
        write_rows(self.transcripts / "current.jsonl", [turn("a", 0, 0, 9000, "1h")])
        env = dict(self.env, ANTHROPIC_API_KEY=SECRET)
        self.assertEqual(billing.detect(env, self.payload(), deadline=time.monotonic() - 1)[0], billing.PER_TOKEN)
        self.assertEqual(billing.detect(env, self.payload())[0], billing.SUBSCRIPTION)

    def test_a_project_cannot_plant_a_billing_signal(self):
        self.settings(self.project / ".claude" / "settings.json", {"apiKeyHelper": "x", "env": {"ANTHROPIC_API_KEY": "x"}})
        self.assertEqual(billing.detect(dict(self.env, ANTHROPIC_API_KEY="x"), self.payload())[0], billing.UNKNOWN)
        (self.project / ".claude" / "settings.json").write_text("{broken")
        project_env._cache.clear()
        self.assertEqual(billing.detect(dict(self.env, ANTHROPIC_API_KEY="x"), self.payload())[0], billing.UNKNOWN,
                         "an unreadable project file makes every name untrusted")

    def test_a_project_that_steers_billing_or_caching_voids_the_cache_signal(self):
        # #110 review: a project's API key really bills per token, so its sessions write
        # 5-minute rows; reading those as billing would let the project turn the nudge on.
        write_rows(self.transcripts / "earlier.jsonl", [turn("a", 0, 0, 9000, "5m")])
        self.assertEqual(billing.detect(self.env, self.payload())[0], billing.PER_TOKEN, "control: no project settings")
        for value in ({"env": {"ANTHROPIC_API_KEY": "x"}}, {"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}},
                      {"env": {"FORCE_PROMPT_CACHING_5M": "1"}}, {"apiKeyHelper": "x"}, {"promptCacheTtl": "5m"}):
            with self.subTest(value=value):
                self.settings(self.project / ".claude" / "settings.local.json", value)
                self.assertEqual(billing.detect(self.env, self.payload())[0], billing.UNKNOWN)
        self.settings(self.project / ".claude" / "settings.local.json", {"env": {"UNRELATED": "1"}})
        self.assertEqual(billing.detect(self.env, self.payload())[0], billing.PER_TOKEN)

    def test_errors_fail_closed_to_unknown(self):
        with patch.object(billing, "observed", side_effect=RuntimeError("boom")):
            self.assertEqual(billing.detect(dict(self.env, ANTHROPIC_API_KEY="x"), self.payload())[0], billing.UNKNOWN)


class PinnedDefault(Base):
    def context(self, env=None, session_id="s1", event="SessionStart"):
        session_id = "%s-%s" % (session_id, self.root.name)
        payload = dict(self.payload(), session_id=session_id, scratchpad_dir=str(self.root / ("scratch-" + session_id)))
        ctx = hook.Context(dict(self.env, **(env or {})), payload, event, time.monotonic() + 5)
        policy.bind(ctx, publish=False)
        return ctx

    def test_per_token_session_gets_the_nudge_and_keeps_it_for_the_session(self):
        first = self.context({"ANTHROPIC_API_KEY": SECRET})
        self.assertTrue(features.enabled(first.env, "context_hygiene"))
        self.assertNotIn(SECRET, Path(first.store().policy_path).read_text(), "the pin holds no secret")
        later = self.context(event="UserPromptSubmit")   # the signal is gone: the pinned default stays
        self.assertTrue(features.enabled(later.env, "context_hygiene"))
        self.assertFalse(features.enabled(self.context({HYGIENE: "off"}, event="UserPromptSubmit").env, "context_hygiene"),
                         "an explicit off applies at once")

    def test_subscription_or_unknown_stays_off_and_never_flips_mid_session(self):
        write_rows(self.transcripts / "current.jsonl", [turn("a", 0, 0, 9000, "1h")])
        self.assertFalse(features.enabled(self.context().env, "context_hygiene"))
        self.assertFalse(features.enabled(self.context({"ANTHROPIC_API_KEY": "x"}, event="UserPromptSubmit").env, "context_hygiene"))
        self.assertFalse(features.enabled(self.context(session_id="s2").env, "context_hygiene"))

    def test_an_explicit_setting_wins_and_skips_detection(self):
        with patch.object(billing, "detect", side_effect=AssertionError("must not run")):
            self.assertFalse(features.enabled(self.context({"ANTHROPIC_API_KEY": "x", HYGIENE: "off"}).env, "context_hygiene"))
            self.assertTrue(features.enabled(self.context({HYGIENE: "on"}, session_id="s2").env, "context_hygiene"))

    def test_detection_runs_once_per_session(self):
        calls = []
        real = billing.detect
        with patch.object(billing, "detect", side_effect=lambda *a, **k: calls.append(1) or real(*a, **k)):
            for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "UserPromptSubmit"):
                self.context({"ANTHROPIC_API_KEY": "x"}, event=event)
        self.assertEqual(len(calls), 1)

    def test_a_project_can_still_only_turn_it_off(self):
        self.settings(self.project / ".claude" / "settings.json", {"env": {HYGIENE: "off"}})
        self.assertFalse(features.enabled(self.context({"ANTHROPIC_API_KEY": "x", HYGIENE: "off"}).env, "context_hygiene"))
        self.settings(self.project / ".claude" / "settings.json", {"env": {HYGIENE: "on"}})
        self.assertFalse(features.enabled(self.context({HYGIENE: "on"}, session_id="s2").env, "context_hygiene"),
                         "a project's on is ignored when nothing says per token")

    def test_a_project_api_key_never_pins_the_nudge_on_in_later_sessions(self):
        self.settings(self.project / ".claude" / "settings.json", {"env": {"ANTHROPIC_API_KEY": "x"}})
        write_rows(self.transcripts / "earlier.jsonl", [turn("a", 0, 0, 9000, "5m")])   # an earlier session it billed per token
        for sid in ("s1", "s2"):
            self.assertFalse(features.enabled(self.context({"ANTHROPIC_API_KEY": "x"}, session_id=sid).env, "context_hygiene"), sid)

    def test_pure_registry_helpers(self):
        self.assertEqual(features.PER_TOKEN_DEFAULTS, {"context_hygiene": HYGIENE})
        self.assertEqual(features.with_billing_defaults({}, True)[HYGIENE], "on")
        self.assertNotIn(HYGIENE, features.with_billing_defaults({}, False))
        self.assertEqual(features.with_billing_defaults({HYGIENE: "off"}, True)[HYGIENE], "off")
        pinned = features.policy_settings({HYGIENE: "on"})
        self.assertEqual(features.apply_policy_settings({}, pinned)[HYGIENE], "on", "unset keeps the pinned default")
        self.assertEqual(features.apply_policy_settings({HYGIENE: "no"}, pinned)[HYGIENE], "off")
        self.assertEqual(features.apply_policy_settings({}, features.policy_settings({}))[HYGIENE], "off")
        self.assertEqual(features.apply_policy_settings({}, features.policy_settings({"ORK_METER_STALE_GUARD": "on"}))["ORK_METER_STALE_GUARD"],
                         "off", "other opt-ins still turn off when unset")


class AuditAdvice(Base):
    def setUp(self):
        super().setUp()
        self.audit = load("scripts/audit.py")
        self.config = self.root / "claude"
        self.env["CLAUDE_CONFIG_DIR"] = str(self.config)

    def kinds(self, result, kind):
        return [r for r in result["recommendations"] if r["kind"] == kind]

    def test_lsp_plugins_for_typed_languages_from_names_only(self):
        (self.project / "tsconfig.json").write_text("PRIVATE_CONFIG_CANARY")
        (self.project / "src").mkdir()
        for n in range(3):
            (self.project / "src" / ("m%d.ts" % n)).write_text("PRIVATE_SOURCE_CANARY")
        for n in range(9):
            (self.project / "src" / ("p%d.py" % n)).write_text("x = 1")
        (self.project / "node_modules" / "dep").mkdir(parents=True)
        for n in range(9):
            (self.project / "node_modules" / "dep" / ("d%d.go" % n)).write_text("")
        result = self.audit.audit(str(self.project), str(self.home), self.env)
        lsp = self.kinds(result, "lsp_plugin")
        self.assertEqual([r["plugin"] for r in lsp], ["typescript-lsp"], "plain .py files and dependencies don't count")
        self.assertEqual(lsp[0]["basis"]["source_files"], 3)
        self.assertEqual(lsp[0]["basis"]["config_files"], ["tsconfig.json"])
        self.assertIn("3 .ts/.tsx/.mts/.cts files", lsp[0]["text"])
        self.assertNotIn("CANARY", json.dumps(result))
        (self.project / "mypy.ini").write_text("")
        self.assertIn("pyright-lsp", [r["plugin"] for r in self.kinds(self.audit.audit(str(self.project), str(self.home), self.env), "lsp_plugin")])
        # The user's settings are in $CLAUDE_CONFIG_DIR when it is set, as for Claude Code (#191).
        self.settings(Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json",
                      {"enabledPlugins": {"typescript-lsp@claude-plugins-official": True}})
        self.assertNotIn("typescript-lsp", [r["plugin"] for r in self.kinds(self.audit.audit(str(self.project), str(self.home), self.env), "lsp_plugin")],
                         "an enabled plugin is not recommended again")

    def project_transcript(self, rows):
        folder = self.config / "projects" / __import__("re").sub(r"[^A-Za-z0-9]", "-", os.path.realpath(self.project))
        return write_rows(folder / "session.jsonl", rows)

    def idle_rows(self, ttl, gap):
        rows, minutes = [turn("m0", 0, 0, 20000, ttl)], 0
        for n in range(1, 5):
            minutes += gap
            rows.append(turn("m%d" % n, minutes, 0, 20000 + n * 500, ttl))
        return rows

    def test_one_hour_cache_only_for_five_minute_writes_with_idle_rewrites(self):
        self.project_transcript(self.idle_rows("5m", 12))
        result = self.audit.audit(str(self.project), str(self.home), self.env)
        advice = self.kinds(result, "cache_1h")
        self.assertEqual(len(advice), 1)
        basis = advice[0]["basis"]
        self.assertEqual((basis["requests"], basis["requests_5m"], basis["requests_1h"], basis["idle_rewrites"]), (5, 5, 0, 4))
        self.assertIn("4 came after a 5-60 minute pause", advice[0]["text"])
        self.assertIn('"promptCacheTtl": "1h"', advice[0]["text"])
        self.assertNotIn("CANARY", json.dumps(result))

    def test_no_cache_advice_on_a_subscription_or_without_idle_rewrites(self):
        self.project_transcript(self.idle_rows("1h", 12))
        result = self.audit.audit(str(self.project), str(self.home), self.env)
        self.assertEqual(self.kinds(result, "cache_1h"), [])
        self.assertEqual(result["cache_lifetime"]["requests_1h"], 5)
        self.project_transcript(self.idle_rows("5m", 2))
        self.assertEqual(self.kinds(self.audit.audit(str(self.project), str(self.home), self.env), "cache_1h"), [])
        self.project_transcript(self.idle_rows("5m", 90))   # past an hour, 1 hour would not have helped
        self.assertEqual(self.kinds(self.audit.audit(str(self.project), str(self.home), self.env), "cache_1h"), [])

    def test_audit_states_the_nudge_default_and_its_basis(self):
        self.project_transcript([turn("a", 0, 0, 9000, "5m")])
        result = self.audit.audit(str(self.project), str(self.home), self.env)
        self.assertTrue(result["context_nudge_default"]["on"])
        self.assertIn("5-minute", result["context_nudge_default"]["basis"])
        result = self.audit.audit(str(self.project), str(self.home), dict(self.env, **{HYGIENE: "off"}))
        self.assertEqual(result["context_nudge_default"], {"on": False, "basis": "set explicitly"})
        self.settings(self.project / ".claude" / "settings.json", {"env": {HYGIENE: "on"}})
        result = self.audit.audit(str(self.project), str(self.home), dict(self.env, **{HYGIENE: "on"}))
        self.assertNotEqual(result["context_nudge_default"]["basis"], "set explicitly", "a project's on is ignored, as in the hooks")


if __name__ == "__main__":
    unittest.main()
