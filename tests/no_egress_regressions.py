"""The plugin's own code sends nothing (#44): a static scan plus a canary run.

The only network path in a shipped plugin is the bundled runtime
(runtime/<os>-<arch>/ork-meter-plugin-runtime, executed by scripts/runtime.sh
and hooks/session-start.sh), which is ork-meter's code and is qualified there
and in tests/native_canary_qualification.py. Everything else, the Python hooks
and scripts and the shell wrappers, must stay offline:

* an AST scan of hooks/**/*.py and scripts/*.py rejects network modules and
  process launches whose literal argv names a network tool;
* a token scan of hooks/*.sh and scripts/*.sh rejects network tools and
  bash's /dev/tcp;
* the files the runtime reads from the plugin ($XDG_STATE_HOME/ork-meter:
  plugin-features.json and session-features/*.json, docs/token-optimization.md
  "Active-lever file") keep exactly their contract keys, and nothing the hooks
  write under $XDG_STATE_HOME/ork-meter holds conversation content, after the
  hooks have run on payloads full of canary strings.

A new finding fails. A legitimate exception goes in an allowlist below with
its reason, so every exception is reviewed once and stays visible.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import support  # noqa: E402,F401  (scrubs ORK_METER_*; puts hooks/lib on sys.path)
import plus_policy_sign  # noqa: E402
import features  # noqa: E402

NETWORK_MODULES = ("socket", "ssl", "http", "http.client", "http.server", "urllib", "urllib.request",
                   "requests", "httpx", "aiohttp", "ftplib")
NETWORK_TOOLS = ("curl", "wget", "nc", "ssh")
LAUNCHERS = {("subprocess", name) for name in ("run", "call", "check_call", "check_output", "Popen", "getoutput",
                                                "getstatusoutput")}
LAUNCHERS |= {("os", name) for name in ("system", "popen", "execv", "execve", "execvp", "execvpe", "execl", "execlp",
                                         "execle", "execlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl",
                                         "spawnlp", "posix_spawn", "posix_spawnp")}

# (relative path, module) -> why it is not egress. Empty today: no hook or
# script imports a network module. ``urllib.parse`` is pure string handling
# and is not in NETWORK_MODULES, so it needs no entry.
ALLOWED_IMPORTS = {}

# (relative path, tool) -> why the launch is not egress. Empty today: no
# hook or script launches curl, wget, nc or ssh. Launches with a computed
# argv (hooks/lib/process_tree.py runs the command the user's tool call
# names; scripts/command.py the user's own command; scripts/build_runtime.py
# and scripts/package_runtime.py the developer's ``go`` toolchain at release
# build time) carry no literal network tool and are the caller's own action.
ALLOWED_LAUNCHES = {}

# (relative path, token) -> why the shell text is not egress. Empty today:
# scripts/runtime.sh only verifies and execs the bundled runtime, and
# hooks/session-start.sh only runs scripts/runtime.sh.
ALLOWED_SHELL = {}

SHELL_PATTERN = re.compile(r"(?<![\w.-])(curl|wget|nc|ssh)(?![\w.-])|/dev/(tcp|udp)/")


def python_sources():
    return sorted(set((ROOT / "hooks").rglob("*.py")) | set((ROOT / "scripts").glob("*.py")))


def shell_sources():
    return sorted(set((ROOT / "hooks").glob("*.sh")) | set((ROOT / "scripts").glob("*.sh")))


def relative(path):
    return path.relative_to(ROOT).as_posix()


def network_module(name):
    """The NETWORK_MODULES entry ``name`` is or is inside, else None."""
    if not name:
        return None
    for module in sorted(NETWORK_MODULES, key=len, reverse=True):
        if name == module or name.startswith(module + "."):
            # urllib.parse / urllib.error are string and exception helpers.
            if module == "urllib" and name.split(".")[:2] in (["urllib", "parse"], ["urllib", "error"]):
                return None
            return module
    return None


def import_findings(path, tree):
    found = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module] + ["%s.%s" % (node.module, alias.name) for alias in node.names]
            if node.module == "urllib":
                names = ["urllib." + alias.name for alias in node.names]
        elif (isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
              and (getattr(node.func, "id", None) == "__import__" or getattr(node.func, "attr", None) == "import_module")
              and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            names = [node.args[0].value]
        modules = {network_module(name) for name in names} - {None}
        for module in sorted(modules):
            if (relative(path), module) not in ALLOWED_IMPORTS:
                found.append("%s:%d imports %s" % (relative(path), node.lineno, module))
    return found


def literal_strings(node):
    """Every string constant inside ``node`` (argv lists, f-string parts)."""
    return [item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)]


def launcher(func):
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr)
    return None


def launch_findings(path, tree):
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or launcher(node.func) not in LAUNCHERS:
            continue
        words = []
        for argument in list(node.args) + [keyword.value for keyword in node.keywords]:
            for text in literal_strings(argument):
                words += re.split(r"[\s/\\'\";|&()]+", text)
        for tool in NETWORK_TOOLS:
            if tool in words and (relative(path), tool) not in ALLOWED_LAUNCHES:
                found.append("%s:%d %s.%s runs %s" % ((relative(path), node.lineno) + launcher(node.func) + (tool,)))
    return found


def shell_findings(path, name=None):
    """Network tools in the code (not comments) of a shell file."""
    name = name or relative(path)
    found = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#!") else ""
        for match in SHELL_PATTERN.finditer(code):
            token = match.group(1) or "/dev/" + match.group(2)
            if (name, token) not in ALLOWED_SHELL:
                found.append("%s:%d uses %s" % (name, number, token))
    return found


class StaticScan(unittest.TestCase):
    def test_sources_exist(self):
        names = {relative(path) for path in python_sources()}
        self.assertIn("hooks/trim-tool-output.py", names)
        self.assertIn("hooks/lib/session.py", names)
        self.assertIn("scripts/statusline.py", names)
        self.assertIn("scripts/runtime.sh", {relative(path) for path in shell_sources()})

    def test_python_imports_no_network_module(self):
        found = []
        for path in python_sources():
            found += import_findings(path, ast.parse(path.read_text(encoding="utf-8"), str(path)))
        self.assertEqual(found, [], "a hook or script imports a network module; allowlist it with a reason or remove it")

    def test_python_launches_no_network_tool(self):
        found = []
        for path in python_sources():
            found += launch_findings(path, ast.parse(path.read_text(encoding="utf-8"), str(path)))
        self.assertEqual(found, [], "a hook or script launches a network tool")

    def test_shell_uses_no_network_tool(self):
        found = []
        for path in shell_sources():
            found += shell_findings(path)
        self.assertEqual(found, [], "a shell hook or script uses a network tool")

    def test_runtime_sh_only_execs_the_bundled_runtime(self):
        text = (ROOT / "scripts/runtime.sh").read_text(encoding="utf-8")
        execs = [line.strip() for line in text.splitlines() if re.match(r"\s*exec\b", line)]
        self.assertEqual(execs, ['exec "$binary" "$@" --data-dir "$data"'])
        self.assertEqual([line for line in text.splitlines() if line.startswith("binary=")],
                         ['binary="$destination/$name"'])

    def test_allowlists_name_real_files(self):
        for path, _ in list(ALLOWED_IMPORTS) + list(ALLOWED_LAUNCHES) + list(ALLOWED_SHELL):
            self.assertTrue((ROOT / path).is_file(), "stale allowlist entry " + path)

    def test_scanners_catch_what_they_claim(self):
        """Self-test: each rule fires on a planted example."""
        fake = ROOT / "hooks/planted.py"
        samples = {
            "import socket": 1, "import http.client": 1, "from urllib import request": 1,
            "from urllib.request import urlopen": 1, "import requests as r": 1, "import aiohttp": 1,
            "__import__('ftplib')": 1, "importlib.import_module('httpx')": 1,
            "from urllib.parse import quote": 0, "import json": 0,
        }
        for source, expected in samples.items():
            self.assertEqual(len(import_findings(fake, ast.parse(source))), expected, source)
        launches = {
            "subprocess.run(['curl', '-s', url])": 1, "subprocess.Popen('wget ' + url, shell=True)": 1,
            "os.system('nc host 80')": 1, "subprocess.call(['/usr/bin/ssh', host])": 1,
            "subprocess.run([sys.executable, '-c', 'pass'])": 0, "subprocess.run(argv)": 0,
            "subprocess.run(['sync'])": 0,
        }
        for source, expected in launches.items():
            self.assertEqual(len(launch_findings(fake, ast.parse(source))), expected, source)
        shell = Path(tempfile.mkdtemp(prefix="ork-no-egress-")) / "planted.sh"
        try:
            for source, expected in {"curl -s x": 1, "exec 3<>/dev/tcp/h/80": 1, "x=$(wget -q y)": 1, "ssh h": 1,
                                     "# curl in a comment": 0, "ncpus=4": 0, "sync": 0, "echo func": 0}.items():
                shell.write_text(source + "\n", encoding="utf-8")
                self.assertEqual(len(shell_findings(shell, "planted.sh")), expected, source)
        finally:
            shutil.rmtree(shell.parent, ignore_errors=True)


# --- what the runtime reads from the plugin ----------------------------------

CONTRACT_KEYS = ["features", "plugin_version", "updated_at"]


def shell():
    """The bash Claude Code runs hooks with, or None. On Windows a bare
    "bash" is System32's WSL launcher (exit 1 and no stderr without a
    distribution), so it is Git Bash, as in tests/recovery_regressions.py."""
    if os.name != "nt":
        return "bash"
    path = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or "C:/Program Files/Git/bin/bash.exe"
    return path if os.path.isfile(path) else None


SHELL = shell()


@unittest.skipUnless(SHELL, "Git Bash is required to run the hooks on Windows")
class StateFilesCarryNoContent(unittest.TestCase):
    """Run the hooks on canary payloads, then read every file they left
    under $XDG_STATE_HOME/ork-meter (the runtime reads that directory)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ork-no-egress-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        suffix = secrets.token_hex(6)
        self.canaries = {name: "CANARY_%s_%s" % (name, suffix)
                         for name in ("PROMPT", "TOOLOUT", "PATH", "REPO", "CWD", "ERROR", "TRANSCRIPT")}
        self.work = self.tmp / self.canaries["REPO"] / self.canaries["CWD"]
        (self.work / self.canaries["PATH"]).mkdir(parents=True)
        self.note = self.work / self.canaries["PATH"] / "notes.txt"
        self.note.write_text("".join("%s line %04d of the notes file\n" % (self.canaries["TOOLOUT"], i)
                                     for i in range(1200)), encoding="utf-8")
        self.scratch = self.tmp / "scratchpad"
        self.scratch.mkdir()
        self.transcript = self.tmp / (self.canaries["TRANSCRIPT"] + ".jsonl")
        self.transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": self.canaries["PROMPT"]}}) + "\n")
        home = self.tmp / "home"
        home.mkdir()
        data = home / ".plugin-data"
        data.mkdir(mode=0o700)
        plus_policy_sign.install(str(data))   # a signed Plus envelope, so the levers run
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("ORK_METER_", "ANTHROPIC_", "CLAUDE_"))}
        self.env.update(HOME=str(home), XDG_STATE_HOME=str(self.tmp / "state"), CLAUDE_PLUGIN_DATA=str(data),
                        TMPDIR=str(self.tmp / "tmpdir"), TEMP=str(self.tmp / "tmpdir"), TMP=str(self.tmp / "tmpdir"),
                        CLAUDE_PROJECT_DIR=str(self.work), ORK_METER_CONTEXT_HYGIENE="on")
        (self.tmp / "tmpdir").mkdir()

    def payload(self, event, **extra):
        body = {"cwd": str(self.work), "hook_event_name": event, "permission_mode": "default",
                "prompt_id": "11111111-1111-1111-1111-111111111111", "scratchpad_dir": str(self.scratch),
                "session_id": "7d3f6c1e-5b8a-4e2f-9c0d-1a2b3c4d5e6f", "transcript_path": str(self.transcript)}
        body.update(extra)
        return body

    def hook(self, script, body):
        completed = subprocess.run([SHELL, (ROOT / "hooks/py.sh").as_posix(), script], input=json.dumps(body),
                                   env=self.env, cwd=self.work, capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, script + ": " + completed.stderr[-500:])
        return completed.stdout

    def test_state_files_keep_contract_keys_and_no_canary(self):
        big = "".join("%s output line %04d padding padding padding\n" % (self.canaries["TOOLOUT"], i) for i in range(900))
        for source in ("startup", "resume", "clear", "compact"):
            self.hook("trim-session.py", self.payload("SessionStart", source=source))
        prompt = "Please look at %s and %s" % (self.canaries["PROMPT"], self.note)
        self.hook("context-nudge.py", self.payload("UserPromptSubmit", prompt=prompt))
        self.hook("stale-context.py", self.payload("UserPromptSubmit", prompt=prompt))
        read_input = {"file_path": str(self.note)}
        self.hook("read-guard.py", self.payload("PreToolUse", tool_name="Read", tool_input=read_input,
                                                tool_use_id="toolu_canary_pre"))
        read_response = {"type": "text", "file": {"filePath": str(self.note), "content": self.note.read_text(),
                                                  "numLines": 1200, "startLine": 1, "totalLines": 1200}}
        for number in (1, 2):
            self.hook("dedupe-reads.py", self.payload("PostToolUse", tool_name="Read", tool_input=read_input,
                                                      tool_response=read_response, tool_use_id="toolu_canary_read_%d" % number))
        command = "cat %s/notes.txt # %s" % (self.canaries["PATH"], self.canaries["PROMPT"])
        trimmed = self.hook("trim-tool-output.py", self.payload(
            "PostToolUse", tool_name="Bash", tool_input={"command": command, "description": self.canaries["PROMPT"]},
            tool_response={"stdout": big, "stderr": self.canaries["ERROR"], "interrupted": False, "isImage": False},
            tool_use_id="toolu_canary_bash", duration_ms=12))
        self.assertIn("Full output:", trimmed, "the canary run must exercise the trim lever (entitled)")
        for number in (1, 2, 3):
            self.hook("loop-guard.py", self.payload("PostToolUseFailure", tool_name="Bash", tool_input={"command": command},
                                                    error="Exit code 1\n" + self.canaries["ERROR"], is_interrupt=False,
                                                    tool_use_id="toolu_canary_fail_%d" % number))
        self.hook("compact-records.py", self.payload("PostToolUse", tool_name="mcp__canary__lookup",
                                                     tool_input={"q": self.canaries["PROMPT"]},
                                                     tool_response=[{"type": "text", "text": json.dumps(
                                                         [{"id": i, "v": self.canaries["TOOLOUT"]} for i in range(400)])}],
                                                     tool_use_id="toolu_canary_mcp"))
        self.hook("trim-session.py", self.payload("PostCompact", trigger="auto", compact_summary=self.canaries["PROMPT"]))
        state = self.tmp / "state/ork-meter"
        global_file = state / "plugin-features.json"
        session_files = sorted((state / features.SESSION_FEATURES_DIR).glob("*.json"))
        self.assertTrue(global_file.is_file())
        self.assertEqual(len(session_files), 1, "one session wrote its own lever file")
        self.hook("session-summary.py", self.payload("SessionEnd", reason="other"))
        for path in [global_file] + sorted((state / features.SESSION_FEATURES_DIR).glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(doc), CONTRACT_KEYS, path)
            self.assertTrue(set(doc["features"]) <= set(features.ALL_FEATURES), path)
            self.assertEqual(doc["plugin_version"], features.plugin_version(), path)
            self.assertRegex(doc["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(session_files[0].name,
                         hashlib.sha256(b"7d3f6c1e-5b8a-4e2f-9c0d-1a2b3c4d5e6f").hexdigest() + ".json")
        scanned = self.assert_no_canary(state)
        self.assertGreaterEqual(scanned, 2)
        # The full output is kept locally, in the session store only.
        copies = [path for path in self.tmp.rglob("*") if path.is_file() and path != self.note
                  and self.canaries["TOOLOUT"].encode() in path.read_bytes()]
        self.assertTrue(copies, "the trimmed Bash output is saved in the session store")
        for path in copies:
            self.assertTrue(self.scratch in path.parents, "full output outside the session store: %s" % path)

    def assert_no_canary(self, directory):
        scanned = 0
        for path in sorted(directory.rglob("*")):
            name = path.relative_to(directory).as_posix()
            self.assertNotIn("CANARY", name.upper(), "a state file name carries content: " + name)
            if path.is_file():
                scanned += 1
                raw = path.read_bytes()
                for canary in self.canaries.values():
                    self.assertNotIn(canary.encode(), raw, "%s holds %s" % (name, canary))
                self.assertNotIn(str(self.work).encode(), raw, "%s holds the work path" % name)
        return scanned


if __name__ == "__main__":
    unittest.main(verbosity=2)
