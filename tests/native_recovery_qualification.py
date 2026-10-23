"""Installed-host artifact recovery, localhost synthetic inference only."""
import json
import os
import re
from pathlib import Path
import shlex
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import host_retry  # noqa: E402
from native_host import Fixture, Host, assert_hooks_fail_open, latest_tool_results, result_text, tool_id, tool_results  # noqa: E402
from native_result_assertions import assert_host_suffix
sys.path.insert(0, str(ROOT / "hooks/lib"))
import artifacts
import ledger
import session


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "opt in to installed host + localhost inference")
@host_retry.retry_once
class NativeRecovery(unittest.TestCase):
    def test_full_range_and_search_recovery_are_not_reduced_again(self):
        with Host(self, "ork-native-recovery-", "Bash,Read", inference=Fixture) as host:
            base, plugin, work, scratch = host.base, host.plugin, host.work, host.scratch
            original = {"stdout": "".join("record_%04d evidence\n" % i for i in range(3000)),
                        "stderr": "exact separate stderr", "exitCode": 1, "interrupted": False,
                        "final_marker": "END_EXACT_ORIGINAL"}
            origin = session.Store.for_payload({"scratchpad_dir": str(base / "originals"), "session_id": "native-origin-" + base.name})
            path = artifacts.save(origin, "Bash", original, original["stdout"])
            prefix = ('bash ' + shlex.quote(str(plugin / "hooks/py.sh").replace("\\", "/")) + ' --script scripts/artifact.py --scratchpad ' +
                      shlex.quote(str(base / "originals").replace("\\", "/")) + " --id " + Path(path).stem)
            persisted = []
            def exact_native_read(body):
                results = latest_tool_results(body)
                raw = results[0]["content"]
                if isinstance(raw, list):
                    raw = "".join(item.get("text", "") for item in raw)
                match = re.search(r"Full output saved to: ([^\r\n]+)", raw)
                self.assertIsNotNone(match, "large recovery should use the native persisted-result handoff")
                saved = Path(match.group(1))
                self.assertEqual(json.loads(saved.read_text()), original)
                persisted.append(saved)
                return {"file_path": str(saved), "offset": 1, "limit": 10000}
            actions = ["--full", "--start 2751 --limit 250", "--search evidence --start 2751 --limit 250"]
            expected = [None, original, artifacts.select(artifacts.load(origin, Path(path).stem), 2751, 250),
                        artifacts.select(artifacts.load(origin, Path(path).stem), 2751, 250, "evidence")]
            Fixture.calls = [("Bash", {"command": prefix + " --full"}), ("Read", exact_native_read)]
            Fixture.calls += [("Bash", {"command": prefix + " " + action}) for action in actions[1:]]
            Fixture.requests, Fixture.arrivals = [], []
            host.run("Recover the fixture.", system="Retrieve the requested local original evidence.")
            host.assert_no_external_traffic()
            self.assertEqual(len(Fixture.requests), 5)
            for i, value in enumerate(expected):
                results = latest_tool_results(Fixture.requests[i + 1])
                self.assertEqual(len(results), 1)
                self.assertFalse(results[0].get("is_error", False), str(results[0].get("content"))[:1000])
                raw = results[0]["content"]
                if isinstance(raw, list):
                    raw = "".join(item.get("text", "") for item in raw)
                self.assertNotIn("Full output:", raw)
                if value is None:
                    self.assertIn("<persisted-output>", raw)
                    continue
                if i == 1:
                    # Native Read formats line numbers around exact source.
                    raw = re.sub(r"^\s*\d+(?:→|\t|$)", "", raw, flags=re.MULTILINE)
                    raw = raw.split("\n\n<system-reminder>", 1)[0]
                self.assertTrue(raw.lstrip().startswith("{"), raw[:1200])
                parsed, end = json.JSONDecoder().raw_decode(raw)
                assert_host_suffix(self, raw[end:], work)
                self.assertEqual(parsed, value, "host-visible recovery must equal the CLI selection")
                self.assertIn("record_2999", raw)
                self.assertGreater(len(raw.encode("utf-8")), 12 * 1024)
            self.assertEqual([r["kind"] for r in ledger.load(origin)], ["recovery"] * 3)
            # Recovery cannot create another artifact of its own output.
            files = list(Path(origin.tool_output_dir()).glob("*.json"))
            self.assertEqual(len(files), 1)
            other_artifacts = list(scratch.rglob("tool-output/*.json"))
            self.assertEqual(other_artifacts, [])
            for ledger_path in scratch.rglob("savings.jsonl"):
                rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
                self.assertFalse(any(row["kind"] in ("trim", "test_filter") for row in rows))


def permission_denials(stream):
    """Every ``permission_denials`` entry of the CLI's result events."""
    found = []
    for line in stream.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("type") == "result":
            found += item.get("permission_denials") or []
    return found


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "opt in to installed host + localhost inference")
@host_retry.retry_once
class NativeRestrictedRecovery(unittest.TestCase):
    def test_restricted_session_reads_trimmed_output_with_bash_as_the_note_says(self):
        """#269: under --restricted the file tools are confined to the
        workspace, so a Read of the saved output is refused (a wasted
        request). The note says to read it with Bash; sed -n pages of it
        arrive untrimmed and nothing is denied."""
        with Host(self, "ork-native-restricted-recovery-", "Bash,Read") as host:
            # 27 KB: over the 12 KiB Bash budget, under the CLI's own 30,000
            # character Bash limit, as each page is.
            text = "".join("record_%04d evidence padding\n" % i for i in range(900))
            (host.work / "big.txt").write_text(text, encoding="utf-8")
            seen = {}

            def script(body, number):
                if number == 1:
                    return [("tool", "Bash", {"command": "cat big.txt"})]
                if number == 2:
                    seen["note"] = result_text(tool_results(body)[tool_id(1, 0)]).rsplit("Full output: ", 1)[1]
                    seen["path"] = seen["note"].rsplit(" (", 1)[0]
                if number in (2, 3):
                    # Numeric ranges, as the note shows: on Windows (2.1.280 and
                    # 2.1.302) the CLI's permission check denies sed -n '451,$p'
                    # of the same file in dontAsk mode, though Bash is allowed.
                    pages = {2: "1,450p", 3: "451,900p"}
                    return [("tool", "Bash", {"command": "sed -n '%s' %s" % (pages[number], shlex.quote(seen["path"]))})]
                return [("text", "QUALIFIED")]
            host.script = script
            completed = host.run("Show the whole file.", restricted=True)
            host.assert_no_external_traffic()
            assert_hooks_fail_open(self, completed.stdout)
            self.assertEqual(len(host.requests), 4)
            self.assertEqual(permission_denials(completed.stdout), [], [
                result_text(block)[:600] for body in host.requests[2:] for block in tool_results(body).values()
                if block.get("is_error")])
            first = result_text(tool_results(host.requests[1])[tool_id(1, 0)])
            self.assertIn("Ork Meter trimmed", first, first[-1000:])
            self.assertNotIn("record_0450", first, "the middle was trimmed away")
            self.assertEqual(seen["note"].split("\n")[1],
                             "Outside the workspace; Bash sed -n 'A,Bp', head, tail or cat reads it.")
            assert_host_suffix(self, "\n".join(seen["note"].split("\n")[2:]), host.work)
            recovered = ""
            for number in (2, 3):
                block = tool_results(host.requests[number])[tool_id(number, 0)]
                raw = result_text(block)
                self.assertFalse(block.get("is_error", False), raw[:1000])
                self.assertNotIn("Full output:", raw)
                self.assertNotIn("Ork Meter", raw)
                notice = "Shell cwd was reset to " + str(host.work)   # Windows only
                if notice in raw:
                    raw, _, rest = raw.rpartition(notice)
                    assert_host_suffix(self, notice + rest, host.work)
                    raw = raw.rstrip("\n")
                recovered += raw + "\n"
            # The Bash tool drops each output's final newline; the saved
            # original is the cat output as the hook received it.
            self.assertEqual(recovered, text, "the pages are the full original")
            [store] = host.stores().values()
            self.assertEqual([row["kind"] for row in ledger.load(store)], ["trim"])
            self.assertEqual(len(list(Path(store.tool_output_dir()).glob("*.txt"))), 1, "no second artifact")

            # Control: a Read of the same path in this restricted host is
            # refused, which is why the note points at Bash.
            host.requests.clear()
            host.script = lambda body, number: ([("tool", "Read", {"file_path": seen["path"]})] if number == 1
                                                else [("text", "QUALIFIED")])
            control = host.run("Read the saved output.", restricted=True)
            block = tool_results(host.requests[1])[tool_id(1, 0)]
            self.assertTrue(block.get("is_error"), result_text(block)[:500])
            self.assertIn("--restricted", result_text(block))
            self.assertEqual([denial["tool_name"] for denial in permission_denials(control.stdout)], ["Read"])


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "opt in to installed host + localhost inference")
@host_retry.retry_once
class NativePersistedBash(unittest.TestCase):
    def test_bash_output_the_host_persisted_is_neither_trimmed_nor_credited(self):
        """A Bash output over the CLI's own 30,000 character limit is saved
        under ``<session>/tool-results/`` and the model gets the host's
        ``<persisted-output>`` preview. The hook leaves it alone: it neither
        replaces it nor saves a copy nor records a saving the model never
        received (#100, #350)."""
        with Host(self, "ork-native-persisted-", "Bash") as host:
            text = "".join("+    line %04d of a large diff with some content padding padding\n" % i for i in range(900))
            (host.work / "big.txt").write_text(text, encoding="utf-8")
            host.script = lambda body, number: ([("tool", "Bash", {"command": "cat big.txt"})] if number == 1
                                                else [("text", "QUALIFIED")])
            completed = host.run("Show the file.")
            host.assert_no_external_traffic()
            assert_hooks_fail_open(self, completed.stdout)
            self.assertEqual(len(host.requests), 2)
            raw = result_text(tool_results(host.requests[1])[tool_id(1, 0)])
            self.assertIn("<persisted-output>", raw, raw[:600])
            self.assertNotIn("Full output:", raw)
            self.assertNotIn("Ork Meter", raw)
            for store in host.stores().values():
                self.assertEqual([row["kind"] for row in ledger.load(store) if row["kind"] in ("trim", "test_filter")], [])
                self.assertEqual(list(Path(store.tool_output_dir()).glob("*")), [], "no copy of a host-persisted output")


if __name__ == "__main__":
    unittest.main()
