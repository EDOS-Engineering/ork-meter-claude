"""A filtered test log still over the Bash budget is delivered, not dropped (#200).

When ``filter_test_output`` removed the passing tests but the failures alone
were over budget and every remaining line was a mandatory line or its context,
the head/tail trim changed nothing. The hook still recorded that no-op trim as
the final stage, charged the recovery note to it, found the stage expanding and
dropped the whole reply, so the filter's saving was lost too.

``tests/fixtures/trim_tool_output_reference.py`` is hooks/trim-tool-output.py
as it was before the fix, verbatim. Every result it replied to must get a
byte-identical reply and the same ledger rows from the current hook.
"""
import json
from pathlib import Path
import random
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import StoreCase, load, payload, run_hook  # noqa: E402
import entitled  # noqa: E402
entitled.install()
import hook  # noqa: E402
import ledger  # noqa: E402
import session  # noqa: E402
import token_gate  # noqa: E402
import trimmer  # noqa: E402

BUDGET = 12 * 1024
current = load("hooks/trim-tool-output.py")
reference = load("tests/fixtures/trim_tool_output_reference.py")

def pytest_log(passes, failures, pad=0):
    total = passes + failures
    lines = ["============================= test session starts =============================="]
    lines += ["tests/test_mod.py::test_ok_%04d PASSED [%3d%%]" % (i, i * 100 // total) for i in range(passes)]
    lines += ["tests/test_mod.py::test_bad_%04d FAILED [%3d%%]" % (i, 100) for i in range(failures)]
    lines.append("=================================== FAILURES ===================================")
    for i in range(failures):
        lines += ["______________________________ test_bad_%04d ______________________________" % i, "",
                  "    def test_bad_%04d():" % i, "        value = compute(%d)%s" % (i, " " * pad),
                  ">       assert value == %d" % (i + 1), "E       assert %d == %d" % (i, i + 1), "",
                  "tests/test_mod.py:%d: AssertionError" % (100 + i)]
    lines.append("=========================== short test summary info ============================")
    lines += ["FAILED tests/test_mod.py::test_bad_%04d - assert %d == %d" % (i, i, i + 1) for i in range(failures)]
    lines.append("======================= %d failed, %d passed in 3.21s =======================" % (failures, passes))
    return "\n".join(lines) + "\n"


def go_log(passes, failures):
    lines = []
    for i in range(passes):
        lines += ["=== RUN   TestOk%04d" % i, "--- PASS: TestOk%04d (0.00s)" % i]
    for i in range(failures):
        lines += ["=== RUN   TestBad%04d" % i, "    bad_test.go:%d: expected %d, got %d" % (10 + i, i + 1, i),
                  "--- FAIL: TestBad%04d (0.00s)" % i]
    lines += ["FAIL", "exit status 1", "FAIL\texample.com/pkg\t0.412s"]
    return "\n".join(lines) + "\n"


def jest_log(passes, failures):
    lines = ["PASS src/ok.test.js (1.2 s)"]
    lines += ["  ✓ adds case %04d (3 ms)" % i for i in range(passes)]
    lines.append("FAIL src/bad.test.js")
    lines += ["  ✕ case %04d fails on negative input (5 ms)" % i for i in range(failures)]
    for i in range(failures):
        lines += ["  ● calc › case %04d fails on negative input" % i, "",
                  "    expect(received).toBe(expected) // failed", "    Expected: %d" % (i + 1), "    Received: %d" % i,
                  "      at Object.<anonymous> (src/bad.test.js:%d:5)" % (10 + i)]
    lines += ["Tests:       %d failed, %d passed, %d total" % (failures, passes, failures + passes)]
    return "\n".join(lines) + "\n"


class Harness(StoreCase):
    def invoke(self, module, stdout, stderr="", command="pytest -v", env=None):
        """(raw reply, ledger rows without their time) from a fresh store."""
        shutil.rmtree(self.scratch, ignore_errors=True)
        event = payload(self.scratch, "Bash", {"command": command},
                        {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False},
                        hook_event_name="PostToolUse", session_id="over-budget", tool_use_id="call", cwd=str(self.root))
        reply = run_hook(event, module.handle, module.EVENTS, env=env, raw=True, feature=module.FEATURE,
                         reducer=module.REDUCER)
        rows = [{k: v for k, v in row.items() if k != "ts"} for row in ledger.load(self.store)]
        return reply, rows

    def artifacts(self):
        folder = self.scratch / "ork-meter" / session.TOOL_OUTPUT_DIR
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


class FilteredOverBudget(Harness):
    def check_delivered(self, text, command):
        filtered, omitted = trimmer.filter_test_output(text)
        self.assertGreater(omitted, 0)
        self.assertGreater(trimmer.nbytes(filtered), BUDGET, "the filtered log must stay over budget")
        self.assertIsNone(trimmer.trim_text(filtered, BUDGET)[1], "and the trim must be unable to shorten it")
        # Before #200 nothing was delivered and nothing recorded.
        self.assertEqual(self.invoke(reference, text, command=command), ("", []))

        raw, rows = self.invoke(current, text, command=command)
        self.assertTrue(raw, "the filtered output is delivered")
        stdout = json.loads(raw)["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
        self.assertTrue(stdout.startswith(filtered + "\nFull output: "))
        for line in text.split("\n"):
            if trimmer.mandatory_line(line):
                self.assertIn(line, stdout.split("\n"))
        path = stdout.rsplit("Full output: ", 1)[1].rsplit(" (", 1)[0]
        self.assertEqual(Path(path).read_text(encoding="utf-8"), text)
        self.assertEqual(len(self.artifacts()), 2)
        self.assertEqual([(r["kind"], r["bytes_before"], r["bytes_after"]) for r in rows],
                         [("test_filter", trimmer.nbytes(text), trimmer.nbytes(stdout))])
        self.assertLess(trimmer.nbytes(stdout), trimmer.nbytes(text))
        return stdout

    def test_many_failure_pytest_log_is_delivered_filtered(self):
        stdout = self.check_delivered(pytest_log(400, 60), "pytest -v")
        self.assertNotIn("test_ok_0001 PASSED", stdout)
        self.assertEqual(stdout.count("E       assert "), 60)

    def test_many_failure_go_test_log_is_delivered_filtered(self):
        stdout = self.check_delivered(go_log(400, 150), "go test -v ./...")
        self.assertNotIn("--- PASS", stdout)
        self.assertEqual(stdout.count("--- FAIL: TestBad"), 150)

    def test_many_failure_jest_log_is_delivered_filtered(self):
        stdout = self.check_delivered(jest_log(400, 60), "npx jest")
        self.assertNotIn("✓", stdout)
        self.assertEqual(stdout.count("✕ case"), 60)

    def test_a_saving_smaller_than_the_note_still_passes_through(self):
        # Three passing lines are a filter saving below the note's size.
        text = pytest_log(3, 60)
        self.assertGreater(trimmer.nbytes(trimmer.filter_test_output(text)[0]), BUDGET)
        self.assertEqual(self.invoke(current, text), ("", []))
        self.assertEqual(self.artifacts(), [])

    def test_a_gate_refusal_still_passes_through_and_writes_nothing(self):
        with patch.object(token_gate, "permits_reply", return_value=False):
            self.assertEqual(self.invoke(current, pytest_log(400, 60)), ("", []))
        self.assertEqual(self.artifacts(), [])

    def test_a_late_reply_still_passes_through_and_writes_nothing(self):
        with patch.object(hook, "may_emit", return_value=False):
            self.assertEqual(self.invoke(current, pytest_log(400, 60)), ("", []))
        self.assertEqual(self.artifacts(), [])

    def test_with_the_test_filter_off_the_trim_alone_is_unchanged(self):
        env = {"ORK_METER_TEST_FILTER": "off"}
        before = self.invoke(reference, pytest_log(400, 60), env=env)
        self.assertTrue(before[0])
        self.assertEqual(self.invoke(current, pytest_log(400, 60), env=env), before)


class ChargeNote(unittest.TestCase):
    def test_the_final_stage_pays_when_it_can(self):
        rows = [ledger.test_filtered("Bash", 9000, 5000), ledger.trimmed("Bash", 5000, 3000)]
        self.assertEqual(current.charge_note(rows, 200),
                         [ledger.test_filtered("Bash", 9000, 5000), ledger.trimmed("Bash", 5000, 3200)])

    def test_an_earlier_stage_pays_and_the_chain_still_telescopes(self):
        rows = [ledger.test_filtered("Bash", 9000, 5000), ledger.trimmed("Bash", 5000, 4900)]
        charged = current.charge_note(rows, 200)
        self.assertEqual(charged, [ledger.test_filtered("Bash", 9000, 5200), ledger.trimmed("Bash", 5200, 5100)])
        self.assertTrue(all(r["bytes_after"] <= r["bytes_before"] for r in charged))
        self.assertEqual(sum(r["bytes_before"] - r["bytes_after"] for r in charged), 9000 - 4900 - 200)

    def test_no_stage_that_covers_the_note_is_no_reply(self):
        self.assertIsNone(current.charge_note([ledger.test_filtered("Bash", 9000, 8900)], 200))
        self.assertIsNone(current.charge_note([ledger.test_filtered("Bash", 9000, 8900),
                                               ledger.trimmed("Bash", 8900, 8800)], 200))


class Differential(Harness):
    """Every reply the hook gave before #200 is unchanged, byte for byte."""

    def corpus(self, seed=200):
        rand = random.Random(seed)
        logs = (pytest_log, go_log, jest_log)
        commands = {pytest_log: "pytest -v", go_log: "go test -v ./...", jest_log: "npx jest"}
        yield pytest_log(400, 60), "", "pytest -v", {}
        yield pytest_log(400, 2), "", "pytest -v", {}
        yield pytest_log(10, 0), "", "pytest -v", {}
        for _ in range(36):
            make = rand.choice(logs)
            text = make(rand.choice([0, 3, 20, 200, 600]), rand.choice([0, 1, 10, 60, 150]))
            stderr = rand.choice(["", "warning: deprecated\n" * rand.choice([1, 50, 400])])
            command = rand.choice([commands[make], commands[make], "cat run.log"])
            env = rand.choice([{}, {}, {"ORK_METER_TRIM_BUDGET_BASH": rand.choice(["4K", "32K"])}])
            yield text, stderr, command, env

    def test_replies_before_the_fix_are_byte_identical(self):
        replied = newly = 0
        for text, stderr, command, env in self.corpus():
            before = self.invoke(reference, text, stderr, command, env)
            after = self.invoke(current, text, stderr, command, env)
            if before[0]:
                replied += 1
                self.assertEqual(after, before)
            elif after[0]:
                newly += 1
        # The corpus exercises both: unchanged replies, and results only the fix delivers.
        self.assertGreater(replied, 5)
        self.assertGreater(newly, 0)


if __name__ == "__main__":
    unittest.main()
