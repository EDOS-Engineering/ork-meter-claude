"""A finished reply is printed while it can still reach Claude Code (#140).

hooks.json gives each Python hook 5 s. hook.run used to drop a reply that was
already computed whenever its 1.5 s planning budget had passed, so a slow
Windows runner lost trims that would still have arrived in time. The emit
deadline counts from hooks/py.sh's launch when it passes a plausible launch
time, else from run.py's start with a wider margin.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, clean_environ, git_bash, run_hook  # noqa: E402
import entitled  # noqa: E402
entitled.install()
import hook  # noqa: E402

BIG = "".join("line %05d of verbose build output that nobody needs to read\n" % i for i in range(1500))


class EmitLimit(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.now = 1000.0

    def clock(self):
        return self.now

    def run_hook(self, finished_after, pass_started=True, launch_age=None):
        """Run a hook whose handler finishes ``finished_after`` seconds after the process started."""
        started = self.now
        def handler(ctx):
            self.now = started + finished_after
            return hook.updated_tool_output(ctx.event, {"stdout": "line 00000 ... (trimmed)\n", "stderr": "",
                                                       "interrupted": False, "isImage": False})
        return self.run_handler(handler, started if pass_started else None, launch_age)

    def run_handler(self, handler, started, launch_age=None):
        payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "emit-limit",
                   "scratchpad_dir": self.temporary.name, "tool_use_id": "call-1", "prompt_id": "p",
                   "tool_input": {"command": "make"},
                   "tool_response": {"stdout": BIG, "stderr": "", "interrupted": False, "isImage": False}}
        # started=None, for in-process callers: the clock starts when run() is entered.
        with patch.object(hook.time, "monotonic", self.clock):
            return run_hook(payload, handler, started=started, launch_age=launch_age)

    def test_reply_finished_after_the_planning_budget_is_still_printed(self):
        late = hook.DEFAULT_BUDGET + 0.1          # 1.6 s: past the budget, well before the host's 2 s
        reply = self.run_hook(late, pass_started=False)
        self.assertIsNotNone(reply, "a finished reply that can still arrive in time must be printed")
        self.assertIn("updatedToolOutput", reply["hookSpecificOutput"])

    def test_reply_past_the_emit_limit_and_the_budget_is_dropped(self):
        self.assertIsNone(self.run_hook(hook.EMIT_LIMIT + 0.05))

    def test_slow_imports_never_shorten_the_budget(self):
        # Windows CI: imports (tiktoken, the vocabulary) can take a large part
        # of the 1.8 s counted from process start. A reply finished within the
        # budget counted from the hook's own start must still be printed.
        started = self.now - 1.0                  # run.py started 1 s before hook.run was entered
        def handler(ctx):
            self.now += hook.DEFAULT_BUDGET - 0.1 # finishes inside the budget, 2.4 s after process start
            return hook.updated_tool_output(ctx.event, {"stdout": "line 00000 ... (trimmed)\n", "stderr": "",
                                                       "interrupted": False, "isImage": False})
        self.assertIsNotNone(self.run_handler(handler, started))

    def test_may_emit_is_the_rule_run_applies(self):
        with patch.object(hook.time, "monotonic", self.clock):
            self.check_may_emit()

    def check_may_emit(self):
        ctx = hook.Context({}, {}, "PostToolUse", self.now - 0.1)        # budget already spent
        self.assertFalse(hook.may_emit(ctx), "no start known: the budget alone decides")
        ctx.started = self.now - 1.6
        self.assertTrue(hook.may_emit(ctx))
        ctx.started = self.now - hook.EMIT_LIMIT - 0.01
        self.assertFalse(hook.may_emit(ctx))
        self.assertTrue(hook.may_emit(hook.Context({}, {}, "PostToolUse", self.now + 1)))

    def test_launch_time_extends_the_window_to_its_own_limit(self):
        # bash and the interpreter took 0.1 s: the deadline is 4.4 s after
        # run.py started, later than the 4.0 s fallback.
        age = 0.1
        self.assertIsNotNone(self.run_hook(hook.EMIT_LIMIT + 0.2, launch_age=age),
                             "a reply 4.3 s after launch is still in time")
        self.assertIsNone(self.run_hook(hook.EMIT_LIMIT_FROM_LAUNCH - age + 0.05, launch_age=age))

    def test_slow_start_up_moves_the_deadline_earlier(self):
        # A stalled runner spent 0.9 s before Python ran: 4.5 s from launch
        # is 3.6 s from run.py's start, sooner than the fallback would allow.
        age = 0.9
        self.assertIsNotNone(self.run_hook(hook.EMIT_LIMIT_FROM_LAUNCH - age - 0.05, launch_age=age))
        self.assertIsNone(self.run_hook(hook.EMIT_LIMIT_FROM_LAUNCH - age + 0.05, launch_age=age))

    def test_launch_time_never_drops_a_reply_inside_the_budget(self):
        self.assertIsNotNone(self.run_hook(hook.DEFAULT_BUDGET - 0.1, launch_age=hook.LAUNCH_MAX_AGE))

    def test_without_a_launch_time_the_limit_counts_from_run_py(self):
        self.assertIsNotNone(self.run_hook(hook.EMIT_LIMIT - 0.05, launch_age=None))
        self.assertIsNone(self.run_hook(hook.EMIT_LIMIT + 0.05, launch_age=None))
        self.assertEqual(hook.emit_deadline(10.0), 10.0 + hook.EMIT_LIMIT)

    def test_launch_age_accepts_only_a_plausible_launch_time(self):
        now = 1_760_000_000.25
        self.assertAlmostEqual(hook.launch_age("1760000000.000000", now), 0.25)
        self.assertAlmostEqual(hook.launch_age("1760000000,000000", now), 0.25, msg="EPOCHREALTIME in a comma locale")
        self.assertAlmostEqual(hook.launch_age(" 1760000000 ", now), 0.25)
        self.assertEqual(hook.launch_age(str(now), now), 0.0)
        self.assertAlmostEqual(hook.launch_age(str(now - hook.LAUNCH_MAX_AGE), now), hook.LAUNCH_MAX_AGE)
        for value in (None, "", "abc", "1760000000.0.0", "nan", "inf", "-inf", "1e999", "0x10",
                      str(now + 0.5),                          # in the future: the clock stepped back
                      str(now - hook.LAUNCH_MAX_AGE - 0.01),   # too old: inherited from a parent shell
                      "0"):
            self.assertIsNone(hook.launch_age(value, now), value)

    def test_may_emit_with_a_launch_time(self):
        with patch.object(hook.time, "monotonic", self.clock):
            ctx = hook.Context({}, {}, "PostToolUse", self.now - 0.1, started=self.now - 4.2, launch_age=0.2)
            self.assertTrue(hook.may_emit(ctx), "4.4 s after launch")
            ctx.launch_age = 0.4
            self.assertFalse(hook.may_emit(ctx), "4.6 s after launch")
            ctx.launch_age = None
            self.assertFalse(hook.may_emit(ctx), "4.2 s after run.py started, no launch time")

    def test_emit_limit_leaves_margin_under_the_hook_timeout(self):
        timeouts = {hook_["timeout"] for group in json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"].values()
                    for entry in group for hook_ in entry["hooks"] if "py.sh" in hook_["command"]}
        # Counted from run.py's start, bash and interpreter start-up are unseen.
        self.assertLessEqual(hook.EMIT_LIMIT + 1.0, min(timeouts))
        # Counted from py.sh's first line, only the host's spawn is unseen.
        self.assertLessEqual(hook.EMIT_LIMIT_FROM_LAUNCH + 0.5, min(timeouts))
        self.assertGreater(hook.EMIT_LIMIT, hook.DEFAULT_BUDGET)
        self.assertGreater(hook.EMIT_LIMIT_FROM_LAUNCH, hook.EMIT_LIMIT)

    def test_run_py_starts_the_clock_before_its_imports(self):
        source = (ROOT / "hooks/lib/run.py").read_text()
        self.assertLess(source.index("STARTED = time.monotonic()"), source.index("import importlib.util"))
        self.assertIn("started=STARTED", source)


# A hook module that records, at import, what run.py took from the
# environment and what the hook's own environment still holds, and the
# launch_age run.py then passes to hook.run.
PROBE = """
import __main__, json, os
import hook
OUT = %r
seen = {"raw": __main__.LAUNCHED_AT, "env": os.environ.get("ORK_HOOK_LAUNCHED_AT"),
        "wall": __main__.STARTED_WALL}
def _run(*args, **kwargs):
    seen["launch_age"] = kwargs.get("launch_age")
    with open(OUT, "w") as out:
        json.dump(seen, out)
hook.run = _run
EVENTS = ("PostToolUse",)
def handle(ctx):
    return None
"""


class LaunchTime(unittest.TestCase):
    """run.py and py.sh pass the launch time to the hook runtime only."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.out = self.root / "seen.json"

    def probe(self, directory):
        path = Path(directory) / "launch-probe.py"
        path.write_text(PROBE % str(self.out), encoding="utf-8")
        return path

    def seen(self):
        return json.loads(self.out.read_text(encoding="utf-8"))

    def run_py(self, value):
        env = clean_environ()
        env.pop("ORK_HOOK_LAUNCHED_AT", None)
        if value is not None:
            env["ORK_HOOK_LAUNCHED_AT"] = value
        subprocess.run([sys.executable, "-I", "-B", str(ROOT / "hooks/lib/run.py"), str(self.probe(self.root))],
                       input='{"hook_event_name": "PostToolUse"}', env=env, capture_output=True, text=True,
                       timeout=60, check=True)
        return self.seen()

    def test_run_py_takes_the_launch_time_out_of_the_environment(self):
        seen = self.run_py("%.6f" % (time.time() - 0.5))
        self.assertIsNone(seen["env"], "no handler or child process may see the launch time")
        self.assertGreaterEqual(seen["launch_age"], 0.5)
        self.assertLess(seen["launch_age"], 0.5 + 20)

    def test_run_py_ignores_an_implausible_launch_time(self):
        for value in ("abc", "", "%.6f" % (time.time() + 60), "%.6f" % (time.time() - 3600)):
            seen = self.run_py(value)
            self.assertIsNone(seen["launch_age"], value)
            self.assertIsNone(seen["env"])

    def test_run_py_without_a_launch_time(self):
        seen = self.run_py(None)
        self.assertIsNone(seen["raw"])
        self.assertIsNone(seen["launch_age"])

    def test_py_sh_passes_its_own_launch_time_to_the_hook_only(self):
        bash = git_bash()
        if bash is None:
            self.skipTest("no Git Bash")
        hooks = self.root / "plugin/hooks"
        shutil.copytree(ROOT / "hooks", hooks, ignore=shutil.ignore_patterns("__pycache__"))
        shim = self.root / "bin"
        shim.mkdir()
        (shim / "python3").write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable.replace("\\", "/")) + ' "$@"\n')
        (shim / "python3").chmod(0o700)
        self.probe(hooks)
        env = {key: value for key, value in clean_environ().items() if key != "CLAUDE_PLUGIN_DATA"}
        env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
        env["ORK_HOOK_LAUNCHED_AT"] = "%.6f" % (time.time() - 1)   # inherited from a parent: replaced
        before = time.time()
        subprocess.run([bash, (hooks / "py.sh").as_posix(), "launch-probe.py"], input='{"hook_event_name": "PostToolUse"}',
                       env=env, capture_output=True, text=True, timeout=60, check=True)
        seen = self.seen()
        self.assertIsNone(seen["env"], "run.py removes it from the hook's environment")
        version = subprocess.run([bash, "-c", "echo ${BASH_VERSINFO[0]}"], env=env, capture_output=True, text=True,
                                 timeout=60).stdout.strip()
        if int(version) >= 5:
            launched = float(seen["raw"].replace(",", "."))
            self.assertGreaterEqual(launched, before - 0.01, "py.sh's own stamp, not the inherited one")
            self.assertLessEqual(launched, seen["wall"] + 0.01)
            self.assertIsNotNone(seen["launch_age"])
        else:
            # bash 3.2 (macOS) has no EPOCHREALTIME: nothing is passed on.
            self.assertEqual(seen["raw"], "")
            self.assertIsNone(seen["launch_age"])

    def test_py_sh_sets_the_launch_time_for_the_hook_command_only(self):
        source = (ROOT / "hooks/py.sh").read_text(encoding="utf-8")
        self.assertNotIn("export ORK_HOOK_LAUNCHED_AT", source)
        self.assertEqual(source.count("ORK_HOOK_LAUNCHED_AT="), 1)
        self.assertIn('ORK_HOOK_LAUNCHED_AT=$_ork_launched \\\n    "$hook_python" -I', source)
        self.assertEqual(hook.LAUNCH_ENV, "ORK_HOOK_LAUNCHED_AT")
        runner = (ROOT / "hooks/lib/run.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.pop("%s", None)' % hook.LAUNCH_ENV, runner)
        self.assertLess(runner.index("STARTED_WALL = time.time()"), runner.index("import importlib.util"))
        self.assertFalse(hook.LAUNCH_ENV.startswith("ORK_METER_"), "ORK_METER_* is reserved for settings")


if __name__ == "__main__":
    unittest.main(verbosity=1)
