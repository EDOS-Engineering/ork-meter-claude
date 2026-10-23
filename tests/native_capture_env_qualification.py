"""What a Bash command sees under the installed CLI, for the stable capture
form (#212): the real ``claude`` CLI, the localhost fixture and refusing proxy
of tests/native_host.py, a private HOME and CLAUDE_CONFIG_DIR, no external
traffic.

Settled here rather than in a live run:

1. ``CLAUDE_CODE_SESSION_ID`` in a Bash command is the session id of that
   session's hook payloads, so ``scripts/command.py`` without ``--session``
   writes to the hooks' own store: its artifact and ledger row land there.
2. Claude Code does not put ``CLAUDE_PLUGIN_ROOT`` or ``CLAUDE_PLUGIN_DATA`` in
   a Bash command's environment (2.1.302). The capture skill therefore spells
   both out, substituted inline; a caller that exports them to the CLI (the
   coding benchmark) does reach its Bash commands, and the
   ``$CLAUDE_PLUGIN_ROOT`` form then runs entitled and stays exempt from the
   Bash reducer.
3. A plain Bash command's stdout and stderr are regular files; piped into
   another command, stdout is a FIFO. The wrapper reduces only the first, and
   a pipeline after it gets the original output.
"""
import json
import os
import re
from pathlib import Path
import shlex
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import host_retry  # noqa: E402
from native_host import (Host, assert_hooks_fail_open, result_text, stream_session_ids,  # noqa: E402
                         tool_id, tool_results)
sys.path.insert(0, str(ROOT / "hooks/lib"))
import ledger  # noqa: E402
import session  # noqa: E402

PROBE = r'''import json, os, stat
def kind(fd):
    mode = os.fstat(fd).st_mode
    return "regular" if stat.S_ISREG(mode) else "fifo" if stat.S_ISFIFO(mode) else "socket" if stat.S_ISSOCK(mode) else "other"
print(json.dumps({"env": {name: os.environ.get(name) for name in
                          ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "CLAUDE_CODE_TMPDIR")},
                  "stdout": kind(1), "stderr": kind(2)}))
'''
# Large enough to be reduced: 3,000 rows, ~90 KB, with a distinct last line.
NOISY = "import sys\nsys.stdout.write(''.join('row_%04d ordinary context line\\n' % i for i in range(3000)))\n"
CAPTURE = "Ork Meter capture qualification"
# Claude Code on Windows (2.1.280, 2.1.303) appends this line to a Bash
# result when the shell's working directory differs from the session's.
HOST_NOTE = re.compile(r"^Shell cwd was reset to ")


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
class NativeCaptureEnvironment(unittest.TestCase):
    def session(self, host, env, capture):
        """One session: a plain and a piped probe, then ``capture`` (the
        wrapper's command form) run plainly and piped into ``tail -3``."""
        (host.work / "probe.py").write_text(PROBE)
        (host.work / "noisy.py").write_text(NOISY)
        seen = {}

        def result(body, request, index):
            block = tool_results(body).get(tool_id(request, index))
            return result_text(block) if block is not None else None

        # The script never raises: a failure here would only show as a
        # missing tool result in the next request. The assertions below
        # report what was received.
        def script(body, number):
            if number == 1:
                return [("tool", "Bash", {"command": "python3 probe.py"}),
                        ("tool", "Bash", {"command": "python3 probe.py | cat"})]
            if number == 2:
                seen["plain"], seen["piped"] = result(body, 1, 0), result(body, 1, 1)
                return [("tool", "Bash", {"command": capture + " -- python3 noisy.py"})]
            if number == 3:
                seen["capture"] = result(body, 2, 0)
                return [("tool", "Bash", {"command": capture + " -- python3 noisy.py | tail -3"})]
            if number == 4:
                seen["capture_piped"] = result(body, 3, 0)
            return [("text", "QUALIFIED")]
        host.script = script
        completed = host.run("Qualify the capture environment.", env=env)
        assert_hooks_fail_open(self, completed.stdout)
        host.assert_no_external_traffic()
        [session_id] = stream_session_ids(completed.stdout)
        stores = host.stores()
        for name in ("plain", "piped"):
            text = seen.get(name)
            self.assertIsInstance(text, str, "no %s probe result: %r" % (name, seen))
            # The probe prints one JSON line; anything after it is the host's.
            try:
                seen[name] = json.loads(text.strip().splitlines()[0])
            except (ValueError, IndexError):
                self.fail("unreadable %s probe result: %r" % (name, text[:2000]))
            seen[name]["raw"] = text
        return seen, session_id, stores

    def assert_capture(self, seen, session_id, stores):
        # (1) The Bash command's session id is the hooks' session id.
        self.assertEqual(seen["plain"]["env"]["CLAUDE_CODE_SESSION_ID"], session_id)
        self.assertEqual(list(stores), [session_id], "hook payloads name the same session")
        # (4) A plain command writes to regular files; piped, stdout is a FIFO.
        self.assertEqual((seen["plain"]["stdout"], seen["plain"]["stderr"]), ("regular", "regular"), seen["plain"]["raw"])
        self.assertEqual(seen["piped"]["stdout"], "fifo", seen["piped"]["raw"])
        # (3) The wrapper reduced the plain run, saved it in the hooks' store,
        # and recorded one trim there; the PostToolUse reducer left it alone.
        store = stores[session_id]
        self.assertIsInstance(seen.get("capture"), str, seen)
        self.assertTrue(seen["capture"].startswith("Full output: "), seen["capture"][:500])
        named = Path(seen["capture"].split("\n", 1)[0][len("Full output: "):].rsplit(" (", 1)[0])
        self.assertEqual(session.comparable_path(named.parent.parent), session.comparable_path(store.path))
        artifact = Path(seen["capture"].split("\n", 1)[0][len("Full output: "):].rsplit(" (", 1)[0])
        self.assertEqual(artifact.read_text(encoding="utf-8"),
                         "".join("row_%04d ordinary context line\n" % i for i in range(3000)))
        rows = [row for row in ledger.load(store) if row.get("tool") == "Bash"]
        self.assertEqual([(row["kind"], row.get("policy_version")) for row in rows], [("trim", "command-capture-v1")])
        # The piped run passed the original through: tail saw the real last lines.
        self.assertIsInstance(seen.get("capture_piped"), str, seen)
        self.assertEqual([line for line in seen["capture_piped"].strip().splitlines() if not HOST_NOTE.match(line)],
                         ["row_%04d ordinary context line" % i for i in (2997, 2998, 2999)])

    def test_skill_form_with_the_values_claude_code_substitutes(self):
        with Host(self, "ork-native-capture-env-", "Bash") as host:
            data = host.config / "plugins/data/ork-meter-inline"
            capture = "bash %s --script scripts/command.py --plugin-data %s" % (
                shlex.quote(str(host.plugin / "hooks/py.sh")), shlex.quote(str(data)))
            seen, session_id, stores = self.session(host, host.env(), capture)
            # (2) Claude Code exports neither plugin variable to Bash commands.
            self.assertEqual((seen["plain"]["env"]["CLAUDE_PLUGIN_ROOT"], seen["plain"]["env"]["CLAUDE_PLUGIN_DATA"]),
                             (None, None), "Claude Code now exports plugin variables to Bash: revisit the capture skill")
            self.assert_capture(seen, session_id, stores)

    def test_benchmark_form_with_the_variables_exported_to_the_cli(self):
        with Host(self, "ork-native-capture-env-exported-", "Bash") as host:
            data = host.config / "plugins/data/ork-meter-inline"
            env = host.env(CLAUDE_PLUGIN_ROOT=str(host.plugin), CLAUDE_PLUGIN_DATA=str(data),
                           CLAUDE_CODE_TMPDIR=str(host.scratch))
            seen, session_id, stores = self.session(
                host, env, 'bash "$CLAUDE_PLUGIN_ROOT/hooks/py.sh" --script scripts/command.py')
            # (2) Exported to the CLI, both reach its Bash commands unchanged.
            self.assertEqual(seen["plain"]["env"]["CLAUDE_PLUGIN_ROOT"], str(host.plugin))
            self.assertEqual(seen["plain"]["env"]["CLAUDE_PLUGIN_DATA"], str(data))
            self.assertEqual(seen["plain"]["env"]["CLAUDE_CODE_TMPDIR"], str(host.scratch))
            self.assert_capture(seen, session_id, stores)


if __name__ == "__main__":
    unittest.main()
