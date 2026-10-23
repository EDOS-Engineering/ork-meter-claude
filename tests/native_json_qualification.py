"""Installed-host nested JSON recovery, bounded refusal and exact evidence."""
from decimal import Decimal
import json
import os
from pathlib import Path
import shlex
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import host_retry  # noqa: E402
from native_host import Fixture, Host, latest_tool_results  # noqa: E402
from native_result_assertions import assert_host_suffix
sys.path.insert(0, str(ROOT / "hooks/lib"))
import artifacts
import json_selection
import ledger
import session


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
class NativeJSON(unittest.TestCase):
    def test_nested_numeric_filter_and_whole_value_budget_refusal_reach_model_exactly(self):
        with Host(self, "ork-native-json-", "Bash", inference=Fixture) as host:
            base, plugin, work, scratch = host.base, host.plugin, host.work, host.scratch
            value = {"diagnostics": [{"id": i, "state": "failed" if i % 2 else "passed", "detail": "evidence detail " * 16}
                                      for i in range(1000)], "amount": "PRECISE_NUMBER"}
            text = json.dumps(value).replace('"PRECISE_NUMBER"', '0.100000000000000000001')
            original = {"stdout": text, "stderr": "", "exitCode": 7, "interrupted": False}
            origin = session.Store.for_payload({"scratchpad_dir": str(base / "originals"), "session_id": "json-origin-" + base.name})
            path = artifacts.save(origin, "Bash", original, text)
            document = artifacts.load(origin, Path(path).stem)
            specs = [
                {"pointers": ["/amount"], "filter": {"pointer": "/diagnostics", "where": "/state", "equals": "failed", "start": 21, "limit": 50}},
                {"pointers": ["/diagnostics", "/amount"]},
            ]
            budgets = [18432, 1024]
            expected = [json_selection.serialized(json_selection.select(document, json.dumps(spec), budget))
                        for spec, budget in zip(specs, budgets)]
            self.assertGreater(len(expected[0]), 12 * 1024)
            self.assertLess(len(expected[0]), 20 * 1024)
            prefix = ('bash ' + shlex.quote(str(plugin / "hooks/py.sh").replace("\\", "/"))
                      + ' --script scripts/artifact.py --scratchpad ' + shlex.quote(str(base / "originals").replace("\\", "/"))
                      + ' --id ' + Path(path).stem)
            calls = [("Bash", {"command": prefix + ' --select-json ' + shlex.quote(json.dumps(spec)) + ' --budget ' + str(budget)})
                     for spec, budget in zip(specs, budgets)]
            Fixture.calls, Fixture.requests, Fixture.arrivals = calls, [], []
            # No ORK_METER_MODE: the plugin's default mode.
            host.run("Recover the selected JSON fixture evidence.", "--max-turns", "4",
                     env=host.env(None, ORK_METER_TRIM_BUDGET_BASH="1024"))
            host.assert_no_external_traffic()
            self.assertEqual(len(Fixture.requests), 3)
            for index, expected_raw in enumerate(expected):
                body = Fixture.requests[index + 1]
                self.assertFalse(any(tool["name"].startswith("mcp__") for tool in body["tools"]))
                results = latest_tool_results(body)
                self.assertEqual(len(results), 1)
                self.assertFalse(results[0].get("is_error", False), str(results[0])[:700])
                raw = results[0]["content"]
                if isinstance(raw, list):
                    raw = "".join(item.get("text", "") for item in raw)
                decoder = json.JSONDecoder(parse_float=Decimal)
                parsed, end = decoder.raw_decode(raw)
                self.assertEqual(parsed, json.loads(expected_raw, parse_float=Decimal))
                self.assertIn('0.100000000000000000001', raw)
                self.assertNotIn("Full output:", raw)
                assert_host_suffix(self, raw[end:], work)
            rows = ledger.load(origin)
            self.assertEqual([row["bytes_after"] for row in rows], list(map(len, expected)))
            self.assertEqual([row["kind"] for row in rows], ["recovery", "recovery"])
            self.assertEqual(artifacts.load(origin, Path(path).stem)["response"], original)
            self.assertEqual(list(scratch.rglob("tool-output/*.json")), [])


if __name__ == "__main__":
    unittest.main()
