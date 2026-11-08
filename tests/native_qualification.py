"""Installed Claude CLI qualification using localhost synthetic inference only."""
import json
import os
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import entitled  # noqa: E402
import host_retry  # noqa: E402
import plus_policy_sign  # noqa: E402
from native_host import Fixture, Host, hook_evidence, latest_tool_delivery, result_text  # noqa: E402
from native_cache_qualification import history  # noqa: E402
sys.path.insert(0, str(ROOT / "hooks/lib"))
import ledger  # noqa: E402
import read_tool  # noqa: E402
import session  # noqa: E402


def content_only(messages):
    # Opus changes string/single-text-block containers. Keep actual content,
    # roles and tool data exact; raw wire equality is reported by the cache suite.
    return history(messages)


def delivered_text(body):
    """The latest tool result and the hook context after it, as the model reads them."""
    parts = []
    for message in latest_tool_delivery(body):
        content = message.get("content")
        for block in content if isinstance(content, list) else [{"text": content}]:
            if isinstance(block, dict):
                parts.append(result_text(block) if block.get("type") == "tool_result" else str(block.get("text", "")))
    return "\n".join(parts)


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

    def test_a_cut_read_says_so_keeps_real_numbers_and_a_re_read_gets_the_rest(self):
        """F016 (0.5.8, Claude Code 2.1.316): the Read budget kept lines 1-706 of
        a 1,402-line file, nothing told the model, and its whole-file re-reads
        were answered "file unchanged". Now the cut result ends in the marker at
        its own line number, the fixed notice reaches the model beside it, and a
        whole-file re-read is sent from the first line not shown, so the host
        returns the rest instead of its unchanged-file answer."""
        with Host(self, "ork-plugin-native-rest-", "Read", inference=Fixture) as host:
            env = host.env("reduce")
            lines = ["evidence_%04d exact source context" % i for i in range(1402)]
            path = host.work / "fields.py"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            Fixture.calls, Fixture.requests, Fixture.arrivals = [("Read", {"file_path": str(path)}),
                                                                 ("Read", {"file_path": str(path)})], [], []
            result = host.run("Read the fixture file twice.", env=env, system="Use the requested local files.")
            host.assert_no_external_traffic()
            self.assertEqual(len(Fixture.requests), 3)
            first = delivered_text(Fixture.requests[1])
            found = re.search(r"\n(\d+)\t\u2026 lines (\d+)\u2013(\d+) not shown; Read with offset=(\d+)", first)
            self.assertIsNotNone(found, hook_evidence(result.stdout))
            number, start, end, offset = (int(value) for value in found.groups())
            self.assertEqual((number, offset), (start, start), "the marker sits at its own line number")
            self.assertIn(end, (1402, 1403))
            self.assertIn("\n%d\tevidence_%04d exact source context" % (start - 1, start - 2), first)
            self.assertNotIn("evidence_%04d" % (start - 1), first)
            self.assertIn("Ork Meter shortened this Read of %s: it shows lines 1\u2013%d" % (path, start - 1), first,
                          "the notice reaches the model")
            second = delivered_text(Fixture.requests[2])
            self.assertNotIn("unchanged", second.lower())
            self.assertIn("%d\tevidence_%04d exact source context" % (start, start - 1), second)
            self.assertIn("1402\tevidence_1401 exact source context", second)
            [store] = host.stores().values()
            self.assertEqual([row["kind"] for row in ledger.load(store)], ["trim"])

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
            # No plugin prose reaches the model (hooks/lib/native_text.py clean_reply drops
            # additionalContext; source_output keeps only received lines): the evidence of a
            # reduction is the received-only text plus the ledger rows of this session's store.
            if ast_file:
                delivered = json.dumps(first)
                self.assertNotIn("Python AST outline", delivered)
                if mode == "reduce":   # Read compaction is opt-in (#316): the guard pages as before
                    # The guard's updatedInput survives the boundary (limit=OUTLINE_LIMIT lines);
                    # the outline it used to add as additionalContext does not, so the distant
                    # symbol is no longer in the delivery: the read_guard ledger row is the evidence.
                    self.assertIn("# line %d " % (read_tool.OUTLINE_LIMIT - 3), delivered)
                    self.assertNotIn("# line %d " % (read_tool.OUTLINE_LIMIT - 2), delivered)
                    self.assertNotIn("# line 340", delivered)
                    self.assertNotIn("distant", delivered)
                    [store] = host.stores().values()
                    guarded = [row for row in ledger.load(store) if row["kind"] == "read_guard"]
                    self.assertEqual([row["path_sha256"] for row in guarded], [ledger.sha256_hex(paths[0])],
                                     hook_evidence(result.stdout))
                else:
                    self.assertIn("# line 340", delivered)
                    if ast_lines > 1000:
                        self.assertIn("[Truncated: PARTIAL view", delivered)
                        self.assertNotIn("distant", delivered)
                    else:
                        self.assertIn("distant", delivered)
            elif mode == "reduce":
                # The model gets the exact head lines, the marker for the rest and the fixed
                # "shortened" notice (no snapshot path); the full text is in the session's
                # tool-output snapshot and the ledger holds the trim row.
                delivered = json.dumps(first)
                self.assertNotIn("evidence_0499", delivered, hook_evidence(result.stdout))
                self.assertIn("evidence_0000 exact source context", delivered)
                self.assertIn("Ork Meter shortened this Read of", delivered)
                self.assertIn("not shown; Read with offset=", delivered)
                self.assertNotIn("tool-output", delivered)
                [store] = host.stores().values()
                # One snapshot per trimmed Read: the second, unranged Read is trimmed too
                # under the pinned 1 KiB budget (mutate_defaults); the ranged one is not.
                snapshots = list(Path(store.tool_output_dir()).glob("*.txt"))
                self.assertEqual([path.read_text(encoding="utf-8") for path in snapshots],
                                 [original] * (2 if mutate_defaults else 1))
                row = [row for row in ledger.load(store) if row["kind"] == "trim"][0]
                self.assertEqual((row["tool"], row.get("native_text_boundary_reconciled")), ("Read", True))
                self.assertLess(row["bytes_after"], row["bytes_before"])
            else:
                self.assertIn("evidence_0499", json.dumps(first))
                traces = list(temporary_files.rglob("shadow-traces/*.json"))
                self.assertTrue(traces, "installed hook must produce local observations")
                observed = [json.loads(path.read_text()) for path in traces]
                post = [item for item in observed if item["event"] == "PostToolUse"]
                # Every PostToolUse hook on Read captures in shadow mode (hook.run), and
                # polling-completion.py now matches Read as well as dedupe-reads.py: one
                # observation per hook, so count the Reads observed, not the files.
                self.assertEqual(len({item["tool_use_id"] for item in post}), 2)
                self.assertEqual({item["tool_name"] for item in post}, {"Read"})
                for item in post:
                    self.assertIn("evidence_0499", json.dumps(item["tool_response"]))
            if mutate_defaults:
                # Still trimmed under the pinned 1 KiB budget: the head, a marker and the notice.
                self.assertNotIn("evidence_0499", json.dumps(second))
                self.assertIn("evidence_0000 exact source context", json.dumps(second))
                self.assertIn("Ork Meter shortened this Read of", json.dumps(second))
                pins = list(temporary_files.rglob(session.POLICY_NAME))
                self.assertEqual(len(pins), 1)
                pin = json.loads(pins[0].read_text())
                self.assertEqual(pin["settings"]["ORK_METER_TRIM_BUDGET_READ"], "1024")
                for path in temporary_files.rglob(session.LEDGER_NAME):
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    self.assertTrue(rows)
                    self.assertEqual([row["kind"] for row in rows], ["trim", "trim"])
                    self.assertTrue(all(row["policy_sha256"] == pin["policy_sha256"] for row in rows))
            else:
                # An explicit range (limit=1000) is never shortened: the exact page arrives.
                self.assertIn("evidence_0499", json.dumps(second))
                self.assertNotIn("Ork Meter", json.dumps(second))
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
