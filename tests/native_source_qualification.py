"""Native source batch delivery and task-workspace binding; localhost only."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_host import Fixture, Host  # noqa: E402
from support import load  # noqa: E402
import host_retry  # noqa: E402

source = load("scripts/source.py")
source_context = load("scripts/source_context.py")


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "opt in to installed CLI + localhost inference")
@host_retry.retry_once
class NativeSource(unittest.TestCase):
    def test_exact_batches_use_task_workspaces_and_bypass_recursive_reduction(self):
        self.qualify()

    def test_related_definitions_and_tests_arrive_exactly_without_mcp_or_recursive_reduction(self):
        self.qualify(related=True)

    def qualify(self, related=False):
        with Host(self, "ork-native-source-", "Bash", inference=Fixture) as host:
            plugin, work, scratch = host.plugin, host.work, host.scratch
            (plugin / "units.py").write_text('raise RuntimeError("plugin decoy must not execute")\n')
            expected = []
            Fixture.calls = []
            for name, value in (("task-a", 1000), ("task-b", 2000)):
                task = work / name
                task.mkdir()
                raw = ('@decorator\r\ndef millis(value):\r\n' + '    # exact important context\r\n' * 440
                       + '    return value * %d\r\n' % value).encode()
                (task / "units.py").write_bytes(raw)
                (task / "tests.txt").write_bytes(('verify millis %d\nother evidence\n' % value).encode())
                requests = [{"id": "body", "path": "units.py", "action": "symbol", "symbol": "millis"},
                            {"id": "test", "path": "tests.txt", "action": "search", "query": "millis"},
                            {"id": "stale", "path": "units.py", "expected_hash": "0" * 64},
                            {"id": "escape", "path": "../../plugin/units.py"}]
                expected.append(source.batch(requests, task, budget=18 * 1024))
                command = ('bash ' + shlex.quote(str(plugin / "hooks/py.sh").replace("\\", "/"))
                           + ' --script scripts/source.py --workspace ' + shlex.quote(str(task).replace("\\", "/"))
                           + ' --budget 18432 --batch ' + shlex.quote(json.dumps(requests)))
                if related:
                    raw = ('def decorator(fn):\r\n    return fn\r\nRATE = %d\r\n' % value
                           + raw.decode().replace('return value * %d' % value, 'return value * RATE')).encode()
                    (task / "units.py").write_bytes(raw)
                    primary = 'from units import millis\n\ndef timeout(value):\n    return millis(value)\n'
                    test_source = 'from service import timeout\n\ndef test_timeout():\n    assert timeout(2) == %d\n' % (value * 2)
                    (task / "service.py").write_text(primary, encoding="utf-8")
                    (task / "tests_service.py").write_text(test_source, encoding="utf-8")
                    expected[-1] = source_context.related("service.py", "timeout", task, hops=2,
                                                          references=["tests_service.py"], budget=24 * 1024)
                    command = ('bash ' + shlex.quote(str(plugin / "hooks/py.sh").replace("\\", "/"))
                               + ' --script scripts/source.py service.py --workspace ' + shlex.quote(str(task).replace("\\", "/"))
                               + ' --related timeout --hops 2 --references tests_service.py --budget 24576')
                Fixture.calls.append(("Bash", {"command": command}))
            Fixture.requests, Fixture.arrivals = [], []
            host.run("Retrieve the two task-source batches.", "--max-turns", "4",
                     system="Retrieve exact fixture evidence.")
            host.assert_no_external_traffic()
            self.assertEqual(len(Fixture.requests), 3)
            for index, value in enumerate(expected):
                results = [entry for message in Fixture.requests[index + 1]["messages"]
                           if message.get("role") == "user" and isinstance(message.get("content"), list)
                           for entry in message["content"] if isinstance(entry, dict) and entry.get("type") == "tool_result"
                           and entry.get("tool_use_id") == "native_%d" % (index + 1)]
                self.assertEqual(len(results), 1)
                self.assertFalse(results[0].get("is_error", False))
                raw = results[0]["content"]
                if isinstance(raw, list):
                    raw = "".join(item.get("text", "") for item in raw)
                parsed, end = json.JSONDecoder().raw_decode(raw.lstrip())
                self.assertEqual(parsed, value, "host-visible evidence must match exact task source")
                self.assertGreater(len(raw.encode()), 12 * 1024)
                self.assertNotIn("Full output:", raw)
                if related:
                    self.assertTrue(parsed["complete"], parsed["issues"])
                    self.assertFalse(parsed["semantic_closure"])
                    self.assertTrue(all(row["status"] == "ok" for row in parsed["results"]))
                    exact = next(row for row in parsed["results"] if row["symbol"] == "millis")
                    file = work / ("task-a" if index == 0 else "task-b") / "units.py"
                    self.assertEqual(exact["sha256"], hashlib.sha256(file.read_bytes()).hexdigest())
                    self.assertEqual(exact["source"], "@decorator" + file.read_bytes().decode().split("@decorator", 1)[1])
                    self.assertTrue(any(row.get("source", "").startswith("def test_timeout():") for row in parsed["results"]))
                    self.assertFalse(any(str(tool.get("name", "")).startswith("mcp__") for tool in Fixture.requests[index + 1]["tools"]))
                else:
                    self.assertFalse(parsed["complete"])
                    self.assertEqual([row["status"] for row in parsed["results"]], ["ok", "ok", "error", "error"])
                self.assertNotIn("plugin decoy must not execute", raw)
            self.assertEqual(list(scratch.rglob("tool-output/*.json")), [])
            for path in scratch.rglob("savings.jsonl"):
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertFalse(any(row["kind"] in ("trim", "test_filter") for row in rows))


if __name__ == "__main__":
    unittest.main()
