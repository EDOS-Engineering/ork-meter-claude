"""Signed local accounting retains recovery costs and telescoping stages."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_* when this file runs on its own

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/lib"))
import ledger
import session


class NetAccounting(unittest.TestCase):
    def test_truncated_tail_remains_partial_in_a_new_reporting_process(self):
        with tempfile.TemporaryDirectory() as directory:
            store = session.Store.for_payload({"scratchpad_dir": directory})
            ledger.append(store, ledger.stamp(ledger.trimmed("Bash", 1000, 100), "test"))
            with open(store.ledger_path, "a") as stream:
                stream.write('{"kind":"recovery","bytes_after":1500')
            scan = ledger.scan(store)
            self.assertEqual(scan["status"], "partial")
            self.assertEqual(len(scan["rows"]), 1)
            result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/savings.py"),
                                     "--scratchpad", directory, "--no-measure", "--json"], capture_output=True, text=True)
            value = json.loads(result.stdout)
            self.assertEqual(value["accounting_status"], "partial")
            self.assertIsNone(value["estimated"]["totals"]["net_bytes_avoided"])
            self.assertEqual(value["estimated"]["totals"]["bytes_avoided"], 900)

    def test_unreadable_and_invalid_rows_expose_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            store = session.Store.for_scratchpad(directory)
            self.assertEqual(ledger.scan(store)["status"], "unavailable")
            session.write_private(store.ledger_path, '[]\n{"kind":"trim","bytes_before":true}\n')
            scan = ledger.scan(store)
            self.assertEqual(scan["status"], "partial")
            self.assertEqual(len(scan["issues"]), 2)
    def test_larger_recovery_makes_net_negative_while_gross_stays_positive(self):
        totals = ledger.summarise([ledger.trimmed("Bash", 1000, 100), ledger.recovered("Bash", 1500)])["totals"]
        self.assertEqual(totals["bytes_avoided"], 900)
        self.assertEqual(totals["est_tokens_avoided"], 225)
        self.assertEqual(totals["recovery_bytes"], 1500)
        self.assertEqual(totals["net_bytes_avoided"], -600)
        self.assertEqual(totals["est_net_tokens_avoided"], -150)

    def test_multiple_stages_telescope_and_expanding_stages_are_not_hidden(self):
        totals = ledger.summarise([ledger.test_filtered("Bash", 1000, 500), ledger.trimmed("Bash", 500, 100),
                                  ledger.recovered("Bash", 1500)])["totals"]
        self.assertEqual(totals["net_bytes_avoided"], -600)
        totals = ledger.summarise([ledger.trimmed("Bash", 1000, 500), ledger.trimmed("Bash", 500, 700)])["totals"]
        self.assertEqual(totals["bytes_avoided"], 500)
        self.assertEqual(totals["net_bytes_avoided"], 300)

    def test_fresh_reports_label_gross_and_suppress_net_on_accounting_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            store = session.Store.for_payload({"scratchpad_dir": directory})
            for row in (ledger.trimmed("Bash", 1000, 100), ledger.recovered("Bash", 1500)):
                ledger.append(store, ledger.stamp(row, "test"))
            command = [sys.executable, "-I", "-B", str(ROOT / "scripts/savings.py"), "--scratchpad", directory, "--no-measure"]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("gross bytes avoided", result.stdout)
            self.assertIn("-600 bytes", result.stdout)
            session.write_json(store.accounting_gap_path("missing"), {"event": "recovery"})
            result = subprocess.run(command + ["--json"], capture_output=True, text=True)
            report = json.loads(result.stdout)
            self.assertEqual(report["accounting_status"], "partial")
            self.assertIsNone(report["estimated"]["totals"]["net_bytes_avoided"])
            self.assertEqual(report["estimated"]["totals"]["bytes_avoided"], 900)


if __name__ == "__main__":
    unittest.main()
