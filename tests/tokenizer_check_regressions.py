"""Session-start tokenizer check (hooks/lib/tokenizer_check.py, #115) and the
promise behind it: without tiktoken the pure-Python encoder counts exactly and
nothing is reported; without any exact encoder only a replacement the
byte-count gate proves is delivered, and its ledger row holds byte counts."""
import importlib.metadata
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_* when this file runs on its own

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/lib"))
sys.path.insert(0, str(ROOT / "tests"))
import entitled
import hook
import ledger
import session
import token_gate
import tokenizer_check

try:
    token_gate.count("ork")
    HAVE_TOKENIZER = True
except Exception:
    HAVE_TOKENIZER = False


def missing(name):
    raise importlib.metadata.PackageNotFoundError(name)


class ProblemTests(unittest.TestCase):
    """The reasons apply when no exact encoder loads; tiktoken's absence alone
    is no problem while pure_bpe works."""

    def setUp(self):
        patcher = patch.object(token_gate, "available", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_names_a_missing_tiktoken(self):
        with patch.object(importlib.metadata, "version", side_effect=missing):
            self.assertEqual(tokenizer_check.problem(), "has no tiktoken")

    def test_names_the_wrong_tiktoken_version(self):
        with patch.object(importlib.metadata, "version", return_value="0.9.0"):
            self.assertEqual(tokenizer_check.problem(), "has tiktoken 0.9.0, not 0.14.0")

    def test_names_a_missing_regex(self):
        with patch.object(importlib.metadata, "version", return_value=token_gate.VERSION), \
                patch.dict(sys.modules, {"regex": None}):
            self.assertEqual(tokenizer_check.problem(), "has no regex")

    def test_a_tokenizer_that_fails_to_load_is_reported(self):
        with patch.object(importlib.metadata, "version", return_value=token_gate.VERSION), \
                patch.dict(sys.modules, {"regex": sys.modules.get("regex") or object()}), \
                patch.object(token_gate, "count", side_effect=ValueError("token vocabulary integrity check failed")):
            self.assertEqual(tokenizer_check.problem(), "cannot load the offline tokenizer")



class ExactEncoderProblemTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_TOKENIZER, "the offline token gate needs tiktoken in this interpreter")
    def test_a_working_tokenizer_is_no_problem(self):
        self.assertIsNone(tokenizer_check.problem())

    def test_missing_tiktoken_is_no_problem_while_pure_bpe_works(self):
        token_gate.encoding.cache_clear()
        try:
            with patch.object(importlib.metadata, "version", side_effect=missing), \
                    patch.dict(sys.modules, {"tiktoken": None}):
                self.assertIsNone(tokenizer_check.problem())
        finally:
            token_gate.encoding.cache_clear()

    def test_missing_tiktoken_is_reported_when_pure_bpe_fails_too(self):
        token_gate.encoding.cache_clear()
        try:
            with patch.object(importlib.metadata, "version", side_effect=missing), \
                    patch.dict(sys.modules, {"tiktoken": None, "pure_bpe": None}):
                self.assertEqual(tokenizer_check.problem(), "has no tiktoken")
        finally:
            token_gate.encoding.cache_clear()


class MessageTests(unittest.TestCase):
    requirements = str(ROOT / "requirements-hooks.txt")

    def test_suggests_a_private_venv_under_the_plugin_data(self):
        text = tokenizer_check.message("has no tiktoken", "/usr/bin/python3",
                                       {"CLAUDE_PLUGIN_DATA": "/data/ork"}, platform="darwin", prefix="/usr")
        self.assertIn("Ork Meter: tokenizer missing: the hooks' Python", text)
        self.assertIn("so trimming uses a conservative byte-count gate", text)
        self.assertIn("For full savings, install the tokenizer: run ", text)
        self.assertNotIn("saves nothing", text)
        self.assertIn("(/usr/bin/python3, ", text)
        self.assertIn("has no tiktoken", text)
        self.assertIn('"/usr/bin/python3" -m venv --clear "%s"' % os.path.join("/data/ork", "hook-python"), text)
        self.assertIn('"%s" -m pip install --require-hashes --only-binary=:all: -r "%s"'
                      % (os.path.join("/data/ork", "hook-python", "bin", "python3"), self.requirements), text)
        self.assertIn("restart Claude Code", text)

    def test_windows_venv_layout(self):
        data = "C:\\Users\\me\\ork"
        text = tokenizer_check.message("has no tiktoken", "C:\\Python312\\python.exe",
                                       {"CLAUDE_PLUGIN_DATA": data}, platform="win32", prefix="C:\\Python312")
        self.assertIn(os.path.join(data, "hook-python", "Scripts", "python.exe"), text)
        self.assertNotIn(os.path.join("hook-python", "bin"), text)

    def test_inside_the_private_venv_only_pip_is_needed(self):
        with tempfile.TemporaryDirectory() as data:
            venv = os.path.join(data, "hook-python")
            os.makedirs(venv)
            text = tokenizer_check.message("has no tiktoken", os.path.join(venv, "bin", "python3"),
                                           {"CLAUDE_PLUGIN_DATA": data}, platform="linux", prefix=venv)
        self.assertNotIn("-m venv", text)
        self.assertIn("-m pip install --require-hashes --only-binary=:all: -r", text)

    def test_without_plugin_data_install_into_the_hooks_python(self):
        for env in ({}, {"CLAUDE_PLUGIN_DATA": "relative/dir"}):
            text = tokenizer_check.message("has no tiktoken", "/opt/homebrew/bin/python3", env,
                                           platform="darwin", prefix="/opt/homebrew")
            self.assertIn('"/opt/homebrew/bin/python3" -m pip install --require-hashes --only-binary=:all: -r "%s"'
                          % self.requirements, text)
            self.assertIn("not --user", text)
            self.assertNotIn("-m venv", text)

    def test_drive_paths_are_absolute(self):
        for data in ("C:\\x", "c:/x", "/x"):
            self.assertTrue(tokenizer_check.is_absolute(data, "win32"), data)
        for data in ("", "x", "C:x", "C:"):
            self.assertFalse(tokenizer_check.is_absolute(data, "win32"), data)
        # Elsewhere a drive-like name is relative: the cwd must not pick the interpreter.
        for data in ("C:\\x", "c:/x"):
            self.assertFalse(tokenizer_check.is_absolute(data, "darwin"), data)
        self.assertTrue(tokenizer_check.is_absolute("/x", "linux"))


class MainTests(unittest.TestCase):
    def run_main(self, env, reason):
        sink = io.StringIO()
        with patch.object(tokenizer_check, "problem", return_value=reason):
            tokenizer_check.main(env, sink)
        return sink.getvalue()

    def test_entitled_without_tokenizer_prints_one_system_message(self):
        with tempfile.TemporaryDirectory() as data:
            out = self.run_main({"CLAUDE_PLUGIN_DATA": entitled.write(data)}, "has no tiktoken")
        self.assertEqual(out.count("\n"), 1)
        value = json.loads(out)
        self.assertEqual(list(value), ["systemMessage"])
        self.assertIn("has no tiktoken", value["systemMessage"])

    def test_silent_when_the_tokenizer_works(self):
        with tempfile.TemporaryDirectory() as data:
            self.assertEqual(self.run_main({"CLAUDE_PLUGIN_DATA": entitled.write(data)}, None), "")

    def test_silent_without_plus_because_every_lever_is_off(self):
        with tempfile.TemporaryDirectory() as data:
            self.assertEqual(self.run_main({"CLAUDE_PLUGIN_DATA": data}, "has no tiktoken"), "")
            free = entitled.write(data, optimize=False, state="free", until=None)
            self.assertEqual(self.run_main({"CLAUDE_PLUGIN_DATA": free}, "has no tiktoken"), "")
        self.assertEqual(self.run_main({}, "has no tiktoken"), "")


    def test_silent_when_the_person_turned_reduction_off(self):
        # Review of #161: an entitled user who switched trimming off on purpose
        # must not be told every startup that the tokenizer is missing.
        with tempfile.TemporaryDirectory() as data:
            for switch in ({"ORK_METER_MODE": "off"}, {"ORK_METER_TRIM": "off"}):
                env = dict(switch, CLAUDE_PLUGIN_DATA=entitled.write(data))
                self.assertEqual(self.run_main(env, "has no tiktoken"), "", switch)

class ByteCountGateDeliveryTests(unittest.TestCase):
    """#115: without tiktoken, only a byte-proven trim is delivered and credited,
    and its ledger row is byte counts with the ceil(bytes/4) estimate, never a
    tokenizer count."""

    def deliver(self, scratch, stdout, replacement="short"):
        payload = {"hook_event_name": "PostToolUse", "session_id": "no-tokenizer", "scratchpad_dir": scratch,
                   "tool_name": "Bash", "tool_response": {"stdout": stdout}}

        def handler(ctx):
            ctx.record(ledger.trimmed("Bash", len(stdout), len(replacement)))
            return hook.updated_tool_output("PostToolUse", {"stdout": replacement})
        sink = io.StringIO()
        token_gate._last_verdict = None
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", sink):
            hook.run(("PostToolUse",), handler)
        return sink.getvalue(), ledger.load(session.Store.for_scratchpad(scratch))

    def without(self, stdout, *modules):
        token_gate.encoding.cache_clear()
        try:
            with tempfile.TemporaryDirectory() as d, patch.dict(sys.modules, dict.fromkeys(modules)):
                return self.deliver(d, stdout)
        finally:
            token_gate.encoding.cache_clear()

    def without_tiktoken(self, stdout):
        """Neither tiktoken nor pure_bpe: the byte-count gate decides."""
        return self.without(stdout, "tiktoken", "pure_bpe")

    def test_without_tiktoken_pure_bpe_delivers_the_borderline_trim(self):
        out, rows = self.without("data " * 4000, "tiktoken")
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["updatedToolOutput"], {"stdout": "short"})
        self.assertEqual(len(rows), 1)

    def test_borderline_trim_without_tiktoken_replaces_nothing_and_credits_nothing(self):
        # 20,000 bytes: thousands of tokens saved by any real count, but only
        # ceil(20,016 / 128) = 157 proven, short of the 512-token reserve.
        out, rows = self.without_tiktoken("data " * 4000)
        self.assertEqual(out, "")
        self.assertEqual(rows, [])

    def test_large_trim_without_tiktoken_is_delivered_with_a_byte_count_row(self):
        stdout = "data " * 200000
        out, rows = self.without_tiktoken(stdout)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["updatedToolOutput"], {"stdout": "short"})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["bytes_before"], row["bytes_after"]), (len(stdout), len("short")))
        self.assertEqual(row["est_tokens_avoided"], ledger.est_tokens(len(stdout)) - ledger.est_tokens(5))
        self.assertFalse(any("tokenizer" in key or key.startswith("original_tokens") for key in row))

    @unittest.skipUnless(HAVE_TOKENIZER, "the offline token gate needs tiktoken in this interpreter")
    def test_control_with_the_tokenizer_the_ledger_row_is_identical(self):
        stdout = "data " * 200000
        with tempfile.TemporaryDirectory() as d:
            out, rows = self.deliver(d, stdout)
        self.assertTrue(out)
        _, fallback_rows = self.without_tiktoken(stdout)
        strip = lambda r: {k: v for k, v in r.items() if k not in ("ts",)}
        self.assertEqual([strip(r) for r in rows], [strip(r) for r in fallback_rows])

    @unittest.skipUnless(HAVE_TOKENIZER, "the offline token gate needs tiktoken in this interpreter")
    def test_control_with_the_tokenizer_the_borderline_trim_is_delivered(self):
        with tempfile.TemporaryDirectory() as d:
            out, rows = self.deliver(d, "data " * 4000)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["updatedToolOutput"], {"stdout": "short"})
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
