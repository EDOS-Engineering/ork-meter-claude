"""Installed-CLI canary proof (#44): conversation content stays local.

The real ``claude`` CLI runs the plugin as installed, with the shipped
runtime (runtime/<os>-<arch>, its SHA check passing, no .git in the copy),
against the localhost fixture in tests/native_host.py. Every child, the
runtime included, sees HTTP(S)_PROXY pointing at the refusing proxy, so any
attempt to leave localhost is recorded and fails the run.

Canaries carry one random suffix: the work dir is a git repository
CANARY_REPO_<r> with remote example.invalid/CANARY_ORG_<r>/CANARY_REPO_<r>,
a file sits at CANARY_PATH_<r>/notes.txt, the prompt holds CANARY_PROMPT_<r>,
the fixture model answers CANARY_COMPLETION_<r> and makes the model run a
Bash command printing about 26 KiB with CANARY_TOOLOUT_<r>, over the trim
budget and under the host's 30,000-character limit (a persisted result is
left alone, #351), so the trim lever saves the full output locally. A hit is the suffix in any case: Claude Code
lowercases repository names in telemetry attributes.

* Run 1, installed and not connected (the runtime installed untimed first,
  Host.warm; the cold install is native_runtime_install_qualification's):
  no external traffic; the SessionStart
  hook exits 0 with at most one line; full-output copies exist only in the
  plugin's session store (or the plugin data / state dirs), besides Claude
  Code's own local copies (its persisted tool result in its config dir and,
  on some versions, its task output under the temp dir); nothing under
  $XDG_STATE_HOME/ork-meter, entitlement.json or observed-usage-* holds a
  canary.
* Run 2, OTEL as the runtime configures it on connect
  (tests/fixtures/ork-meter-otel-env.json, from ork-meter
  internal/localcapture/plugin.go:39-61) against a capturing localhost OTLP
  sink: with the runtime's flags no metrics body holds a canary. With hostile
  project settings that turn the content flags on, the test records, and
  does not fail on, what Claude Code puts into metrics: ork-meter's receiver
  filter (internal/connectors/telemetry.go) is what drops it.

With ORK_CANARY_REPORT set (not ORK_METER_*: tests/support.py, imported by
other suites during discovery, deletes every ORK_METER_* variable), a
machine-readable evidence record is
written there (docs/release.md, "Per-release canary record"); it is always
printed to the test log.
"""
from http.server import BaseHTTPRequestHandler
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import entitled  # noqa: E402
import host_retry  # noqa: E402
sys.path.insert(0, str(ROOT / "hooks/lib"))
import session  # noqa: E402
from native_host import Host, assert_hooks_fail_open, bash, claude_version, hook_evidence, hook_names  # noqa: E402

OTEL_FIXTURE = ROOT / "tests/fixtures/ork-meter-otel-env.json"
KINDS = ("REPO", "ORG", "PATH", "PROMPT", "COMPLETION", "TOOLOUT")
EVIDENCE = {"sinks": [], "observations": []}


def runtime_target():
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system(), platform.system().lower())
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64" if machine in ("x86_64", "amd64") else machine
    return "%s-%s" % (system, arch)


def runtime_sha256():
    """(manifest digest, digest of the binary): runtime/<target>/SHA256."""
    package = ROOT / "runtime" / runtime_target()
    name = "ork-meter-plugin-runtime" + (".exe" if runtime_target().startswith("windows") else "")
    manifest = (package / "SHA256").read_text(encoding="utf-8").strip()
    return manifest, hashlib.sha256((package / name).read_bytes()).hexdigest()


def otel_env(endpoint):
    fixture = json.loads(OTEL_FIXTURE.read_text(encoding="utf-8"))
    return {key: value.replace("{endpoint}", endpoint) for key, value in fixture["env"].items()}, fixture


class Canaries:
    """One run's canary strings and the fixture session that carries them."""

    def __init__(self, host, reseed=True):
        self.suffix = secrets.token_hex(8)
        self.values = {kind: "CANARY_%s_%s" % (kind, self.suffix) for kind in KINDS}
        self.host = host
        # Claude Code's stores are keyed by the work path; set before any run.
        host.work = host.work / self.values["REPO"]
        host.work.mkdir()
        git = {key: value for key, value in host.env().items() if not key.startswith("GIT_")}
        for argv in (["git", "init", "-q"], ["git", "remote", "add", "origin", "https://example.invalid/%s/%s.git"
                                                % (self.values["ORG"], self.values["REPO"])]):
            subprocess.run(argv, cwd=host.work, env=git, check=True, capture_output=True, timeout=30)
        notes = host.work / self.values["PATH"] / "notes.txt"
        notes.parent.mkdir()
        notes.write_text("notes for the canary run\n", encoding="utf-8")
        # The canary is assembled at run time, so only the tool's output (and
        # whatever copies it) holds CANARY_TOOLOUT_<r>, not the script.
        (host.work / "emit.py").write_text(
            "import sys\nprint(open(sys.argv[1]).read().strip())\n"
            "tag = 'CANARY_' + 'TOOLOUT_' + %r\n"
            "for i in range(300):\n    print(tag, 'line %%04d' %% i, 'canary tool output padding' * 2)\n"
            % self.suffix, encoding="utf-8")
        self.command = "%s emit.py %s/notes.txt" % (shlex.quote(sys.executable.replace("\\", "/")), self.values["PATH"])
        self.reseeded = []

        def script(body, number):
            if number == 1 and reseed:
                # The real runtime, not connected, removes entitlement.json at
                # SessionStart (ork-meter cmd/claude-plugin-runtime main.go,
                # session-start: "Not connected means not entitled"; on a
                # store it cannot read it leaves it). SessionStart has finished
                # by the first model request: seed it again, as
                # tests/entitled.py does for the other suites, so trims run.
                self.reseeded.append((host.plugin_data / "entitlement.json").exists())
                entitled.write(str(host.plugin_data))
            if number == 1:
                return [("tool", "Bash", {"command": self.command, "description": "Print the canary notes"})]
            return [("text", "Done: " + self.values["COMPLETION"])]
        host.script = script

    @property
    def prompt(self):
        return "Run the notes printer. " + self.values["PROMPT"]

    def hits(self, data):
        """Canary kinds found in ``data`` (bytes or str), in any case."""
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        lowered = text.lower()
        if self.suffix not in lowered:
            return []
        found = [kind for kind in KINDS if self.values[kind].lower() in lowered]
        return found or ["SUFFIX"]


def session_start_responses(stream):
    """The hook_response events of hooks/session-start.sh (its output names
    an /ork-meter: command; when connected it prints nothing)."""
    found = []
    for line in stream.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if (isinstance(item, dict) and item.get("subtype") == "hook_response"
                and str(item.get("hook_name", "")).startswith("SessionStart")
                and "Ork Meter: " in str(item.get("stdout")) and "/ork-meter:" in str(item.get("stdout"))):
            found.append(item)
    return found


def session_start_timeout():
    """hooks.json's timeout for hooks/session-start.sh: Claude Code cancels the
    hook after it, so any bound must stay under it."""
    hooks = json.loads((Path(__file__).resolve().parents[1] / "hooks/hooks.json").read_text(encoding="utf-8"))
    return min(hook["timeout"] for group in hooks["hooks"]["SessionStart"] for hook in group["hooks"]
               if "session-start.sh" in hook["command"])


# The runtime caps its own SessionStart work at 2 s, so 3 s catches added
# plugin overhead on POSIX. A cold Windows runner spends most of a second just
# starting bash and the runtime (3.94 s seen in CI on #239), so there the bound
# is the host's own limit minus a margin: the hook must finish before Claude
# Code would cancel it.
SESSION_START_BOUND = 3.0 if os.name != "nt" else session_start_timeout() - 0.5
assert SESSION_START_BOUND < session_start_timeout(), "the bound must stay under the hook timeout"


def run_session_start(test, host, env=None, bound=SESSION_START_BOUND):
    """hooks/session-start.sh as Claude Code runs it, timed. Asserts exit 0,
    at most one line, the wall-time bound; returns (seconds, stdout)."""
    env = env or host.env()
    started = time.monotonic()
    completed = subprocess.run([bash(env), (host.plugin / "hooks/session-start.sh").as_posix(), str(host.plugin_data)],
                               input="{}", cwd=host.work, env=env, capture_output=True, text=True, timeout=30)
    elapsed = time.monotonic() - started
    test.assertEqual(completed.returncode, 0)
    lines = completed.stdout.splitlines()
    test.assertLessEqual(len(lines), 1, completed.stdout)
    if lines:
        test.assertEqual(sorted(json.loads(lines[0])), ["systemMessage"])
    test.assertLess(elapsed, bound, "SessionStart hook took %.2f s" % elapsed)
    return elapsed, completed.stdout.strip()


def temp_roots(host):
    """Claude Code's per-project system temp dirs for this host's work path."""
    roots = []
    if hasattr(os, "getuid"):
        names = {re.sub(r"[^A-Za-z0-9]", "-", path) for path in (str(host.work), os.path.realpath(host.work))}
        for base in {"/tmp", "/private/tmp", tempfile.gettempdir()}:
            for name in names:
                path = Path(base) / ("claude-%d" % os.getuid()) / name
                if path.is_dir():
                    roots.append(path)
    return roots


def under(path, root):
    """``path`` is inside ``root``, compared in one spelling
    (session.comparable_path: no Windows \\\\?\\ prefix, links and 8.3
    names resolved, case folded): the store path carries that prefix,
    the file walk does not."""
    inner, outer = session.comparable_path(path), session.comparable_path(root)
    return inner.startswith(outer.rstrip(os.sep) + os.sep)


UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def temp_bases(host):
    """The machine's temp dirs and the TMPDIR the host gives Claude Code."""
    bases = {"/tmp", "/private/tmp", tempfile.gettempdir(), str(host.scratch)}
    return {Path(os.path.realpath(base)) for base in bases} | {Path(base) for base in bases}


def claude_task_output(host, path):
    """Claude Code's own output file for a Bash call it ran as a background
    task (seen with 2.1.280): exactly
    ``<temp>/claude[-<uid>]/<sanitized work path>/<session uuid>/tasks/<id>.output``
    under the machine's temp dir or the host's TMPDIR. Host-owned and local;
    nothing else matches."""
    names = {re.sub(r"[^A-Za-z0-9]", "-", item) for item in (str(host.work), os.path.realpath(host.work))}
    for base in temp_bases(host):
        try:
            parts = path.relative_to(base).parts
        except ValueError:
            continue
        if (len(parts) == 5 and re.fullmatch(r"claude(-\d+)?", parts[0]) and parts[1] in names
                and re.fullmatch(UUID, parts[2]) and parts[3] == "tasks" and re.fullmatch(r"[A-Za-z0-9]+\.output", parts[4])):
            return True
    return False


def files_under(*roots):
    for root in roots:
        if Path(root).is_dir():
            for path in sorted(Path(root).rglob("*")):
                if path.is_file() and not path.is_symlink():
                    yield path


class Sink:
    """A capturing localhost OTLP/HTTP receiver; answers 200 ``{}``."""

    def __init__(self, host):
        self.bodies = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                sink.bodies.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        self.server = host._serve(Handler)
        self.endpoint = "http://127.0.0.1:%d/v1/metrics" % self.server.server_port

    def metric_names(self):
        names = set()
        for _, body in self.bodies:
            try:
                document = json.loads(body)
            except ValueError:
                continue
            for resource in document.get("resourceMetrics", []):
                for scope in resource.get("scopeMetrics", []):
                    names.update(metric.get("name") for metric in scope.get("metrics", []))
        return names

    def attribute_keys(self, canaries):
        """Attribute keys whose value holds a canary."""
        keys = set()

        def walk(node):
            if isinstance(node, dict):
                if "key" in node and "value" in node and canaries.hits(json.dumps(node["value"])):
                    keys.add(node["key"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        for _, body in self.bodies:
            try:
                walk(json.loads(body))
            except ValueError:
                continue
        return sorted(keys)


def record_sink(name, checked, hits, detail=None):
    entry = {"sink": name, "checked": checked, "hits": len(hits)}
    if hits:
        entry["hit_kinds"] = sorted(set(hits))
    if detail:
        entry.update(detail)
    EVIDENCE["sinks"] = [item for item in EVIDENCE["sinks"] if item["sink"] != name] + [entry]


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
class NativeCanary(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        if not EVIDENCE["sinks"]:
            return
        manifest, actual = runtime_sha256()
        fixture = json.loads(OTEL_FIXTURE.read_text(encoding="utf-8"))
        version = claude_version()
        report = {"schema_version": 1, "kind": "ork-meter-plugin-canary-record",
                  "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "claude_version": version, "platform": platform.platform(), "runtime_target": runtime_target(),
                  "runtime_sha256": manifest, "runtime_sha256_verified": manifest == actual,
                  "otel_env_source": {"ork_meter_commit": fixture["ork_meter_commit"], "source": fixture["source"]},
                  "inference": "localhost-synthetic", "sinks": sorted(EVIDENCE["sinks"], key=lambda item: item["sink"]),
                  "observations": EVIDENCE["observations"],
                  "hits": sum(item["hits"] for item in EVIDENCE["sinks"]),
                  "server_side_sink_check": "human, per release: docs/release.md (#44)"}
        print("\nCanary evidence: " + json.dumps(report, sort_keys=True), flush=True)
        target = os.environ.get("ORK_CANARY_REPORT")
        if target:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def setUp(self):
        manifest, actual = runtime_sha256()
        self.assertEqual(manifest, actual, "runtime/%s/SHA256 does not match the shipped binary" % runtime_target())

    def test_installed_not_connected_keeps_canaries_local(self):
        with Host(self, "ork-native-canary-", "Bash", runtime=True) as host:
            host.assert_credential_store_isolated()
            canaries = Canaries(host)
            # Untimed runtime install and hook bytecode first: the measured
            # run's SessionStart is warm.
            host.warm()
            completed = host.run(canaries.prompt)
            host.assert_no_external_traffic()
            record_sink("proxy (every non-local request, runtime included)", True, ["REQUEST"] * len(host.refused))
            assert_hooks_fail_open(self, completed.stdout)
            self.assertIn("PostToolUse:Bash", hook_names(completed.stdout))
            responses = session_start_responses(completed.stdout)
            self.assertEqual(len(responses), 1, "hooks/session-start.sh must answer once")
            self.assertLessEqual(len(responses[0]["stdout"].strip().splitlines()), 1)
            self.assertIn(canaries.values["COMPLETION"], completed.stdout, "the fixture answer reached the CLI")
            self.assertTrue(list((host.plugin_data / "runtime").glob("*/ork-meter-plugin-runtime*")),
                            "scripts/runtime.sh installed the shipped runtime")
            self.assertEqual(len(canaries.reseeded), 1)

            # Full-output copies: plugin locations, and Claude Code's own
            # local copies (its persisted tool result in the config dir, and
            # on some versions its background-task output in the system temp
            # dir: claude_task_output); nothing else.
            stores = [Path(store.path) for store in host.stores().values()]
            self.assertEqual(len(stores), 1)
            state = host.base / "state"
            plugin_roots = stores + [host.plugin_data, state]
            claude_owned = [host.config / "projects"]
            tool_out = canaries.values["TOOLOUT"].encode()
            copies, stray, owned, task_outputs = [], [], [], set()
            for path in files_under(host.base, *temp_roots(host)):
                if tool_out not in path.read_bytes():
                    continue
                if any(under(path, root) for root in plugin_roots):
                    copies.append(path)
                elif any(under(path, root) for root in claude_owned):
                    owned.append(path)
                elif claude_task_output(host, path):
                    task_outputs.add(os.path.realpath(path))
                else:
                    stray.append(path)
            # A missing trim is either a late hook reply (a request gap near
            # the hooks.json timeout, an empty clean reply) or no entitlement
            # when the hook ran; the reseed state and hook evidence tell them apart.
            where = ("store %s; plugin copies %s; Claude Code copies %s; task outputs %s; stray %s; "
                     "entitlement.json existed at the reseed: %s, exists now: %s; %s; delivered: %s") % (
                stores[0], copies, owned, sorted(task_outputs), stray,
                canaries.reseeded[0] if canaries.reseeded else None,
                (host.plugin_data / "entitlement.json").exists(),
                hook_evidence(completed.stdout, host.arrivals),
                json.dumps(host.requests[1])[-1500:] if len(host.requests) > 1 else None)
            self.assertTrue(any(under(path, stores[0]) for path in copies), "the trim lever saved the full output; " + where)
            self.assertEqual(stray, [], "full output outside the plugin's local stores; " + where)
            ork_state = list(files_under(state / "ork-meter"))
            self.assertTrue([path for path in ork_state if path.name == "plugin-features.json"])
            leaked = [(path, canaries.hits(path.read_bytes())) for path in ork_state]
            leaked += [(path, ["PATH NAME"]) for path in ork_state if canaries.hits(str(path.relative_to(state)))]
            leaked = [(str(path), kinds) for path, kinds in leaked if kinds]
            self.assertEqual(leaked, [], "the runtime-read state holds content")
            data_files = [path for path in files_under(host.plugin_data)
                          if path.name == "entitlement.json" or path.name.startswith("observed-usage-")]
            data_leaks = [(str(path), canaries.hits(path.read_bytes())) for path in data_files if canaries.hits(path.read_bytes())]
            self.assertEqual(data_leaks, [])
            record_sink("$XDG_STATE_HOME/ork-meter/** (features, session-features, hook state)", True,
                        [kind for _, kinds in leaked for kind in kinds], {"files": len(ork_state)})
            record_sink("CLAUDE_PLUGIN_DATA entitlement.json and observed-usage-*", True,
                        [kind for _, kinds in data_leaks for kind in kinds], {"files": len(data_files)})
            record_sink("full-output copies outside the session store, plugin data and state", True,
                        ["TOOLOUT"] * len(stray), {"plugin_copies": len(copies), "claude_code_owned_copies": len(owned),
                         "claude_code_temp_task_outputs": len(task_outputs),
                         "host_owned_local_copies": ["$CLAUDE_CONFIG_DIR/projects/<project>/<session>/tool-results/*",
                                                     "<temp>/claude-<uid>/<project>/<session>/tasks/*.output"]})

    def otel_run(self, hostile):
        with Host(self, "ork-native-canary-otel-", "Bash", runtime=True) as host:
            host.assert_credential_store_isolated()
            sink = Sink(host)
            canaries = Canaries(host)
            host.warm()
            env, fixture = otel_env(sink.endpoint)
            command = host.command()
            if hostile:
                # Where the runtime writes it on connect (ork-meter
                # internal/pluginruntime/settings.go: $CLAUDE_CONFIG_DIR/
                # settings.json), and a repository's project settings that
                # outrank it. --restricted would ignore both files; --tools
                # still names only Bash.
                (host.config / "settings.json").write_text(json.dumps({"env": env}), encoding="utf-8")
                (host.work / ".claude").mkdir()
                project = {key: value for key, value in fixture["hostile_project_env"].items() if not key.startswith("_")}
                (host.work / ".claude/settings.json").write_text(json.dumps({"env": project}), encoding="utf-8")
                command[command.index("--setting-sources") + 1] = "user,project"
                command.remove("--restricted")
            else:
                settings = json.loads(host.settings.read_text(encoding="utf-8"))
                settings["env"] = env
                host.settings.write_text(json.dumps(settings), encoding="utf-8")
            completed = subprocess.run(command, input=canaries.prompt, cwd=host.work, env=host.env(),
                                       capture_output=True, text=True, encoding="utf-8", timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stdout[-3000:] + completed.stderr[-1000:])
            host.assert_no_external_traffic()
            assert_hooks_fail_open(self, completed.stdout)
            self.assertIn("claude_code.token.usage", sink.metric_names(),
                          "the sink must receive Claude Code's metrics export, or this check proves nothing")
            hits = [kind for _, body in sink.bodies for kind in canaries.hits(body)]
            paths = sorted({path for path, _ in sink.bodies})
            return sink, canaries, hits, paths

    def test_runtime_otel_flags_export_no_canaries(self):
        sink, canaries, hits, paths = self.otel_run(hostile=False)
        record_sink("OTLP metrics bodies, runtime flags (content flags 0)", True, hits,
                    {"bodies": len(sink.bodies), "paths": paths})
        self.assertEqual(hits, [], "a canary reached Claude Code's metrics export with the runtime's flags: %s"
                         % sink.attribute_keys(canaries))
        self.assertEqual(paths, ["/v1/metrics"], "metrics only: no logs or traces")

    def test_hostile_project_flags_are_recorded(self):
        """Not a pass/fail on Claude Code: records what reaches the receiver
        when a project turns content flags on. ork-meter's receiver keeps only
        claude_code.token.usage samples (internal/connectors/telemetry.go)."""
        sink, canaries, hits, paths = self.otel_run(hostile=True)
        observation = {"case": "hostile project settings (content and attribute flags on)",
                       "otlp_paths": paths, "bodies": len(sink.bodies),
                       "canary_kinds_in_metrics": sorted(set(hits)),
                       "attribute_keys_with_canaries": sink.attribute_keys(canaries),
                       "metric_names": sorted(sink.metric_names()),
                       "dropped_by": "ork-meter receiver filter (internal/connectors/telemetry.go:52-122)"}
        EVIDENCE["observations"] = [observation]
        print("\n  OBSERVATION hostile OTEL flags: " + json.dumps(observation, sort_keys=True), flush=True)


if __name__ == "__main__":
    unittest.main()
