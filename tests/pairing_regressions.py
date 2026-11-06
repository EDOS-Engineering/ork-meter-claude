"""scripts/pairing.py: connect, and finish a connection from a browser on another computer.

The fake below answers as the runtime's loopback setup server does
(ork-meter internal/pluginruntime/service.go at 44a245f, the commit that
added the ``setup_pending`` answer, #571): ``GET /setup/<nonce>``, ``POST
/prepare/<nonce>`` and ``POST /plugin/claim/<nonce>``, each POST refused
(403) unless it carries exactly the runtime's own Origin, a urlencoded body
and exactly its two fields. Its pages are tests/fixtures/runtime-pages/*.html,
rendered by Go's html/template from that commit's ``page`` template
(``setupPage`` values: the plan form; an approval for proposal ``pl_abc123``
returning to ``http://127.0.0.1:43123/plugin/claim/<64 a>``; Connected; a
refused claim with localpairing.ErrClaimCode's message), so the markup and
its escaping are the runtime's own.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, LocalHTTPServer, load  # noqa: E402

PAGES = ROOT / "tests/fixtures/runtime-pages"
NONCE = "a" * 64
FIXTURE_PORT = "43123"
CODE = "7K3M-9QPX"
REFERENCE = "pl_abc123"


class FakeRuntime(BaseHTTPRequestHandler):
    """The runtime's loopback setup server, as far as pairing.py uses it."""
    connected = False
    requests = []

    def log_message(self, *args):
        pass

    def page(self, name):
        port = str(self.server.server_port)
        body = (PAGES / (name + ".html")).read_text(encoding="utf-8")
        body = body.replace("%3A" + FIXTURE_PORT + "%2F", "%3A" + port + "%2F").replace("/prepare/NONCE", "/prepare/" + NONCE)
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def refuse(self):
        self.send_error(403, "request refused")

    def do_GET(self):
        self.requests.append(("GET", self.path, dict(self.headers), None))
        if self.path != "/setup/" + NONCE:
            return self.refuse()
        self.page("connected" if self.connected else "setup")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("ascii"), keep_blank_values=True)
        self.requests.append(("POST", self.path, dict(self.headers), form))
        local = "http://127.0.0.1:%d" % self.server.server_port
        if (self.headers.get_all("Origin") != [local]
                or self.headers.get("Content-Type") != "application/x-www-form-urlencoded"):
            return self.refuse()
        if self.path == "/prepare/" + NONCE and sorted(form) == ["permitted", "plan"]:
            return self.page("connected" if self.connected else "approve")
        if self.path == "/plugin/claim/" + NONCE and sorted(form) == ["claim_code", "reference"]:
            if form["reference"] != [REFERENCE]:
                return self.refuse()
            if form["claim_code"] == [CODE]:
                type(self).connected = True
                return self.page("connected")
            return self.page("failed")
        self.refuse()


class Pairing(unittest.TestCase):
    def setUp(self):
        self.pairing = load("scripts/pairing.py")
        FakeRuntime.connected, FakeRuntime.requests = False, []
        self.server = LocalHTTPServer(("127.0.0.1", 0), FakeRuntime)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.setup_url = "http://127.0.0.1:%d/setup/%s" % (self.server.server_port, NONCE)
        # A proxy in the environment must never be used for the loopback.
        proxies = patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
                                          "NO_PROXY": "", "no_proxy": ""})
        proxies.start()
        self.addCleanup(proxies.stop)

    def run_script(self, *argv):
        out = []
        with patch.object(self.pairing.sys, "stdout") as stdout:
            stdout.write.side_effect = out.append
            code = self.pairing.main(["--plugin-data", str(self.data), *argv])
        return code, json.loads("".join(out))

    def runtime_answer(self, answer, returncode=0, stderr=""):
        stdout = answer if isinstance(answer, str) else json.dumps(answer) + "\n"
        return patch.object(self.pairing.subprocess, "run",
                            return_value=subprocess.CompletedProcess([], returncode, stdout, stderr))

    def connect_pending(self):
        message = ("No browser could be opened on this machine, so the setup page is waiting at %s on this machine's "
                   "loopback for 30 minutes. ..." % self.setup_url)
        with self.runtime_answer({"state": "setup_pending", "message": message}) as run:
            code, answer = self.run_script("connect", "--environment", "production")
        self.assertEqual(code, 0)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["bash", str(ROOT / "scripts/runtime.sh")])
        self.assertEqual(command[2:], ["--plugin-data", str(self.data), "connect", "--environment", "production"])
        return answer

    # --- connect -------------------------------------------------------------

    def test_connect_passes_a_browser_setup_through_and_keeps_nothing(self):
        answer = {"state": "setup_opened", "message": "Complete sign-in and consent in your browser."}
        with self.runtime_answer(answer):
            code, printed = self.run_script("connect", "--environment", "dev")
        self.assertEqual((code, printed), (0, answer))
        self.assertFalse((self.data / "remote-pairing.json").exists())

    def test_connect_passes_a_failure_through_unchanged(self):
        with self.runtime_answer("", returncode=1, stderr="The setup browser could not be opened\n"), \
                patch.object(self.pairing.sys, "stderr") as stderr, patch.object(self.pairing.sys, "stdout") as stdout:
            code = self.pairing.main(["--plugin-data", str(self.data), "connect"])
        self.assertEqual(code, 1)
        stderr.write.assert_called_once_with("The setup browser could not be opened\n")
        stdout.write.assert_called_once_with("")

    def test_connect_without_a_browser_offers_the_terminal_approval(self):
        answer = self.connect_pending()
        self.assertEqual(answer["state"], "setup_pending")
        self.assertEqual(answer["terminal_approval"], "available")
        path = self.data / "remote-pairing.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(record), ["created_at", "setup_url", "v"])
        self.assertEqual(record["setup_url"], self.setup_url)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_new_connect_replaces_the_terminal_setup(self):
        self.connect_pending()
        with self.runtime_answer({"state": "setup_opened", "message": "browser"}):
            self.run_script("connect")
        self.assertFalse((self.data / "remote-pairing.json").exists())

    # --- prepare -------------------------------------------------------------

    def test_prepare_shows_the_setup_pages_plans_and_permission_word_for_word(self):
        self.connect_pending()
        code, answer = self.run_script("prepare")
        self.assertEqual(code, 0)
        self.assertEqual(answer["state"], "consent_required")
        self.assertEqual(answer["plans"], ["pro-max", "team-enterprise", "api"])
        self.assertEqual(answer["permission"], "I am permitted to export my Claude Code telemetry to Ork Meter, and my "
                                               "organisation's policy (if any) allows it.")
        self.assertTrue(answer["destination"].startswith("Destination: https://orkmeter.com."))
        self.assertEqual([r[0] for r in FakeRuntime.requests], ["GET"])

    def test_prepare_needs_the_persons_permission(self):
        self.connect_pending()
        with self.assertRaises(SystemExit), patch.object(self.pairing.sys, "stderr"):
            self.pairing.main(["--plugin-data", str(self.data), "prepare", "--plan", "api", "--permitted", "no"])
        code, answer = self.run_script("prepare", "--plan", "api")
        self.assertEqual((code, answer["state"]), (1, "consent_required"))
        self.assertEqual(FakeRuntime.requests, [], "nothing is submitted without the permission")

    def test_prepare_submits_the_form_as_the_page_would_and_returns_the_approval_link(self):
        self.connect_pending()
        code, answer = self.run_script("prepare", "--plan", "team-enterprise", "--permitted", "yes")
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["state"], "approval_pending")
        self.assertEqual(answer["fingerprint"], "0123456789abcdef" * 4)
        self.assertTrue(answer["approval_url"].startswith("https://orkmeter.com/pair#proposal=%7B%22id%22%3A%22pl_abc123"))
        self.assertNotIn("&amp;", answer["approval_url"])
        self.assertIn("/ork-meter:claim", answer["next_step"])
        method, path, headers, form = FakeRuntime.requests[-1]
        self.assertEqual((method, path), ("POST", "/prepare/" + NONCE))
        self.assertEqual(form, {"plan": ["team-enterprise"], "permitted": ["yes"]})
        record = json.loads((self.data / "remote-pairing.json").read_text(encoding="utf-8"))
        self.assertEqual(record["reference"], REFERENCE)
        self.assertEqual(record["claim_url"], "http://127.0.0.1:%d/plugin/claim/%s" % (self.server.server_port, NONCE))

    def test_prepare_on_a_finished_setup_says_connected(self):
        self.connect_pending()
        FakeRuntime.connected = True
        code, answer = self.run_script("prepare")
        self.assertEqual((code, answer["state"]), (0, "connected"))
        self.assertFalse((self.data / "remote-pairing.json").exists())

    def test_prepare_without_a_terminal_setup_points_to_connect(self):
        code, answer = self.run_script("prepare")
        self.assertEqual((code, answer["state"]), (1, "no_terminal_setup"))
        self.assertIn("/ork-meter:connect", answer["message"])

    def test_an_expired_setup_is_dropped(self):
        self.connect_pending()
        path = self.data / "remote-pairing.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["created_at"] = int(time.time()) - 31 * 60
        path.write_text(json.dumps(record), encoding="utf-8")
        code, answer = self.run_script("prepare")
        self.assertEqual((code, answer["state"]), (1, "setup_expired"))
        self.assertFalse(path.exists())
        self.assertEqual(FakeRuntime.requests, [])

    def test_a_setup_that_stopped_answering_says_so(self):
        self.connect_pending()
        self.server.shutdown()
        self.server.server_close()
        code, answer = self.run_script("prepare")
        self.assertEqual((code, answer["state"]), (1, "setup_unavailable"))

    # --- claim ---------------------------------------------------------------

    def approved(self):
        self.connect_pending()
        self.assertEqual(self.run_script("prepare", "--plan", "pro-max", "--permitted", "yes")[0], 0)

    def test_claim_with_the_code_connects(self):
        self.approved()
        code, answer = self.run_script("claim", "--code", " 7k3m9qpx ")
        self.assertEqual((code, answer["state"]), (0, "connected"), answer)
        self.assertIn("Restart Claude Code", answer["message"])
        method, path, headers, form = FakeRuntime.requests[-1]
        self.assertEqual((method, path), ("POST", "/plugin/claim/" + NONCE))
        self.assertEqual(form, {"reference": [REFERENCE], "claim_code": [CODE]})
        self.assertFalse((self.data / "remote-pairing.json").exists())
        self.assertNotIn("7K3M", json.dumps(answer))

    def test_claim_reads_o_as_zero_and_i_or_l_as_one_as_the_platform_does(self):
        self.assertEqual(self.pairing.canonical_code("o1il-abcd"), "0111-ABCD")
        for bad in ("", "7K3M-9QP", "7K3M--9QPX", "7K3M 9QPX", "7K3U-9QPX", "7K3M-9QPX0", "../../etc"):
            with self.subTest(code=bad):
                self.assertIsNone(self.pairing.canonical_code(bad))

    def test_a_malformed_code_sends_nothing(self):
        self.approved()
        before = len(FakeRuntime.requests)
        code, answer = self.run_script("claim", "--code", "not-a-code")
        self.assertEqual((code, answer["state"]), (1, "invalid_code"))
        self.assertEqual(len(FakeRuntime.requests), before)

    def test_a_refused_code_reports_the_runtimes_reason_and_keeps_the_setup(self):
        self.approved()
        code, answer = self.run_script("claim", "--code", "ABCD-EFGH")
        self.assertEqual((code, answer["state"]), (0, "not_completed"))
        self.assertIn("XXXX-XXXX", answer["message"])
        self.assertTrue((self.data / "remote-pairing.json").exists(), "the person can try the right code")

    def test_claim_before_prepare_points_to_connect(self):
        self.connect_pending()
        code, answer = self.run_script("claim", "--code", CODE)
        self.assertEqual((code, answer["state"]), (1, "approval_not_requested"))
        self.assertEqual(FakeRuntime.requests, [])

    def test_a_record_naming_anything_but_the_loopback_is_refused(self):
        self.data.mkdir(parents=True, exist_ok=True)
        for url in ("http://example.com:80/setup/" + NONCE, "http://127.0.0.1:1/setup/short", "file:///etc/passwd"):
            with self.subTest(url=url):
                (self.data / "remote-pairing.json").write_text(json.dumps(
                    {"v": 1, "setup_url": url, "created_at": int(time.time())}), encoding="utf-8")
                code, answer = self.run_script("prepare")
                self.assertEqual((code, answer["state"]), (1, "no_terminal_setup"))

    def test_a_claim_address_off_the_loopback_is_never_requested(self):
        self.approved()
        path = self.data / "remote-pairing.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["claim_url"] = "http://192.0.2.1:%d/plugin/claim/%s" % (self.server.server_port, NONCE)
        path.write_text(json.dumps(record), encoding="utf-8")
        with patch.object(self.pairing, "opener", side_effect=AssertionError("no request may be made")):
            code, answer = self.run_script("claim", "--code", CODE)
        self.assertEqual((code, answer["state"]), (1, "setup_unavailable"))


if __name__ == "__main__":
    unittest.main()
