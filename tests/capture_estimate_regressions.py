"""Benchmark B1 findings for the capture path (#212).

1. The test filter's note is complete in itself when only passing-test lines
   went: it does not invite recovery (two of four B1 noisy-tests runs spent a
   request grepping the saved file to re-confirm counts already shown), yet the
   full output stays saved and named, and recovery still works.
2. A capture whose output feeds the caller's own pipeline (``... -- pytest |
   tail -30``, ``| grep -c PASSED``) passes through unreduced and records no
   trim: reducing it would change what that filter answers, and the model sees
   only the filter's output (B1 credited 5,919 per trim; about 55 were real).
3. The short, stable capture form needs no per-run path: the session comes from
   ``CLAUDE_CODE_SESSION_ID`` and the data directory from ``CLAUDE_PLUGIN_DATA``,
   and the form is still recognised as a capture command, with the same
   refusals as before.
"""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, StoreCase, load, payload, run_hook, run_like_bash_tool  # noqa: E402
import entitled  # noqa: E402
entitled.install()
import ledger  # noqa: E402
import recovery  # noqa: E402
import session  # noqa: E402
import trimmer  # noqa: E402


def noisy_pytest(failing=True):
    """B1's noisy-tests shape: 300 parameterized passes, one skip, one result."""
    lines = ["============================= test session starts ==============================",
             "collecting ... collected 302 items", ""]
    lines += ["tests/test_slug.py::test_stable[%d] PASSED [%3d%%]" % (i, (i + 1) * 100 // 302) for i in range(300)]
    lines.append("tests/test_slug.py::test_critical_security_check SKIPPED (needs cred...) [ 99%]")
    if failing:
        lines += ["tests/test_slug.py::test_repeated_whitespace FAILED [100%]", "",
                  "=================================== FAILURES ===================================",
                  "E       assert 'a--b' == 'a-b'",
                  "=========================== short test summary info ============================",
                  "FAILED tests/test_slug.py::test_repeated_whitespace - AssertionError",
                  "=================== 1 failed, 300 passed, 1 skipped in 0.09s ==================="]
    else:
        lines += ["tests/test_slug.py::test_repeated_whitespace PASSED [100%]", "",
                  "======================== 301 passed, 1 skipped in 0.08s ========================"]
    return "\n".join(lines) + "\n"


class FilterNote(unittest.TestCase):
    def test_note_does_not_invite_recovery_when_only_passing_lines_went(self):
        for failing in (True, False):
            text = noisy_pytest(failing)
            filtered, omitted = trimmer.filter_test_output(text)
            self.assertEqual(omitted, 300)
            note = filtered.rstrip("\n").rsplit("\n", 1)[1]
            self.assertEqual(note, "… [Ork Meter test filter: 300 passing-test lines omitted; "
                                   "every other line is shown, including the summary]")
            self.assertNotIn("recover", filtered.lower())
            # Every line that is not a passing-test line is still there.
            for line in text.split("\n"):
                if line.strip() and not trimmer.PASS_LINE_RE.match(line):
                    self.assertIn(line, filtered.split("\n"))

    def test_note_claims_only_what_holds(self):
        passes = "".join("tests/test_x.py::test_%d PASSED\n" % i for i in range(20))
        note = lambda text: trimmer.filter_test_output(text)[0].rstrip("\n").rsplit("\n", 1)[1]
        head = "… [Ork Meter test filter: 20 passing-test lines omitted; "
        cases = {
            "collected 21 items\n" + passes + "tests/test_x.py::test_bad FAILED\n": "every other line is shown]",
            "collected 21 items\n" + passes + "== 1 failed, 20 passed in 0.1s ==\n": "every other line is shown, including the summary]",
            "\x1b[1mcollected 21 items\x1b[0m\n" + passes + "FAILED x\n": "every other line is shown (colour codes removed)]",
            "collected 21 items\n\n\n" + passes + "FAILED x\n": "every other line is shown (repeated blank lines collapsed)]",
            "\x1b[31mcollected\x1b[0m\n\n\n" + passes + "\x1b[1m1 failed\x1b[0m\n":
                "every other line is shown (colour codes removed, repeated blank lines collapsed), including the summary]",
        }
        for text, tail in cases.items():
            with self.subTest(text=text[:40]):
                self.assertEqual(note(text), head + tail)
                self.assertNotIn("recover", note(text))

    def test_a_later_trim_withdraws_the_all_lines_shown_claim(self):
        text = noisy_pytest() + "".join("ordinary context line %05d\n" % i for i in range(4000))
        filtered, omitted = trimmer.filter_test_output(text)
        trimmed, info = trimmer.trim_text(filtered, 4096)
        self.assertIsNotNone(info)
        self.assertIn("every other line is shown", trimmed)
        swapped = trimmer.after_trim(trimmed)
        self.assertNotIn("every other line", swapped)
        self.assertIn("%d passing-test lines omitted; other lines trimmed where marked]" % omitted, swapped)
        # Unchanged when there is no filter note to swap.
        self.assertEqual(trimmer.after_trim("plain"), "plain")

    def test_hook_note_has_no_recover_wording_and_names_the_saved_output(self):
        current = load("hooks/trim-tool-output.py")
        text = noisy_pytest(failing=False) * 3  # over the 12 KiB Bash budget, 900 passes
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "scratch"
            event = payload(scratch, "Bash", {"command": "pytest -v"},
                            {"stdout": text, "stderr": "", "interrupted": False, "isImage": False},
                            hook_event_name="PostToolUse", session_id="b1-note", tool_use_id="call", cwd=directory)
            reply = run_hook(event, current.handle, current.EVENTS, feature=current.FEATURE, reducer=current.REDUCER)
            stdout = reply["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
            self.assertIn("900 passing-test lines omitted; every other line is shown", stdout)
            self.assertNotIn("recover", stdout.lower())
            path = stdout.rsplit("Full output: ", 1)[1].rsplit(" (", 1)[0]
            self.assertEqual(Path(path).read_text(encoding="utf-8"), text)


class Capture(StoreCase):
    def setUp(self):
        super().setUp()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("ORK_METER_", "CLAUDE_CODE_"))}
        self.env["ORK_METER_TEST_FILTER"] = "on"
        self.original = noisy_pytest(failing=False)
        (self.root / "pytest.py").write_text("import sys\nsys.stdout.buffer.write(%r)\n" % self.original.encode(),
                                             encoding="utf-8")

    def wrapper(self, *location):
        return [sys.executable, "-I", "-B", str(ROOT / "scripts/command.py"), *location,
                "--", sys.executable, "-m", "pytest"]

    def test_capture_note_has_no_recover_wording_and_recovery_still_works(self):
        result = run_like_bash_tool(self.wrapper("--scratchpad", str(self.scratch)), env=self.env, cwd=self.root,
                                    timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout.decode()
        self.assertTrue(output.startswith("Full output: "), output[:200])
        self.assertIn("300 passing-test lines omitted; every other line is shown, including the summary]", output)
        self.assertNotIn("recover", output.lower())
        artifact = output.split("\n", 1)[0].split("Full output: ", 1)[1].rsplit(" (", 1)[0]
        self.assertEqual(Path(artifact).read_text(encoding="utf-8"), self.original)
        restored = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/artifact.py"),
                                   "--scratchpad", str(self.scratch), "--id", Path(artifact).stem, "--stream", "stdout"],
                                  capture_output=True, env=self.env, cwd=self.root, timeout=30)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(restored.stdout, self.original.encode())

    def test_output_to_a_file_is_reduced_and_claims_the_saving(self):
        result = run_like_bash_tool(self.wrapper("--scratchpad", str(self.scratch)), env=self.env, cwd=self.root,
                                    timeout=30)
        self.assertEqual(result.returncode, 0)
        [row] = ledger.load(self.store)
        self.assertEqual(row["bytes_after"], len(result.stdout) + len(result.stderr))
        self.assertGreater(row["est_tokens_avoided"], 1000)

    @unittest.skipIf(shutil.which("bash") is None or os.name == "nt", "POSIX shell pipelines")
    def test_a_pipeline_after_the_wrapper_gets_the_original_output(self):
        wrapper = " ".join(map(shlex.quote, self.wrapper("--scratchpad", str(self.scratch))))
        direct = "%s -m pytest" % shlex.quote(sys.executable)
        for tail in ("| grep -c PASSED", "| tail -3", "2>&1 | tail -30"):
            with self.subTest(pipeline=tail):
                plugin = run_like_bash_tool(["bash", "-c", wrapper + " " + tail], env=self.env, cwd=self.root, timeout=30)
                native = run_like_bash_tool(["bash", "-c", direct + " " + tail], env=self.env, cwd=self.root, timeout=30)
                self.assertEqual((plugin.returncode, plugin.stdout, plugin.stderr),
                                 (native.returncode, native.stdout, native.stderr))
        self.assertEqual(run_like_bash_tool(["bash", "-c", wrapper + " | grep -c PASSED"], env=self.env,
                                            cwd=self.root, timeout=30).stdout, b"301\n")
        # Nothing claimed; the original is still saved for recovery.
        self.assertEqual(ledger.load(self.store), [])
        saved = list(Path(self.store.tool_output_dir()).glob("*.txt"))
        self.assertTrue(saved)
        self.assertTrue(all(path.read_text(encoding="utf-8") == self.original for path in saved))

    def test_a_trimmed_stderr_withdraws_the_all_lines_shown_claim(self):
        (self.root / "pytest.py").write_text(
            "import sys\nsys.stdout.buffer.write(%r)\nsys.stderr.write(''.join('warning %%05d\\n' %% i for i in range(3000)))\n"
            % self.original.encode(), encoding="utf-8")
        result = run_like_bash_tool(self.wrapper("--scratchpad", str(self.scratch)), env=self.env, cwd=self.root, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        self.assertIn(b"300 passing-test lines omitted; other lines trimmed where marked]", result.stdout)
        self.assertNotIn(b"every other line is shown", result.stdout)

    def test_session_ids_must_be_plain_names(self):
        for location, env in ((["--session", ""], {}), (["--session", "../x"], {}), (["--session", "/abs/id"], {}),
                              ([], {"CLAUDE_CODE_SESSION_ID": "../../escape"}), (["--scratchpad", ""], {})):
            with self.subTest(location=location, env=env):
                result = subprocess.run(self.wrapper(*location), capture_output=True, cwd=self.root, timeout=30,
                                        env=dict(self.env, **env))
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIsNone(session.Store.find("../../x", {}))

    def test_session_and_data_default_from_the_environment(self):
        temp = self.root / "tmp"
        temp.mkdir()
        env = dict(self.env, CLAUDE_CODE_SESSION_ID="b1-session", TMPDIR=str(temp))
        result = run_like_bash_tool(self.wrapper(), env=env, cwd=self.root, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Entitled through CLAUDE_PLUGIN_DATA alone: the output was reduced.
        self.assertIn(b"passing-test lines omitted", result.stdout)
        stores = list(temp.glob("*/b1-session/ork-meter/tool-output/*.json"))
        self.assertEqual(len(stores), 1, list(temp.rglob("*")))
        missing = subprocess.run(self.wrapper(), capture_output=True, cwd=self.root, timeout=30,
                                 env={k: v for k, v in env.items() if k != "CLAUDE_CODE_SESSION_ID"})
        self.assertEqual(missing.returncode, 2)
        self.assertIn(b"CLAUDE_CODE_SESSION_ID", missing.stderr)


class ShortForm(unittest.TestCase):
    """The stable capture form keeps #180/#217's exemption rules: only the
    plugin's own variable, only as the wrapper's path, no compound command."""

    def test_short_form_is_a_capture_command(self):
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        for command in ('bash "$CLAUDE_PLUGIN_ROOT/hooks/py.sh" --script scripts/command.py -- python3 -m pytest -v',
                        'bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/command.py -- python3 -m pytest -v'):
            with self.subTest(command=command):
                self.assertTrue(recovery.is_capture_command(command, env, str(ROOT)))
                self.assertEqual(recovery.exempt_script(command, env, str(ROOT)).script, "command.py")
        for command in ('bash "$CLAUDE_PLUGIN_ROOT/hooks/py.sh" --script scripts/command.py -- python3 -m pytest | tail -30',
                        'bash "$CLAUDE_PLUGIN_ROOT/hooks/py.sh" --script scripts/command.py -- pytest 2>&1',
                        'bash "$HOME/hooks/py.sh" --script scripts/command.py -- pytest',
                        'bash "${CLAUDE_PLUGIN_ROOT:-/tmp}/hooks/py.sh" --script scripts/command.py -- pytest',
                        'bash "$CLAUDE_PLUGIN_ROOT/hooks/py.sh" --script scripts/command.py -- pytest "$(cat x >&2)"',
                        'bash "$CLAUDE_PLUGIN_DATA/hooks/py.sh" --script scripts/command.py -- pytest'):
            with self.subTest(command=command):
                self.assertFalse(recovery.is_capture_command(command, env, str(ROOT)))
        # The wrapper path must be the plugin root the hook runs from, through
        # no link: a symlink to the plugin elsewhere is refused.
        elsewhere = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, elsewhere, True)
        try:
            (elsewhere / "link").symlink_to(ROOT, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        self.assertFalse(recovery.is_capture_command(
            'bash "%s/link/hooks/py.sh" --script scripts/command.py -- pytest' % elsewhere, env, str(ROOT)))

    @unittest.skipIf(shutil.which("bash") is None or os.name == "nt", "POSIX bash runs py.sh here")
    def test_short_form_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ORK_METER_", "CLAUDE_CODE_"))}
            env.update(CLAUDE_PLUGIN_ROOT=str(ROOT), CLAUDE_CODE_SESSION_ID="b1-short", TMPDIR=directory)
            result = subprocess.run(["bash", "-c", 'bash "$CLAUDE_PLUGIN_ROOT/hooks/py.sh" --script scripts/command.py'
                                     ' -- python3 -c "print(42)"'], capture_output=True, env=env, timeout=30)
            self.assertEqual((result.returncode, result.stdout), (0, b"42\n"), result.stderr)
            self.assertEqual(len(list(Path(directory).glob("*/b1-short/ork-meter/tool-output/*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
