"""Unknown structured/binary-like output stays intact through generic reducers."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import context, load  # noqa: E402
import ledger  # noqa: E402
import session  # noqa: E402
import trimmer  # noqa: E402


class Formats(unittest.TestCase):
    def test_structured_and_partial_payloads_are_not_line_logs(self):
        values = [json.dumps({"items": ["value_%04d" % i for i in range(4000)]}),
                  json.dumps(["value_%04d" % i for i in range(4000)]),
                  "\n".join(json.dumps({"value": i}) for i in range(4000)),
                  '{"partial": [' + "1," * 10000,
                  "<?xml version='1.0'?><root>" + "<row>value</row>" * 3000 + "</root>",
                  "<html>" + "<p>value</p>" * 3000 + "</html>",
                  "---\nitems:\n" + "  - value\n" * 4000,
                  '"' + "literal " * 4000 + '"', "1" * 20000,
                  "[NaN," + "1," * 10000 + "Infinity]", "[unrecognized payload " + "value " * 4000]
        for value in values:
            with self.subTest(prefix=value[:30]):
                output, info = trimmer.trim_text(value, 1024)
                self.assertTrue(output == value)
                self.assertIsNone(info)
                self.assertEqual(trimmer.filter_test_output(value), (value, 0))

    def test_binary_like_received_text_passes_through(self):
        for control in ("\x00", "\x01", "\x08", "\x1b", "\ud800"):
            value = ("line" + control + "data\n") * 4000
            self.assertEqual(trimmer.trim_text(value, 1024), (value, None))

    def test_bracketed_log_tags_and_ansi_still_reduce(self):
        for tag in ("build", "INFO", "DEBUG", "test"):
            with self.subTest(tag=tag):
                value = "\n".join("\x1b[32m[%s] item_%04d\x1b[0m" % (tag, i) for i in range(4000))
                output, info = trimmer.trim_text(value, 1024)
                self.assertIsNotNone(info)
                self.assertLess(len(output), len(value))
                self.assertIn("item_0000", output)
                self.assertIn("item_3999", output)

    def test_actual_hook_bypasses_structured_results_without_artifact_or_rows(self):
        module = load("hooks/trim-tool-output.py")
        value = json.dumps({"items": ["value_%04d" % i for i in range(4000)]})
        with tempfile.TemporaryDirectory() as temporary:
            for tool, response in (("Bash", {"stdout": value, "stderr": "fatal: retained", "interrupted": False}),
                                   ("Bash", {"stdout": "ordinary\n" * 4000, "stderr": value, "interrupted": False}),
                                   ("Grep", {"content": value}), ("WebFetch", {"result": value}),
                                   ("Bash", {"stdout": "go_test\x00data\n" * 4000, "stderr": ""})):
                with self.subTest(tool=tool, fields=list(response)):
                    payload = {"tool_name": tool, "tool_response": response, "tool_input": {"command": "go test"},
                               "scratchpad_dir": temporary}
                    self.assertIsNone(module.handle(context(payload, seconds=5)))
            store = session.Store.for_scratchpad(temporary)
            self.assertEqual(ledger.load(store), [])
            self.assertEqual(list(Path(store.tool_output_dir()).glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
