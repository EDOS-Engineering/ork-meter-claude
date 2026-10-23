"""Behavior checks for recoverable reductions and fresh source selection."""
import contextlib
import collections
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import itertools
import json
import os
import random
import re
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, StoreCase, context as hook_context, git_bash, load, payload, run_hook  # noqa: E402
import artifacts  # noqa: E402
import features  # noqa: E402
import ledger  # noqa: E402
import session  # noqa: E402
import token_gate  # noqa: E402
import trimmer  # noqa: E402
import recovery  # noqa: E402


class RecoveryTests(StoreCase):
    def symlink(self, link, target):
        """Make a symlink, or skip: on Windows it may need privileges."""
        try:
            Path(link).symlink_to(target, target_is_directory=Path(target).is_dir())
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")

    def shell(self):
        found = git_bash()
        if not found:
            self.skipTest("Git Bash not found")
        return found

    def setUp(self):
        StoreCase.setUp(self)  # also ExemptScriptDifferentialTests.setUp
        self.store = session.Store.for_payload({"scratchpad_dir": str(self.scratch)})

    def context(self, response, tool="Bash", tool_input=None, agent=None):
        fields = {"agent_id": agent} if agent else {}
        return hook_context(payload(self.scratch, tool, tool_input or {"command": "echo noisy"}, response,
                                    tool_use_id="reused-id", prompt_id="prompt-1", **fields))

    def test_recovery_cli_output_bypasses_bash_reduction_for_full_ranges_and_search(self):
        cli = load("scripts/artifact.py")
        reducer = load("hooks/trim-tool-output.py")
        original = {"stdout": "".join("record_%04d evidence\n" % i for i in range(3000)),
                    "stderr": "error detail", "exitCode": 1}
        path = artifacts.save(self.store, "Bash", original, original["stdout"])
        args = ["--scratchpad", str(self.scratch), "--id", Path(path).stem]
        for action in (["--full"], ["--start", "1", "--limit", "3000"],
                       ["--search", "evidence", "--limit", "3000"]):
            with self.subTest(action=action):
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    self.assertEqual(cli.main(args + action), 0)
                response = {"stdout": captured.getvalue(), "stderr": "", "exitCode": 0, "isImage": False}
                command = 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/artifact.py ' + " ".join(action)
                ctx = self.context(response, tool_input={"command": command})
                before = list(Path(self.store.tool_output_dir()).iterdir())
                self.assertIsNone(reducer.handle(ctx))
                self.assertEqual(list(Path(self.store.tool_output_dir()).iterdir()), before)
                self.assertIn("record_2999", response["stdout"])
        rows = ledger.load(self.store)
        self.assertEqual([r["kind"] for r in rows], ["recovery"] * 3)

    def test_exact_source_retrieval_bypasses_bash_reduction(self):
        reducer = load("hooks/trim-tool-output.py")
        response = {"stdout": "exact source\n" * 5000, "stderr": "", "isImage": False}
        command = 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/source.py example.py --symbol execute'
        self.assertIsNone(reducer.handle(self.context(response, tool_input={"command": command})))
        self.assertTrue(recovery.is_source_command(command, {"CLAUDE_PLUGIN_ROOT": str(ROOT)}, str(ROOT)))
        self.assertFalse(recovery.is_source_command(command + " | cat", {"CLAUDE_PLUGIN_ROOT": str(ROOT)}, str(ROOT)))
        self.assertFalse(self.store.has_ledger())

    def test_recovery_command_recognition_is_narrow_and_does_not_evaluate_shell(self):
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        wrapper = str(ROOT / "hooks/py.sh").replace("\\", "/")
        artifact = str(ROOT / "scripts/artifact.py").replace("\\", "/")
        for command in (
            'bash "' + wrapper + '" --script scripts/artifact.py --full',
            'bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/artifact.py --search "|"',
            'bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/artifact.py --search "x; y"',
            'python3 -I -B "' + artifact + '" --full',
            'python -I -B scripts/artifact.py --full',
        ):
            with self.subTest(command=command):
                self.assertTrue(recovery.is_recovery_command(command, env, str(ROOT)))
        for command in (
            'echo "scripts/artifact.py --full"',
            'python other/artifact.py --full',
            'bash other/hooks/py.sh --script scripts/artifact.py --full',
            'bash "' + wrapper + '" --script scripts/source.py example.py',
            'bash "' + wrapper + '" --script scripts/artifact.py --full; echo unrelated',
            'bash "' + wrapper + '" --script scripts/artifact.py --full | cat',
            'python -c "print(1)" "' + artifact + '"',
            'bash -c "python scripts/artifact.py --full"',
        ):
            with self.subTest(command=command):
                self.assertFalse(recovery.is_recovery_command(command, env, str(ROOT)))

    def test_expansions_inside_double_quotes_are_refused(self):
        # A command substitution in double quotes runs, and its output on
        # stderr must not skip trimming (#171).
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        wrapper = "bash '" + str(ROOT / "hooks/py.sh").replace("\\", "/") + "' --script scripts/artifact.py --id "
        for argument in (
            '"$(cat bigfile >&2)"',
            '"${x}"',
            '"$x"',
            '$x',
            '${x}',
            '"`cat bigfile >&2`"',
            '"\\"$(cat bigfile >&2)\\""',
            '"\\\\$(cat bigfile >&2)"',
            '"a\\"b$(cat bigfile >&2)"',
            '"$((1 + 1))"',
            '"${CLAUDE_PLUGIN_ROOT:-$(cat bigfile >&2)}"',
            '"${CLAUDE_PLUGIN_ROOT}$(cat bigfile >&2)"',
            '"$CLAUDE_PLUGIN_ROOTX"',
            '"${!CLAUDE_PLUGIN_ROOT}"',
            "$'\\x41'",
            '$"x"',
            '"$1"',
        ):
            with self.subTest(argument=argument):
                self.assertFalse(recovery.is_recovery_command(wrapper + argument, env, str(ROOT)))
                self.assertFalse(recovery.is_source_command(wrapper.replace("artifact.py", "source.py") + argument,
                                                            env, str(ROOT)))
                self.assertFalse(recovery.is_capture_command(wrapper.replace("artifact.py", "command.py") + argument,
                                                             env, str(ROOT)))
        # Literal dollars stay data: single quotes and escaped dollars expand nothing.
        for argument in ("'$(cat bigfile)'", '"\\$(cat bigfile)"', '"cost \\$5"'):
            with self.subTest(argument=argument):
                self.assertTrue(recovery.is_recovery_command(wrapper + argument, env, str(ROOT)))

    def hint_commands(self):
        """Every recovery, source and capture command the plugin's skills
        and docs tell Claude to run, with the script each names."""
        pattern = re.compile(r'^\s*((?:bash "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/py\.sh" --script scripts/'
                             r'|python3 -I -B scripts/)(artifact|source|command)\.py\b.*)$')
        found = []
        for path in sorted(ROOT.glob("skills/*/SKILL.md")) + sorted(ROOT.glob("docs/*.md")):
            for line in path.read_text(encoding="utf-8").splitlines():
                match = pattern.match(line)
                if match:
                    found.append((match.group(1).strip(), match.group(2) + ".py"))
        scripts = {script for _, script in found}
        self.assertEqual(scripts, {"artifact.py", "source.py", "command.py"})
        return found

    def assert_hints_recognised(self, hint_root, env_root):
        env = {"CLAUDE_PLUGIN_ROOT": str(env_root)}
        check = {"artifact.py": recovery.is_recovery_command, "source.py": recovery.is_source_command,
                 "command.py": recovery.is_capture_command}
        for command, script in self.hint_commands():
            with self.subTest(command=command, env_root=str(env_root)):
                # Relative script paths in the docs are run from the plugin root.
                self.assertTrue(check[script](command, env, str(hint_root)))
        record_handles = load("hooks/lib/record_handles.py")
        record_handles.ROOT = str(hint_root)
        artifact = self.root / "scratch/ork-meter/tool-output" / ("a" * 64 + ".txt")
        command = record_handles.recovery_command(artifact)
        with self.subTest(command=command, env_root=str(env_root)):
            self.assertTrue(recovery.is_recovery_command(command, env, str(self.root)))
            self.assertFalse(recovery.is_recovery_command(command + " | cat", env, str(self.root)))

    def test_every_emitted_hint_is_recognised_with_real_and_symlinked_roots(self):
        link = self.root / "plugin-link"
        self.symlink(link, ROOT)
        self.assert_hints_recognised(ROOT, ROOT)
        # The hint's ROOT comes from this module's own path, which is
        # CLAUDE_PLUGIN_ROOT as given or, if py.sh ran from a resolved
        # directory, its realpath.
        self.assert_hints_recognised(ROOT, link)
        self.assert_hints_recognised(link, link)

    def symlinked_plugin_probe(self):
        """A directory whose hooks/py.sh or scripts/artifact.py links into
        the plugin while its other files are someone else's (#180 review)."""
        work = self.root / "work"
        (work / "hooks/lib").mkdir(parents=True)
        (work / "scripts").mkdir()
        self.symlink(work / "hooks/py.sh", ROOT / "hooks/py.sh")
        return work

    def test_symlinked_wrapper_running_other_code_is_not_exempt(self):
        work = self.symlinked_plugin_probe()
        (work / "scripts/artifact.py").write_text('print("EVIL")\n')
        command = "bash hooks/py.sh --script scripts/artifact.py --full"
        result = subprocess.run([self.shell(), "-c", command], cwd=work, capture_output=True, text=True)
        self.assertIn("EVIL", result.stdout)
        self.assertFalse(recovery.is_recovery_command(command, {"CLAUDE_PLUGIN_ROOT": str(ROOT)}, str(work)))

    def test_symlinked_script_importing_other_code_is_not_exempt(self):
        work = self.symlinked_plugin_probe()
        self.symlink(work / "scripts/artifact.py", ROOT / "scripts/artifact.py")
        (work / "hooks/lib/artifacts.py").write_text('print("EVIL2")\n')
        command = "python3 -I -B scripts/artifact.py --full"
        result = subprocess.run([sys.executable] + command.split()[1:], cwd=work, capture_output=True, text=True)
        # artifact.py finds hooks/lib from its resolved path, as the other
        # scripts do (#191), so the link runs the plugin's own modules. The
        # exemption still goes only by the path as written: a script whose
        # path reaches the plugin through a link is never exempt.
        self.assertNotIn("EVIL2", result.stdout + result.stderr)
        self.assertFalse(recovery.is_recovery_command(command, {"CLAUDE_PLUGIN_ROOT": str(ROOT)}, str(work)))

    def test_only_the_plugin_root_variable_may_spell_the_path(self):
        work = self.root / "work"
        work.mkdir()
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        for name in ("CLAUDE_SESSION_ID", "CLAUDE_PLUGIN_DATA", "CLAUDE_PROJECT_DIR"):
            link = work / ("$" + name)
            self.symlink(link, ROOT)
            for spelling in ("$" + name, "${" + name + "}"):
                for command in ('bash "%s/hooks/py.sh" --script scripts/artifact.py --full' % spelling,
                                'python3 "%s/scripts/artifact.py" --full' % spelling):
                    with self.subTest(command=command):
                        self.assertFalse(recovery.is_recovery_command(command, env, str(work)))
        # Single-quoted, the plugin's own variable is not expanded either.
        self.symlink(work / "$CLAUDE_PLUGIN_ROOT", ROOT)
        for command in ("bash '$CLAUDE_PLUGIN_ROOT/hooks/py.sh' --script scripts/artifact.py --full",
                        "bash \\$CLAUDE_PLUGIN_ROOT/hooks/py.sh --script scripts/artifact.py --full"):
            with self.subTest(command=command):
                self.assertFalse(recovery.is_recovery_command(command, env, str(work)))
        # Elsewhere in the command they are arguments, as the skills use them.
        self.assertTrue(recovery.is_recovery_command(
            'bash "$CLAUDE_PLUGIN_ROOT/hooks/py.sh" --script scripts/artifact.py --session "$CLAUDE_SESSION_ID"'
            ' --scratchpad "${CLAUDE_PLUGIN_DATA}"', env, str(work)))

    def test_relative_wrapper_from_a_symlinked_directory_is_not_exempt(self):
        # A logical pwd in py.sh took the root from the symlink's parent and
        # ran its scripts/artifact.py (#180 review).
        work = self.root / "work"
        (work / "scripts").mkdir(parents=True)
        (work / "scripts/artifact.py").write_text('print("EVIL1")\n')
        self.symlink(work / "hk", ROOT / "hooks")
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        for wrapper in ("py.sh", "./py.sh"):
            command = "bash %s --script scripts/artifact.py --full" % wrapper
            with self.subTest(command=command):
                result = subprocess.run([self.shell(), "-c", "cd hk && " + command], cwd=work, capture_output=True,
                                        text=True,
                                        env=dict(os.environ, CLAUDE_PLUGIN_DATA=""))
                self.assertNotIn("EVIL1", result.stdout + result.stderr)
                self.assertFalse(recovery.is_recovery_command(command, env, str(work / "hk")))
                self.assertFalse(recovery.is_recovery_command(command, env, str(ROOT / "hooks")))

    def test_hint_through_a_symlinked_root_runs_the_plugin(self):
        link = self.root / "plugin-link"
        self.symlink(link, ROOT)
        response = {"stdout": "recovered through the link\n", "stderr": "", "exitCode": 0}
        identity = Path(artifacts.save(self.store, "Bash", response, response["stdout"])).stem
        command = ('bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/artifact.py --scratchpad "%s" --id %s --full'
                   % (self.scratch.as_posix(), identity))
        env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(link), CLAUDE_PLUGIN_DATA="")
        result = subprocess.run([self.shell(), "-c", command], cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), response)
        self.assertTrue(recovery.is_recovery_command(command, {"CLAUDE_PLUGIN_ROOT": str(link)}, str(self.root)))

    def test_unquoted_glob_or_brace_in_any_word_is_not_exempt(self):
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        base = 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/artifact.py '
        for tail in ("--id *", "--id {a,b}", "--id a?b", "--search [x]", "--id {a..c}", "--id x*"):
            with self.subTest(tail=tail):
                self.assertFalse(recovery.is_recovery_command(base + tail, env, str(ROOT)))
        for tail in ("--id '*'", '--search "{a,b}"', "--search \\*", "--select-json '{\"pointers\":[\"/0\"]}'"):
            with self.subTest(tail=tail):
                self.assertTrue(recovery.is_recovery_command(base + tail, env, str(ROOT)))

    def test_globbed_artifact_read_is_not_exempt(self):
        path = Path(artifacts.save(self.store, "Bash", {"stdout": "x"}, "x"))
        store = Path(self.store.tool_output_dir())
        work = self.root / "work"
        work.mkdir()
        commands = ("cat */" + path.name, "head -n 5 ?/" + path.name, "cat {*,x}/" + path.name)
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(recovery.artifact_read_paths(command, str(work)))
        # A directory named * (not a valid Windows name) linking to the store.
        self.symlink(work / "*", store)
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(recovery.reads_only_artifacts(command, str(store), str(work)))
        # Quoted, the same name is the one directory, which is the store.
        self.assertTrue(recovery.reads_only_artifacts("cat '*'/" + path.name, str(store), str(work)))

    def test_interpreter_must_be_a_bare_name(self):
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        wrapper = '"' + str(ROOT / "hooks/py.sh") + '" --script scripts/artifact.py --full'
        script = '"' + str(ROOT / "scripts/artifact.py") + '" --full'
        for command in ("./bash " + wrapper, "/tmp/x/bash " + wrapper, "/bin/bash " + wrapper,
                        "BASH " + wrapper, "bash.exe " + wrapper, '"$CLAUDE_PLUGIN_ROOT/bash" ' + wrapper,
                        "/tmp/x/python3 " + script, "./python " + script, "python3.exe " + script,
                        "b?sh " + wrapper, "env bash " + wrapper):
            with self.subTest(command=command):
                self.assertFalse(recovery.is_recovery_command(command, env, str(ROOT)))
        for command in ("bash " + wrapper, "python3 " + script, "python -I -B " + script):
            with self.subTest(command=command):
                self.assertTrue(recovery.is_recovery_command(command, env, str(ROOT)))

    def test_wrapper_path_tricks_are_not_exempt(self):
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        for path in (str(ROOT / "scripts/../hooks/py.sh"), str(ROOT / "hooks/p?.sh"), str(ROOT / "hooks/py.{sh,x}"),
                     "~/hooks/py.sh"):
            command = "bash %s --script scripts/artifact.py --full" % path
            with self.subTest(command=command):
                self.assertFalse(recovery.is_recovery_command(command, env, str(ROOT)))
        self.assertFalse(recovery.is_recovery_command(
            "bash hooks/py.sh # --script scripts/artifact.py", env, str(ROOT)))

    def test_long_commands_are_not_exempt_and_stay_cheap(self):
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        base = 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/artifact.py --search '
        fits = base + '"' + "x" * (recovery.MAX_COMMAND_CHARS - len(base) - 2) + '"'
        self.assertTrue(recovery.is_recovery_command(fits, env, str(ROOT)))
        self.assertFalse(recovery.is_recovery_command(fits + " ", env, str(ROOT)))
        huge = base + '"' + "$CLAUDE_PLUGIN_ROOT" * 55000 + '"'
        started = time.monotonic()
        self.assertFalse(recovery.is_recovery_command(huge, env, str(ROOT)))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_unresolvable_path_is_not_exempt(self):
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        command = 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/artifact.py --full'
        with patch.object(recovery.os.path, "realpath", side_effect=OSError("loop")):
            self.assertFalse(recovery.is_recovery_command(command, env, str(ROOT)))
        self.assertFalse(recovery.is_recovery_command('bash "a\x00b" --script scripts/artifact.py --full',
                                                      env, str(ROOT)))

    def test_mixed_test_outcomes_preserve_skipped_coverage_and_exact_recovery(self):
        cases = [
            ("cargo test", ["test critical_security_check ... ignored", "test auth ... FAILED"],
             ["test a ... ok", "test b ... ok", "test c ... ok"]),
            ("pytest -v", ["tests/security.py .sxX [100%]", "tests/auth.py::check SKIPPED (requires key)",
                           "tests/auth.py::other XFAIL", "tests/auth.py::broken FAILED"],
             [f"tests/a.py::test_{i} PASSED" for i in range(9)]),
            ("mvn test", ["[INFO] Tests run: 4, Failures: 0, Errors: 0, Skipped: 1 - in SecurityTest",
                          "[ERROR] Tests run: 1, Failures: 1, Errors: 0, Skipped: 0"],
             ["[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0 - in Test%d" % i for i in range(3)]),
            ("npm test", ["✓ critical_security_check skipped (missing credentials)", "TODO external_api",
                          "FAIL checkout.test.js"], ["✓ checkout", "✓ login", "✓ retry"]),
        ]
        reducer = load("hooks/trim-tool-output.py")
        for command, required, passed in cases:
            with self.subTest(command=command):
                # Keep the original comfortably larger than its recovery note.
                original = "\n".join(required + [line + " " * 150 for line in passed])
                filtered, omitted = trimmer.filter_test_output(original)
                self.assertEqual(omitted, len(passed))
                for line in required:
                    self.assertIn(line, filtered)
                response = {"stdout": original, "stderr": "", "exitCode": 1, "interrupted": False, "isImage": False}
                # The handler gates its own reply (#130); these outputs are
                # too small to pass, and only the filter is under test.
                with patch.object(token_gate, "permits_reply", return_value=True):
                    reply = reducer.handle(self.context(response, tool_input={"command": command}))
                self.assertIsNotNone(reply)
                output = reply["hookSpecificOutput"]["updatedToolOutput"]
                self.assertEqual(output["exitCode"], 1)
                for line in required:
                    self.assertIn(line, output["stdout"])
                path = Path(output["stdout"].split("Full output: ")[-1].rsplit(" (", 1)[0])
                self.assertEqual(artifacts.load(self.store, path.stem)["response"], response)

    def test_parallel_artifacts_recover_exact_status_and_separate_streams(self):
        responses = [{"stdout": "x\r\n" * 12000 + str(i), "stderr": "fatal: detail\n--- stderr ---\n",
                      "interrupted": False, "isImage": False, "exitCode": i, "extra": {"a": i}}
                     for i in range(4)]
        reducer = load("hooks/trim-tool-output.py")
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda r: reducer.handle(self.context(r)), responses))
        identities = []
        for response, reply in zip(responses, replies):
            output = reply["hookSpecificOutput"]["updatedToolOutput"]
            self.assertEqual(output["exitCode"], response["exitCode"])
            path = Path(output["stdout"].split("Full output: ")[-1].rsplit(" (", 1)[0])
            document = artifacts.load(self.store, path.stem)
            self.assertEqual(document["response"], response)
            self.assertEqual(path.read_bytes(), document["text"].encode("utf-8"))
            identities.append(path.stem)
        self.assertEqual(len(set(identities)), 4)

    def test_search_scope_and_exact_ranges(self):
        path = artifacts.save(self.store, "WebFetch", {"result": "line"}, "a\r\nhit one\nhit two\nlast")
        document = artifacts.load(self.store, Path(path).stem)
        result = artifacts.select(document, 1, 1, "hit")
        self.assertEqual((result["total_matches"], result["returned"], result["omitted"]), (2, 1, 1))
        self.assertEqual(result["lines"], [{"line": 2, "text": "hit one\n"}])
        self.assertEqual(artifacts.select(document, 4, 10)["lines"], [{"line": 4, "text": "last"}])

    def test_tampering_and_traversal_refused(self):
        path = artifacts.save(self.store, "Bash", {"stdout": "original"}, "original")
        identity = Path(path).stem
        document = artifacts.load(self.store, identity)
        document["text"] = "changed"
        Path(self.store.tool_output_path(identity, ".json")).write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "integrity"):
            artifacts.load(self.store, identity)
        with self.assertRaises(ValueError):
            artifacts.load(self.store, "../outside")

    def test_accounting_failure_does_not_prevent_requested_recovery(self):
        cli = load("scripts/artifact.py")
        response = {"stdout": "saved", "stderr": "error detail", "exitCode": 1}
        path = artifacts.save(self.store, "Bash", response, "saved")
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture), patch.object(ledger, "append", side_effect=OSError("disk full")):
            self.assertEqual(cli.main(["--scratchpad", str(self.scratch), "--id", Path(path).stem, "--full"]), 0)
        self.assertEqual(json.loads(capture.getvalue()), response)
        self.assertEqual(self.store.accounting_gaps()[0]["event"], "recovery")
        result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/savings.py"),
                                 "--scratchpad", str(self.scratch), "--no-measure", "--json"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["accounting_status"], "partial")
        self.assertEqual(report["accounting_gap_count"], 1)
        self.assertEqual(ledger.load(self.store), [])

    def test_artifact_selection_preserves_physical_lines_and_embedded_separators(self):
        physical = ["heading\fpage", 'record = "before\u2028after\u2029tail\x85end"', "last"]
        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=repr(newline)):
                original = newline.join(physical) + newline
                path = artifacts.save(self.store, "Read", {"content": original}, original)
                document = artifacts.load(self.store, Path(path).stem)
                selected = artifacts.select(document, 2, 1)
                self.assertEqual(selected["total_lines"], 3)
                self.assertEqual(selected["lines"], [{"line": 2, "text": physical[1] + newline}])
                found = artifacts.select(document, query="before\u2028after")
                self.assertEqual((found["total_matches"], found["returned"], found["omitted"]), (1, 1, 0))
                self.assertEqual(found["lines"], selected["lines"])

    def test_full_recovery_and_json_projection_count_recovery_work(self):
        cli = load("scripts/artifact.py")
        response = {"result": '{"keep": 1, "omit": "SECRET"}', "code": 200}
        path = artifacts.save(self.store, "WebFetch", response, response["result"])
        base = ["--scratchpad", str(self.scratch), "--id", Path(path).stem]
        for args, expected in ((["--full"], response), (["--fields", "keep", "missing"],
                {"fields": {"keep": 1}, "missing_fields": ["missing"], "omitted_fields": 1, "selection": "top-level JSON keys"})):
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture):
                self.assertEqual(cli.main(base + args), 0)
            self.assertEqual(json.loads(capture.getvalue()), expected)
        rows = ledger.load(self.store)
        self.assertEqual([r["kind"] for r in rows], ["recovery", "recovery"])
        self.assertTrue(all(r["est_tokens_avoided"] == 0 and r["bytes_after"] > 0 for r in rows))

    def test_explicit_ranges_and_recovery_reads_bypass_both_reducers(self):
        reader = load("hooks/dedupe-reads.py")
        response = {"type": "text", "file": {"filePath": "source.py", "content": "line\n" * 20000,
                    "startLine": 1, "numLines": 20000, "totalLines": 20000}}
        for input_ in ({"offset": 1}, {"limit": 20000}, {"allow_large": True}):
            self.assertIsNone(reader.handle(self.context(response, "Read", input_)))
        response["file"]["filePath"] = str(Path(self.store.tool_output_dir()) / "artifact.txt")
        self.assertIsNone(reader.handle(self.context(response, "Read")))
        guard = load("hooks/read-guard.py")
        self.assertIsNone(guard.handle(self.context({}, "Read", {"file_path": response["file"]["filePath"]})))

    def test_delivery_state_is_scoped_to_agent_and_resets_on_compaction(self):
        reader = load("hooks/dedupe-reads.py")
        response = {"type": "text", "file": {"filePath": "source.py", "content": "line\n" * 1000,
                    "startLine": 1, "numLines": 1000, "totalLines": 1000}}
        for agent in (None, "a", "b"):
            self.assertIsNone(reader.handle(self.context(response, "Read", agent=agent)))
            self.assertIsNotNone(reader.handle(self.context(response, "Read", agent=agent)))
        self.store.reset()
        self.assertIsNone(reader.handle(self.context(response, "Read", agent="a")))

    def test_snapshot_survives_file_edit(self):
        reader = load("hooks/dedupe-reads.py")
        response = {"type": "text", "file": {"filePath": str(self.root / "source.py"),
                    "content": "original line\n" * 4000, "startLine": 1, "numLines": 4000, "totalLines": 4000}}
        reply = reader.handle(self.context(response, "Read"))
        path = Path(reply["hookSpecificOutput"]["additionalContext"].split("Full original Read snapshot: ")[-1])
        Path(response["file"]["filePath"]).write_text("edited")
        self.assertEqual(artifacts.load(self.store, path.stem)["response"], response)

    def test_artifact_failure_passes_original_without_reexecution(self):
        reducer = load("hooks/trim-tool-output.py")
        event = self.context({"stdout": "line\n" * 20000, "stderr": "", "isImage": False}).payload
        with patch.object(artifacts, "save", side_effect=OSError("disk full")):
            self.assertEqual(run_hook(event, reducer.handle, raw=True), "")

    def test_feature_binding_has_platform_hash_and_no_raw_identity(self):
        env = {"XDG_STATE_HOME": str(self.root / "state")}
        for sid, switches in (("first", {}), ("second", {"ORK_METER_TRIM": "off"})):
            features.write_features_file(dict(env, **switches), session_id=sid)
        first = Path(features.features_path(env, "first"))
        second = Path(features.features_path(env, "second"))
        self.assertEqual(first.stem, hashlib.sha256(b"first").hexdigest())
        self.assertIn("trim", json.loads(first.read_text())["features"])
        self.assertNotIn("trim", json.loads(second.read_text())["features"])
        self.assertEqual(set(json.loads(first.read_text())), {"features", "plugin_version", "updated_at"})
        self.assertNotIn("first", first.read_text())

    def test_accounting_includes_recovery_note_bytes(self):
        reducer = load("hooks/trim-tool-output.py")
        ctx = self.context({"stdout": "line\n" * 20000, "stderr": "error detail", "isImage": False})
        updated = reducer.handle(ctx)["hookSpecificOutput"]["updatedToolOutput"]
        row = ledger.load(self.store)[0]
        self.assertEqual(row["bytes_after"], trimmer.nbytes(updated["stdout"]) + trimmer.nbytes(updated["stderr"]))


class SourceTests(unittest.TestCase):
    def test_physical_lines_match_parser_coordinates_without_changing_source(self):
        source = load("scripts/source.py")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.py"
            for newline in ("\n", "\r\n", "\r"):
                with self.subTest(newline=repr(newline)):
                    physical = ["# heading\fpage break", "LABEL = 'before\u2028middle\u2029after\x85end'", "",
                                "@decorate", "def target():", "    return 7"]
                    raw = (newline.join(physical) + newline).encode("utf-8")
                    path.write_bytes(raw)
                    outline = source.retrieve(path, outline=True)
                    self.assertEqual(outline["total_lines"], 6)
                    entry = next(s for s in outline["symbols"] if s["name"] == "target")
                    self.assertEqual((entry["start"], entry["end"]), (4, 6))
                    result = source.retrieve(path, symbol="target", expected_hash=outline["sha256"])
                    self.assertEqual(result["source"], newline.join(physical[3:]) + newline)
                    self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
                    ranged = source.retrieve(path, start=1, limit=2)
                    self.assertEqual(ranged["source"], newline.join(physical[:2]) + newline)

    def test_form_feed_before_symbol_does_not_omit_final_body_line(self):
        source = load("scripts/source.py")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.py"
            path.write_bytes(b"# heading\fpage break\n\ndef target():\n    return 7\n")
            result = source.retrieve(path, symbol="target")
            self.assertEqual(result["source"], "def target():\n    return 7\n")
            self.assertEqual(result["total_lines"], 4)

    def test_bom_and_non_newline_characters_remain_in_exact_source(self):
        source = load("scripts/source.py")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bom.py"
            raw = b"\xef\xbb\xbf# heading\fbreak\r\ndef target():\r\n    return 7\r\n"
            path.write_bytes(raw)
            self.assertEqual(source.retrieve(path, start=1, limit=1)["source"], "\ufeff# heading\fbreak\r\n")
            result = source.retrieve(path, symbol="target")
            self.assertEqual(result["method"], "Python AST")
            self.assertEqual(result["source"], "def target():\r\n    return 7\r\n")

    def test_decorated_nested_symbol_is_exact_and_stale_hash_refused(self):
        source = load("scripts/source.py")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.py"
            raw = b'class Service:\r\n    @decorate(\r\n        "value"\r\n    )\r\n    def execute(self):\r\n        return 42\r\n\r\nOTHER = 7\r\n'
            path.write_bytes(raw)
            outline = source.retrieve(path, outline=True)
            result = source.retrieve(path, symbol="Service.execute", expected_hash=outline["sha256"])
            self.assertEqual(result["source"], b"".join(raw.splitlines(keepends=True)[1:6]).decode())
            path.write_bytes(raw + b"CHANGED = 1\n")
            with self.assertRaisesRegex(ValueError, "changed"):
                source.retrieve(path, symbol="Service.execute", expected_hash=outline["sha256"])

    def test_unsupported_and_invalid_syntax_fall_back_to_exact_lines(self):
        source = load("scripts/source.py")
        with tempfile.TemporaryDirectory() as temporary:
            for name in ("source.ts", "invalid.py"):
                path = Path(temporary) / name
                path.write_bytes(b"first (\nsecond\nthird\n")
                result = source.retrieve(path, outline=True, start=2, limit=1)
                self.assertEqual(result["source"], "second\n")
                self.assertEqual(result["method"], "exact lines")


class ExemptScriptDifferentialTests(StoreCase):
    """``recovery.exempt_script`` parses a Bash command once (#190 item 4).
    Its decision must equal the one trim-tool-output's ``passes_through``
    made before, with one parse per check: the frozen module in
    tests/fixtures/recovery_reference.py, on the recovery corpora above,
    every hint in skills/ and docs/, and their mutations."""
    setUp = RecoveryTests.setUp
    symlink = RecoveryTests.symlink
    hint_commands = RecoveryTests.hint_commands

    def reference(self):
        return load("tests/fixtures/recovery_reference.py")

    def corpus(self, store, names):
        """Commands: the literal ones in the recovery tests, the hints, record
        handles' command, and those combined with every refused shape."""
        commands = set()
        for source in ("tests/recovery_regressions.py", "tests/record_handle_regressions.py",
                       "tests/output_recovery_regressions.py"):
            path = ROOT / source
            if path.exists():
                for match in re.finditer(r"""(['"])((?:bash|python3?|cat|head|tail|sed) [^'"\n]*)\1""",
                                         path.read_text(encoding="utf-8")):
                    commands.add(match.group(2))
        hints = [command for command, _ in self.hint_commands()]
        commands.update(hints)
        record_handles = load("hooks/lib/record_handles.py")
        record_handles.ROOT = str(ROOT)
        commands.add(record_handles.recovery_command(Path(store) / names[0]))
        roots = [str(ROOT), str(ROOT).replace("\\", "/"), str(self.root / "plugin-link"), "${CLAUDE_PLUGIN_ROOT}",
                 "$CLAUDE_PLUGIN_ROOT", "$CLAUDE_PLUGIN_DATA", "${CLAUDE_SESSION_ID}", ".", "", "~",
                 str(ROOT / "scripts/.."), str(ROOT / "hooks/..")]
        quotes = ['"%s"', "'%s'", "%s"]
        interpreters = ["bash", "python3", "python", "python3 -I -B", "python -I", "/bin/bash", "./python3", "env bash",
                        "BASH", "bash.exe", "b?sh", "python3.exe", "Python3", "sh", "python3.12", "\\bash", "'bash'"]
        targets = ["hooks/py.sh --script scripts/%s", "scripts/%s", "hooks/py.sh --script other/%s",
                   "hooks/py.sh --script", "hooks/p?.sh --script scripts/%s", "hooks/py.sh -script scripts/%s"]
        scripts = ["artifact.py", "source.py", "command.py", "other.py", "artifact.pyc", ""]
        tails = ["", " --full", ' --search "x; y"', " --id *", " --id '*'", ' --id "$x"', " --id $x", " | cat",
                 "; echo hi", " && true", " # c", " > out", ' "$(cat big >&2)"', " '$(cat big)'", ' "\\$5"',
                 " `id`", ' --session "$CLAUDE_SESSION_ID"', " --scratchpad ${CLAUDE_PLUGIN_DATA}", " {a,b}",
                 ' "${CLAUDE_PLUGIN_ROOT:-x}"', " \\\n", " $'\\x41'", ' $"x"', " x\\", ' "open', " 'open"]
        spelled = itertools.product(interpreters, roots, quotes, targets, scripts)
        for index, (interpreter, root, quote, target, script) in enumerate(spelled):
            path = quote % ((root + "/" if root else "") + (target % script if "%s" in target else target))
            commands.add(interpreter + " " + path + tails[index % 4])
        for base in hints + ['bash "%s/hooks/py.sh" --script scripts/artifact.py --full' % ROOT.as_posix()]:
            for tail in tails:
                commands.add(base + tail)
                commands.add(tail.strip() + " " + base)
        operands = list(names) + [str(Path(store) / names[0]), str(Path(store) / names[1]),
                                  "'%s'" % (Path(store) / names[0]).as_posix(), "*/" + names[0], "../" + names[0],
                                  "x/" + names[0], "a" * 64 + ".txt", "a" * 63 + ".txt", "A" * 64 + ".txt",
                                  "$x/" + names[0], '"$CLAUDE_PLUGIN_ROOT/%s"' % names[0], "-", "--", "~/" + names[0],
                                  "notes.txt", "'*'/" + names[0]]
        options = ["", "-n", "-b", "-n 5", "-5", "-c 10k", "--lines=3", "-q", "-v", "-x", "-n", "-nfoo", "--bytes=1M",
                   "-n 'x'", "-n -5"]
        for program in ("cat", "head", "tail", "/bin/cat", "sed -n '1,5p'", "sed -n 1p", "sed '1,5p'",
                        "sed -n '1,5p;9,$p'", "sed -n -e 1p", "sed -n 'w x'", "less", "head -n"):
            for option in options:
                for first in operands:
                    for second in ("", " " + names[1], " " + operands[-1], " | cat", "; rm x"):
                        commands.add(" ".join(part for part in (program, option, first) if part) + second)
        prefix = 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/artifact.py --search '
        for size in (recovery.MAX_COMMAND_CHARS - 1, recovery.MAX_COMMAND_CHARS, recovery.MAX_COMMAND_CHARS + 1):
            commands.add(prefix + '"' + "x" * (size - len(prefix) - 2) + '"')
            commands.add("cat " + names[0] + " " * (size - len(names[0]) - 4))
        # Random recombinations of the pieces above, reproducibly.
        rng = random.Random(190)
        pieces = sorted({word for command in commands for word in command.split(" ")})
        for _ in range(1000):
            commands.add(" ".join(rng.choice(pieces) for _ in range(rng.randint(1, 7))))
        return sorted(commands) + [None, 7, ""]

    def test_exempt_script_decides_exactly_as_the_separate_checks(self):
        old = self.reference()
        link = self.root / "plugin-link"
        self.symlink(link, ROOT)
        work = self.root / "work"
        work.mkdir()
        store = Path(self.store.tool_output_dir())
        names = []
        for text in ("one", "two"):
            names.append(Path(artifacts.save(self.store, "Bash", {"stdout": text}, text)).name)
        self.symlink(work / "out", store)
        commands = self.corpus(store, names)
        self.assertGreater(len(commands), 15000)
        cases = 0
        decided = collections.Counter()
        # The whole corpus in one setting, a fixed sample of it in the others.
        full = {(str(ROOT), str(ROOT))}
        sample = random.Random(1904).sample(commands, 700)
        for root in (str(ROOT), str(link), None):
            env = {} if root is None else {"CLAUDE_PLUGIN_ROOT": root}
            for cwd in (str(ROOT), str(link), str(work), str(store), str(work / "out"), None, "", 5):
                for command in (commands if (root, cwd) in full else sample):
                    old_checks = (old.is_recovery_command(command, env, cwd), old.is_source_command(command, env, cwd),
                                  old.is_capture_command(command, env, cwd))
                    old_reads = old.artifact_read_paths(command, cwd)
                    # passes_through before #190 item 4, call for call.
                    old_exempt = any(old_checks) or bool(old_reads and old.reads_only_artifacts(command, str(store), cwd))
                    new = recovery.exempt_script(command, env, cwd)
                    new_exempt = new.script is not None or recovery.reads_only_in(new.reads, str(store))
                    case = (command, env, cwd)
                    self.assertEqual(new_exempt, old_exempt, case)
                    self.assertEqual(tuple(new.script == script for script in ("artifact.py", "source.py", "command.py")),
                                     old_checks, case)
                    self.assertEqual(new.reads, old_reads, case)
                    # The single-check wrappers kept for other callers are unchanged too.
                    self.assertEqual((recovery.is_recovery_command(command, env, cwd),
                                      recovery.is_source_command(command, env, cwd),
                                      recovery.is_capture_command(command, env, cwd)), old_checks, case)
                    self.assertEqual(recovery.artifact_read_paths(command, cwd), old_reads, case)
                    cases += 1
                    decided[(new.script, bool(new.reads), new_exempt)] += 1
        # The corpus reaches every outcome: each script, artifact reads in and
        # out of this store, and plain refusals.
        for outcome in (("artifact.py", False, True), ("source.py", False, True), ("command.py", False, True),
                        (None, True, True), (None, True, False), (None, False, False)):
            self.assertGreater(decided[outcome], 0, (outcome, decided))

    def test_unresolvable_read_path_is_not_exempt(self):
        store = Path(self.store.tool_output_dir())
        name = Path(artifacts.save(self.store, "Bash", {"stdout": "x"}, "x")).name
        for command, cwd in (("cat 'a\x00b'/" + name, str(store)), ("cat " + name, str(store) + "\x00")):
            with self.subTest(command=command, cwd=cwd):
                reads = recovery.exempt_script(command, {}, cwd).reads
                self.assertTrue(reads)
                self.assertFalse(recovery.reads_only_in(reads, str(store)))
                self.assertFalse(recovery.reads_only_artifacts(command, str(store), cwd))
        self.assertFalse(recovery.reads_only_in([str(store / name)], str(store) + "\x00"))
        self.assertTrue(recovery.reads_only_in([str(store / name)], str(store)))

    def test_exempt_script_refusals_from_the_strictness_rules(self):
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        base = 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/%s --full'
        for script in ("artifact.py", "source.py", "command.py"):
            self.assertEqual(recovery.exempt_script(base % script, env, str(ROOT)), (script, None))
            for command in ((base % script) + " | cat", (base % script) + ' "$(id)"', (base % script) + " *",
                            (base % script).replace("bash", "/bin/bash", 1),
                            (base % script) + " " + "x" * recovery.MAX_COMMAND_CHARS):
                self.assertEqual(recovery.exempt_script(command, env, str(ROOT)), recovery.NOT_EXEMPT, command)
        self.assertEqual(recovery.exempt_script('cat "$CLAUDE_PLUGIN_ROOT/%s"' % ("a" * 64 + ".txt"), env, str(ROOT)),
                         recovery.NOT_EXEMPT)


if __name__ == "__main__":
    unittest.main()
