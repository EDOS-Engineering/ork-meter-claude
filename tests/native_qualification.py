"""Installed Claude CLI qualification using localhost synthetic inference only."""
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import entitled  # noqa: E402
import host_retry  # noqa: E402
import plus_policy_sign  # noqa: E402
from native_host import Fixture, Host, hook_evidence, latest_tool_delivery  # noqa: E402
from native_cache_qualification import history  # noqa: E402
sys.path.insert(0, str(ROOT / "hooks/lib"))
import session  # noqa: E402


def content_only(messages):
    # Opus changes string/single-text-block containers. Keep actual content,
    # roles and tool data exact; raw wire equality is reported by the cache suite.
    return history(messages)


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "opt in to installed Claude + localhost fixture")
@host_retry.retry_once
class NativeQualification(unittest.TestCase):
    def test_automatic_python_ast_outline_and_exact_followup(self):
        reduced = self.qualify("reduce", ast_file=True)
        unmodified = self.qualify("off", ast_file=True)
        self.assertEqual(reduced, unmodified,
                         "AST delivery must preserve the initial system, tools and cache controls")

    def test_extended_python_outline_preserves_cache_prefix(self):
        reduced = self.qualify("reduce", ast_file=True, ast_lines=1500)
        unmodified = self.qualify("off", ast_file=True, ast_lines=1500)
        self.assertEqual(reduced, unmodified,
                         "large AST delivery must preserve the initial system, tools and cache controls")

    def test_new_defaults_do_not_change_native_session_policy(self):
        self.qualify("reduce", mutate_defaults=True)

    def test_new_read_is_reduced_and_explicit_recovery_range_is_preserved(self):
        self.qualify("reduce")

    def test_shadow_preserves_tool_results_and_captures_received_output(self):
        self.qualify("shadow")

    def qualify(self, mode, mutate_defaults=False, ast_file=False, ast_lines=350):
        with Host(self, "ork-plugin-native-", "Read", inference=Fixture) as host:
            base, plugin, work, temporary_files = host.base, host.plugin, host.work, host.scratch
            change_defaults = None
            env = host.env(mode) if mutate_defaults else host.env(mode, ORK_METER_TRIM_BUDGET_READ="1024")
            if mutate_defaults:
                # The defaults come from the signed Plus policy (hooks/lib/entitlement.py),
                # which host.env has just seeded with the contract's tunables. Seed this
                # session's envelope with a 1 KiB Read budget instead; after the first
                # Read pinned it, a re-signed envelope raises it to 64 KiB.
                def policy(identity, budget):
                    tunables = dict(plus_policy_sign.DEFAULT_POLICY["tunables"], **{"trim.budget_read": budget})
                    return dict(plus_policy_sign.DEFAULT_POLICY, id=identity, tunables=tunables)
                entitled.write(str(host.plugin_data), policy=policy("native-small-read", 1024))
                def change_defaults():
                    entitled.write(str(host.plugin_data), policy=policy("native-wide-read", 64 * 1024))
            original = "".join("evidence_%04d exact source context\n" % i for i in range(500))
            paths = [str(work / ("large.py" if ast_file else "noisy.txt")), str(work / "exact.txt")]
            Path(paths[0]).write_text(
                ('def first():\n    return 1\n' + ''.join('# line %d %s\n' % (i, 'x' * 100) for i in range(ast_lines))
                 + 'def distant():\n    return 2\n') if ast_file else original, encoding="utf-8")
            Path(paths[1]).write_text(original, encoding="utf-8")

            def second_read(_body):
                # The second request arrives after the first Read's hooks ran.
                if change_defaults is not None:
                    change_defaults()
                return {"file_path": paths[1]} if mutate_defaults else {"file_path": paths[1], "limit": 1000}
            Fixture.calls, Fixture.requests, Fixture.arrivals = [("Read", {"file_path": paths[0]}), ("Read", second_read)], [], []
            result = host.run("Read the two fixture files.", env=env, system="Use the requested local files.")
            host.assert_no_external_traffic()
            self.assertEqual(len(Fixture.requests), 3)
            first = latest_tool_delivery(Fixture.requests[1])
            second = latest_tool_delivery(Fixture.requests[2])
            if ast_file:
                delivered = json.dumps(first)
                if mode == "reduce":   # Read compaction is opt-in (#316): the guard outlines as before
                    self.assertIn("Python AST outline", delivered)
                    self.assertNotIn("# line 340", delivered)
                else:
                    self.assertNotIn("Python AST outline", delivered)
                    self.assertIn("# line 340", delivered)
                if mode == "off" and ast_lines > 1000:
                    self.assertIn("[Truncated: PARTIAL view", delivered)
                    self.assertNotIn("distant", delivered)
                else:
                    self.assertTrue("distant" in delivered, "AST map must expose the distant symbol")
            elif mode == "reduce":
                self.assertIn("Ork Meter trimmed this Read", json.dumps(first), hook_evidence(result.stdout))
                self.assertIn("Full original Read snapshot", json.dumps(first))
                self.assertNotIn("evidence_0499", json.dumps(first))
            else:
                self.assertIn("evidence_0499", json.dumps(first))
                self.assertNotIn("Ork Meter trimmed this Read", json.dumps(first))
                traces = list(temporary_files.rglob("shadow-traces/*.json"))
                self.assertTrue(traces, "installed hook must produce local observations")
                observed = [json.loads(path.read_text()) for path in traces]
                post = [item for item in observed if item["event"] == "PostToolUse"]
                self.assertEqual(len(post), 2)
                for item in post:
                    self.assertIn("evidence_0499", json.dumps(item["tool_response"]))
            if mutate_defaults:
                self.assertIn("Ork Meter trimmed this Read", json.dumps(second))
                self.assertNotIn("evidence_0499", json.dumps(second))
                pins = list(temporary_files.rglob(session.POLICY_NAME))
                self.assertEqual(len(pins), 1)
                pin = json.loads(pins[0].read_text())
                self.assertEqual(pin["settings"]["ORK_METER_TRIM_BUDGET_READ"], "1024")
                for path in temporary_files.rglob(session.LEDGER_NAME):
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    self.assertTrue(rows)
                    self.assertTrue(all(row["policy_sha256"] == pin["policy_sha256"] for row in rows))
            else:
                self.assertIn("evidence_0499", json.dumps(second))
                self.assertNotIn("Ork Meter trimmed this Read", json.dumps(second))
            earlier = Fixture.requests[1]["messages"]
            self.assertEqual(content_only(Fixture.requests[2]["messages"][:len(earlier)]), content_only(earlier),
                             "previously delivered context must remain unchanged")
            global_flags = json.loads((base / "state/ork-meter/plugin-features.json").read_text())
            if mode == "reduce":
                self.assertIn("trim", global_flags["features"])
            else:
                self.assertNotIn("trim", global_flags["features"])
            bound = list((base / "state/ork-meter/session-features").glob("*.json"))
            self.assertEqual(len(bound), 1)
            bound_flags = json.loads(bound[0].read_text())
            for key in ("features", "plugin_version"):
                self.assertEqual(bound_flags[key], global_flags[key])
            if ast_file:
                initial = Fixture.requests[0]
                stable = {key: initial.get(key) for key in
                          ("system", "tools", "tool_choice", "cache_control", "thinking", "output_config")}
                return json.loads(json.dumps(stable, sort_keys=True).replace(str(base), "<fixture-root>"))


if __name__ == "__main__":
    unittest.main()
