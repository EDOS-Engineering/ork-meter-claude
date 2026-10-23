"""Installed-CLI WebFetch replacement against a localhost HTTPS page (#205).

WebFetch fetches the page, hands it to a secondary model request with the
call's prompt, and returns that model's text as ``result``. Both the fetch
(a throwaway-certificate HTTPS server on 127.0.0.1) and the secondary model
(the same localhost inference fixture) stay local; a refusing proxy
records any other request. The secondary answer is about 19.9 KiB, over the
16 KiB WebFetch budget and under the 20 KiB native-spill band, with a
diagnostic line in its middle.
"""
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import host_retry  # noqa: E402
from native_host import Host, assert_hooks_fail_open, call_sha, hook_names, result_text, text_after, tool_id  # noqa: E402
sys.path.insert(0, str(ROOT / "hooks/lib"))
import ledger  # noqa: E402

PAGE = ("<html><body>" + "".join("<p>page-line-%04d fetched evidence</p>\n" % i for i in range(400))
        + "</body></html>").encode("utf-8")


def answer(count):
    return "".join("summary_line_%04d fetched evidence from the localhost page\n" % i
                   + ("ERROR: webfetch-middle diagnostic\n" if i == 160 else "") for i in range(count))


ANSWER = answer(344)
# Over the 16 KiB budget too, but cutting it to 16 KiB saves fewer than the
# token gate's 512 tokens once the note is added: delivered unchanged.
SMALL_SAVING = answer(320)


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
class NativeWebFetch(unittest.TestCase):
    def test_webfetch_replacement_reaches_model_and_off_mode_is_exact(self):
        self.assertGreater(len(ANSWER.encode()), 16 * 1024)
        self.assertLess(len(ANSWER.encode()), 20 * 1024)
        reduced = self.qualify("reduce")
        unchanged = self.qualify("off")
        self.assertLess(len(reduced), len(unchanged))

    def test_webfetch_result_below_the_token_gate_is_delivered_unchanged(self):
        self.assertGreater(len(SMALL_SAVING.encode()), 16 * 1024)
        self.qualify("reduce", SMALL_SAVING, expect_reduced=False)

    def qualify(self, mode, text=ANSWER, expect_reduced=True):
        with Host(self, "ork-native-webfetch-", "WebFetch") as host:
            url = host.serve_https_page(PAGE)
            secondary = []

            def script(body, number):
                if "WebFetch" not in [tool.get("name") for tool in body.get("tools", [])]:
                    secondary.append(body)
                    return [("text", text)]
                if number == 1:
                    return [("tool", "WebFetch", {"url": url, "prompt": "Return the qualification evidence."})]
                return [("text", "QUALIFIED")]
            host.script = script
            completed = host.run("Fetch the local page.", env=host.env(mode))
            host.assert_no_external_traffic()
            assert_hooks_fail_open(self, completed.stdout)
            self.assertEqual(host.pages, ["/qualification/page.html"], "the CLI must fetch the localhost page once")
            self.assertEqual(len(secondary), 1)
            self.assertIn("page-line-0399 fetched evidence", json.dumps(secondary[0]),
                          "the fetched page must reach WebFetch's secondary model request")
            self.assertIn("PostToolUse:WebFetch", hook_names(completed.stdout))
            final = host.requests[-1]
            identifier = tool_id(1, 0)
            delivered = [result_text(block) for message in final["messages"] if isinstance(message.get("content"), list)
                         for block in message["content"]
                         if isinstance(block, dict) and block.get("tool_use_id") == identifier]
            self.assertEqual(len(delivered), 1)
            visible = delivered[0]
            rows = [row for store in host.stores().values() for row in ledger.load(store)]
            if mode == "off" or not expect_reduced:
                self.assertEqual(visible.strip(), text.strip(), "the host's result must arrive exactly")
                self.assertNotIn("Ork Meter", text_after(final, identifier))
                self.assertEqual(rows, [])
                return visible
            for kept in ("summary_line_0000", "summary_line_0343", "ERROR: webfetch-middle diagnostic"):
                self.assertIn(kept, visible)
            self.assertNotIn("summary_line_0200 ", visible)
            self.assertIn("[Ork Meter trimmed ", visible)
            marker = visible.rsplit("Full output: ", 1)[1].split(" (", 1)[0]
            self.assertEqual(Path(marker).read_text(encoding="utf-8"), text,
                             "the full-output file must hold the host's whole result")
            self.assertEqual([(row["kind"], row["tool"], row.get("tool_call_sha256")) for row in rows],
                             [("trim", "WebFetch", call_sha(identifier))])
            self.assertEqual(rows[0]["bytes_before"], len(text.encode()))
            return visible


if __name__ == "__main__":
    unittest.main()
