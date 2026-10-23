"""Isolated installed-CLI host for lifecycle qualification (#205).

One ``Host`` is a private temporary tree: a copy of the plugin, its own
HOME, CLAUDE_CONFIG_DIR, XDG_STATE_HOME and TMPDIR, a scripted localhost
inference fixture and an HTTP(S) proxy that refuses and records every
request. ``NO_PROXY`` covers only 127.0.0.1 and localhost, so any request
the CLI makes to another host reaches the refusing proxy and fails the test
(``assert_no_external_traffic``). Credentials are a fixed fake key.

The fixture answers each model request with ``script(body, number)``: a list
of blocks, ``("text", str)`` or ``("tool", name, input)``. Several tool
blocks in one answer are concurrent calls in one turn. Tool ids are
``toolu_<request>_<index>``. ``usage(number)`` may report a different
``input_tokens`` (a context near the window triggers auto-compaction).

A suite with its own request handler passes it as ``inference``; ``Fixture``
below is the shared one that answers with a fixed list of tool calls.
``scrubbed_environ`` is the one environment filter for every installed-host
suite: no credential, endpoint, proxy or Ork Meter setting of the caller's
reaches the CLI.
"""
from http.server import BaseHTTPRequestHandler
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import LocalHTTPServer, ROOT  # noqa: E402  also puts hooks/lib on sys.path
import entitled  # noqa: E402
import session  # noqa: E402

LOCAL = "127.0.0.1,localhost"
PROXY_STALL = 30
SCRUBBED_PREFIXES = ("ANTHROPIC_", "OTEL_", "ORK_METER_", "CLAUDE_CODE_")
SCRUBBED = frozenset(("CLAUDECODE", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA",
                      "NODE_EXTRA_CA_CERTS"))
PROXIES = frozenset(("http_proxy", "https_proxy", "all_proxy", "no_proxy"))


def scrubbed_environ(environ=None):
    """``environ`` (default ``os.environ``) without real credentials or
    endpoints (every ``ANTHROPIC_*`` and ``CLAUDE_CODE_*``, so
    ``CLAUDE_CODE_OAUTH_TOKEN`` too), telemetry, proxies, Ork Meter settings
    or the outer session's plugin variables."""
    environ = os.environ if environ is None else environ
    return {key: value for key, value in environ.items()
            if not key.startswith(SCRUBBED_PREFIXES) and key not in SCRUBBED and key.lower() not in PROXIES}


def answer(handler, body, message_id, blocks, input_tokens=17):
    """Stream one assistant message to ``handler``'s client as server-sent
    events. ``blocks``: ``("text", str)`` or ``("tool", name, input, id)``."""
    events = [{"type": "message_start", "message": {
        "id": message_id, "type": "message", "role": "assistant",
        "model": body.get("model"), "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": 0, "cache_read_input_tokens": 3,
                  "cache_creation_input_tokens": 4}}}]
    uses_tool = False
    for index, block in enumerate(blocks):
        if block[0] == "text":
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block[1]}
        else:
            uses_tool = True
            start = {"type": "tool_use", "id": block[3], "name": block[1], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block[2])}
        events += [{"type": "content_block_start", "index": index, "content_block": start},
                   {"type": "content_block_delta", "index": index, "delta": delta},
                   {"type": "content_block_stop", "index": index}]
    events += [{"type": "message_delta", "delta": {"stop_reason": "tool_use" if uses_tool else "end_turn",
                "stop_sequence": None}, "usage": {"output_tokens": 7}},
               {"type": "message_stop"}]
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    for event in events:
        handler.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
    handler.wfile.flush()


def count_tokens(handler):
    """Answer a ``count_tokens`` request; True if this was one."""
    if "count_tokens" not in handler.path:
        return False
    handler.send_response(200)
    handler.end_headers()
    handler.wfile.write(b'{"input_tokens":100}')
    return True


class Fixture(BaseHTTPRequestHandler):
    """Localhost inference that answers request N with ``calls[N-1]``, one
    tool call ``(name, input)`` with id ``native_N``, and with the text
    QUALIFIED once the calls run out. An input may be a function of the
    request body, called when that request arrives. Set ``calls`` and reset
    ``requests`` and ``arrivals`` (their monotonic times) before each run."""
    requests = []
    calls = []
    arrivals = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if count_tokens(self):
            return
        self.requests.append(body)
        self.arrivals.append(time.monotonic())
        number = len(self.requests)
        if number <= len(self.calls):
            name, input_ = self.calls[number - 1]
            block = ("tool", name, input_(body) if callable(input_) else input_, "native_%d" % number)
        else:
            block = ("text", "QUALIFIED")
        answer(self, body, "msg_native_%d" % number, [block])


def latest_tool_results(body):
    """Find the latest tool results before any appended environment message."""
    for message in reversed(body.get("messages", [])):
        content = message.get("content", [])
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        results = [entry for entry in content if isinstance(entry, dict) and entry.get("type") == "tool_result"]
        if results:
            return results
    return []


def latest_tool_delivery(body):
    """Include sibling/following hook context for the latest tool result."""
    messages = body.get("messages", [])
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        content = message.get("content", [])
        if message.get("role") == "user" and isinstance(content, list) and any(
                isinstance(entry, dict) and entry.get("type") == "tool_result" for entry in content):
            return messages[index:]
    return []


HOOK_CONTENT_KEYS = ("stdout", "stderr", "output", "content", "message", "additionalContext", "updatedToolOutput")


def hook_evidence(stream, arrivals=()):
    """Classify a missing reduction without echoing tool content (#140).

    ``stream`` is the host's stream-json stdout (``--include-hook-events``).
    Hook events keep their scalar fields; content-bearing fields become their
    lengths. ``arrivals`` are the fixture's request times, so the gaps are
    tool-plus-hook wall time per call: a gap near the hooks.json timeout
    points at the host killing the hook, an empty clean reply well under it
    at the hook's own deadline or token gate."""
    events = []
    for line in stream.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or "hook" not in str(item.get("subtype", "")):
            continue
        if "PostToolUse" not in json.dumps(item):
            continue
        summary = {}
        for key, value in item.items():
            if key in HOOK_CONTENT_KEYS or isinstance(value, (dict, list)):
                summary[key + "_chars"] = len(value if isinstance(value, str) else json.dumps(value))
            elif key not in ("uuid", "session_id"):
                summary[key] = value
        events.append(summary)
    gaps = [round(later - earlier, 2) for earlier, later in zip(arrivals, arrivals[1:])]
    return "hook evidence: " + json.dumps({"request_gaps_seconds": gaps, "post_tool_use_hook_events": events})[:4000]


def tool_id(request, index):
    return "toolu_%d_%d" % (request, index)


def call_sha(identifier):
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()


def tool_results(body):
    """tool_use_id -> tool_result block, for every result in a request."""
    found = {}
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    found[block["tool_use_id"]] = block
    return found


def result_text(block):
    content = block.get("content")
    if isinstance(content, list):
        return "".join(item.get("text", "") for item in content if isinstance(item, dict))
    return content if isinstance(content, str) else json.dumps(content)


def text_after(body, identifier):
    """The tool result plus everything after it in the same request: hook
    ``additionalContext`` arrives as a following block or message."""
    messages = body.get("messages", [])
    for index, message in enumerate(messages):
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(block, dict) and block.get("tool_use_id") == identifier
                                             for block in content):
            return json.dumps(messages[index:])
    return ""


def hook_names(stream):
    names = []
    for line in stream.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("subtype") == "hook_started":
            names.append(item.get("hook_name"))
    return names


def configured_hooks(hook_name):
    """The hooks.json commands (with their timeouts) a ``hook_name`` such as
    ``SessionStart:startup`` or ``PostToolUse:Bash`` can stand for. The CLI's
    hook events name no command, so a failure lists these candidates; the CLI
    starts them in this order (observed with 2.1.302)."""
    event, _, value = str(hook_name).partition(":")
    groups = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"].get(event, [])
    return ["%s (timeout %ss)" % (hook["command"], hook.get("timeout")) for group in groups
            if not value or re.fullmatch(group.get("matcher") or ".*", value)
            for hook in group["hooks"]]


def assert_hooks_fail_open(test, stream):
    """Every hook response the CLI reported exited 0 with outcome success.
    A host cancel is not accepted: every hook's timeout is 5 s, so one that
    still runs into it is a regression or a stalled runner worth seeing.
    A failure names the hook by its place among the hooks started together
    for one event, which is its place in ``configured_hooks``."""
    position, batch = {}, (None, 0)
    for line in stream.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("subtype") == "hook_started":
            batch = (item.get("hook_name"), batch[1] + 1 if batch[0] == item.get("hook_name") else 1)
            position[item.get("hook_id")] = batch[1]
        if item.get("subtype") == "hook_response":
            stdout = str(item.get("stdout") or "")
            test.assertEqual((item.get("exit_code"), item.get("outcome")), (0, "success"),
                             "%s hook #%s started together, stdout %d chars %r, stderr %r; configured: %s" % (
                                 item.get("hook_name"), position.get(item.get("hook_id"), "?"),
                                 len(stdout), stdout[:200], str(item.get("stderr") or "")[:500],
                                 "; ".join(configured_hooks(item.get("hook_name")))))


def compact_boundaries(stream):
    """The CLI's ``compact_boundary`` events, as their ``compact_metadata``."""
    found = []
    for line in stream.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("subtype") == "compact_boundary":
            found.append(item.get("compact_metadata") or {})
    return found


def stream_session_ids(stream):
    ids = []
    for line in stream.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("subtype") == "init" and item.get("session_id") not in ids:
            ids.append(item.get("session_id"))
    return ids


class Host:
    def __init__(self, test, prefix, tools, inference=None, scratch="tmp", runtime=False, credential_store="isolated"):
        """``inference``: a request handler class to serve instead of the
        scripted one; ``scratch``: TMPDIR's path under the host's root.
        ``runtime``: also copy runtime/, so scripts/runtime.sh runs the
        shipped binary (the copy has no .git, as an installed plugin). The
        runtime then reads the OS credential store: env() points it at an
        empty, private one where the platform allows.
        ``credential_store="inherit"`` keeps the caller's D-Bus session, for
        the connected suite's job-private gnome-keyring on Linux CI only."""
        self.test = test
        self.runtime = runtime
        self.credential_store = credential_store
        # How the proxy answers (tests/native_failure_qualification.py):
        # "refuse" 403 at once, "slow" the same after PROXY_STALL seconds,
        # "5xx" 502. Every request is recorded in ``refused`` whatever the mode.
        self.proxy_mode = "refuse"
        self.prefix = prefix
        self.tools = tools
        self.handler = inference
        self.scratch_path = scratch
        self.script = lambda body, number: [("text", "QUALIFIED")]
        self.usage = lambda number: 17
        self.requests = []
        # Monotonic arrival time of each entry in ``requests``, for hook_evidence.
        self.arrivals = []
        self.paths = []
        self.refused = []
        self.pages = []
        self.page = b""
        self.lock = threading.Lock()
        self.servers = []

    # --- lifecycle --------------------------------------------------------

    def __enter__(self):
        self.binary = shutil.which("claude")
        self.test.assertTrue(self.binary, "installed claude executable is required")
        self.base = Path(tempfile.mkdtemp(prefix=self.prefix))
        self.plugin, self.work, self.config, self.shim, self.scratch, self.home = [
            self.base / name for name in ("plugin", "work", "config", "bin", self.scratch_path, "home")]
        for directory in (".claude-plugin", "hooks", "scripts", "skills", "agents") + (("runtime",) if self.runtime else ()):
            shutil.copytree(ROOT / directory, self.plugin / directory, ignore=shutil.ignore_patterns("__pycache__"))
        for directory in (self.work, self.config, self.shim, self.scratch, self.home):
            Path(session.fs_path(directory)).mkdir(parents=True)
        (self.config / ".claude.json").write_text('{"hasCompletedOnboarding":true}')
        self.settings = self.base / "settings.json"
        self.settings.write_text(json.dumps({"enabledPlugins": {}, "permissions": {"defaultMode": "dontAsk"},
                                             "skipWebFetchPreflight": True}))
        wrapper = self.shim / "python3"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable.replace("\\", "/")) + ' "$@"\n')
        wrapper.chmod(0o700)
        self.inference = self._serve(self.handler or self._inference_handler())
        self.proxy = self._serve(self._proxy_handler())
        return self

    def __exit__(self, *exc):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self._clean_host_temp()
        # Remove only a tree this host made directly under the temp directory.
        if self.base.resolve().parent != Path(tempfile.gettempdir()).resolve() or not self.base.name.startswith(self.prefix):
            raise ValueError("invalid fixture cleanup target: %s" % self.base)
        shutil.rmtree(session.fs_path(self.base), ignore_errors=True)
        return False

    def _serve(self, handler, context=None):
        server = LocalHTTPServer(("127.0.0.1", 0), handler)
        if context is not None:
            server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server

    def _clean_host_temp(self):
        """Claude Code keeps background task output and scratchpads under
        ``<system tmp>/claude-<uid>/<sanitized cwd>`` whatever TMPDIR says.
        Remove only the directory named for this host's private work path."""
        if not hasattr(os, "getuid"):
            return
        names = {re.sub(r"[^A-Za-z0-9]", "-", path) for path in (str(self.work), os.path.realpath(self.work))}
        for root in {"/tmp", "/private/tmp", tempfile.gettempdir()}:
            for name in names:
                target = Path(root) / ("claude-%d" % os.getuid()) / name
                if target.is_dir() and self.base.name.replace("_", "-") in target.name:
                    shutil.rmtree(target, ignore_errors=True)

    # --- servers ----------------------------------------------------------

    def _inference_handler(self):
        host = self

        class Inference(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                host.paths.append(("GET", self.path))
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                host.paths.append(("POST", self.path))
                if count_tokens(self):
                    return
                with host.lock:
                    host.requests.append(body)
                    host.arrivals.append(time.monotonic())
                    number = len(host.requests)
                    blocks = host.script(body, number)
                    tokens = host.usage(number)
                blocks = [block if block[0] == "text" else tuple(block[:3]) + (tool_id(number, index),)
                          for index, block in enumerate(blocks)]
                answer(self, body, "msg_lifecycle_%d" % number, blocks, tokens)
        return Inference

    def _proxy_handler(self):
        host = self

        class Refuse(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def refuse(self):
                host.refused.append((self.command, self.path))
                mode = host.proxy_mode
                if mode == "slow":
                    time.sleep(PROXY_STALL)
                self.send_response(502 if mode == "5xx" else 403)
                self.send_header("Content-Length", "0")
                self.end_headers()
            do_GET = do_POST = do_PUT = do_HEAD = do_CONNECT = do_DELETE = do_PATCH = do_OPTIONS = refuse
        return Refuse

    def serve_https_page(self, body):
        """An HTTPS page on 127.0.0.1 with a throwaway certificate the CLI
        trusts through NODE_EXTRA_CA_CERTS. WebFetch upgrades http:// to
        https:// even for 127.0.0.1 (observed with 2.1.302), so plain HTTP
        never reaches the server. Returns the page URL."""
        openssl = shutil.which("openssl")
        self.test.assertTrue(openssl, "openssl is required to make the localhost certificate")
        certs = self.base / "certs"
        certs.mkdir()
        config = certs / "openssl.cnf"
        config.write_text("[req]\nprompt = no\ndistinguished_name = dn\nx509_extensions = ext\n"
                          "[dn]\nCN = 127.0.0.1\n"
                          "[ext]\nbasicConstraints = critical,CA:TRUE\nkeyUsage = digitalSignature,keyEncipherment,keyCertSign\n"
                          "extendedKeyUsage = serverAuth\nsubjectAltName = IP:127.0.0.1,DNS:localhost\n")
        self.cert, key = certs / "cert.pem", certs / "key.pem"
        subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-config", str(config),
                        "-keyout", str(key), "-out", str(self.cert)], check=True, capture_output=True, timeout=60)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert), str(key))
        host = self
        self.page = body

        class Page(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                host.pages.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(host.page)))
                self.end_headers()
                self.wfile.write(host.page)
        server = self._serve(Page, context)
        return "https://127.0.0.1:%d/qualification/page.html" % server.server_port

    # --- the CLI ----------------------------------------------------------

    def env(self, mode="reduce", **extra):
        """The CLI's environment: ``scrubbed_environ()``, the fake key and
        local endpoints, private state directories, and ``extra``. With
        ``mode=None``, ORK_METER_MODE is left to the plugin's default."""
        env = scrubbed_environ()
        proxy = "http://127.0.0.1:%d" % self.proxy.server_port
        if mode is not None:
            env["ORK_METER_MODE"] = mode
        env.update(ANTHROPIC_API_KEY="fixture-local-only",
                   ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % self.inference.server_port,
                   CLAUDE_CONFIG_DIR=entitled.claude_config(self.config), HOME=str(self.home),
                   XDG_STATE_HOME=str(self.base / "state"),
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
                   DISABLE_AUTOUPDATER="1", HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy,
                   NO_PROXY=LOCAL, no_proxy=LOCAL,
                   TMPDIR=str(self.scratch), TEMP=str(self.scratch), TMP=str(self.scratch),
                   PATH=str(self.shim) + os.pathsep + env.get("PATH", ""))
        if os.name == "nt":
            env["USERPROFILE"] = str(self.home)
            env["CLAUDE_CODE_GIT_BASH_PATH"] = "C:/Program Files/Git/bin/bash.exe"
        if getattr(self, "cert", None):
            env["NODE_EXTRA_CA_CERTS"] = str(self.cert)
        if self.runtime and self.credential_store != "inherit":
            # The runtime's credential store (zalando/go-keyring): macOS
            # /usr/bin/security searches the keychains of $HOME, which is
            # private and empty here (assert_credential_store_isolated checks); Linux's Secret
            # Service is reached over D-Bus, pointed at a socket that does not
            # exist. Windows Credential Manager cannot be redirected; a lookup
            # of this host's unique, never-created item reads nothing.
            env.update(DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(self.base / "no-dbus"),
                       XDG_RUNTIME_DIR=str(self.scratch))
            env.pop("DISPLAY", None)
            env.pop("WAYLAND_DISPLAY", None)
        env.update(extra)
        return env

    def command(self, *extra, persist=False, stream_input=False, system="Use the local fixture tools.", plugin=True,
                setting_sources="", restricted=True):
        """The CLI's argv; ``plugin=False`` runs the stock host, without the
        plugin. ``system=None`` keeps the CLI's default system prompt, and a
        host made with ``tools=None`` its default tools."""
        command = [self.binary, "--print", "--verbose", "--output-format", "stream-json", "--include-hook-events",
                   "--setting-sources", setting_sources, "--settings", str(self.settings),
                   "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'] + (["--restricted"] if restricted else [])
        if self.tools is not None:
            command += ["--tools", self.tools, "--allowedTools", self.tools]
        command += ["--permission-mode", "dontAsk", "--model", "claude-opus-5-5"]
        if system is not None:
            command += ["--system-prompt", system]
        if plugin:
            command += ["--plugin-dir", str(self.plugin)]
        if stream_input:
            command[2:2] = ["--input-format", "stream-json"]
        if not persist:
            command.append("--no-session-persistence")
        return command + list(extra)

    def run(self, prompt, *extra, env=None, persist=False, timeout=120, **command):
        completed = subprocess.run(self.command(*extra, persist=persist, **command), input=prompt, cwd=self.work,
                                   env=env or self.env(), capture_output=True, text=True, encoding="utf-8",
                                   timeout=timeout)
        self.test.assertEqual(completed.returncode, 0, completed.stdout[-3000:] + completed.stderr[-1000:])
        return completed

    @property
    def plugin_data(self):
        """Where Claude Code puts this plugin's data for ``--plugin-dir``
        runs (tests/entitled.py claude_config)."""
        name = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))["name"]
        return self.config / "plugins" / "data" / (name + "-inline")

    def warm_runtime(self, env=None):
        """Install the shipped runtime into ``plugin_data`` before any timed
        run, untimed: ``scripts/runtime.sh --plugin-data … status`` verifies,
        copies and runs it once (not connected, status reads only the
        isolated credential store and sends nothing). A cold install costs
        seconds on Windows (the first exec of a freshly written binary, #140),
        which a hook's 5 s timeout must not absorb in every test; the cold
        path has its own test (tests/native_runtime_install_qualification.py).
        Returns the seconds it took."""
        self.test.assertTrue(self.runtime, "warm_runtime needs Host(runtime=True)")
        self.assert_credential_store_isolated()
        env = env or self.env()
        started = time.monotonic()
        completed = subprocess.run([bash(env), (self.plugin / "scripts/runtime.sh").as_posix(), "--plugin-data",
                                    str(self.plugin_data), "status"], cwd=self.work, env=env, capture_output=True,
                                   text=True, timeout=120)
        elapsed = time.monotonic() - started
        self.test.assertEqual(completed.returncode, 0, completed.stderr[-500:])
        self.test.assertTrue(list((self.plugin_data / "runtime").glob("*/ork-meter-plugin-runtime*")),
                             "the warm-up installed the shipped runtime")
        return elapsed

    def warm_hooks(self, env=None):
        """Compile the Python hooks' bytecode into ``plugin_data``'s cache
        before any timed run, untimed. hooks/py.sh keeps every module's
        bytecode, the standard library's included, in
        $CLAUDE_PLUGIN_DATA/pycache, so a fresh data dir means compiling
        dozens of modules inside the first session's hooks while
        SessionStart's other hooks start (hook #3, trim-session.py, was
        cancelled at its 5 s timeout on Windows CI, #140). Each py.sh hook in
        hooks.json runs once with an empty payload, which imports its modules
        and does nothing else. Returns the seconds it took."""
        Path(session.fs_path(self.plugin_data)).mkdir(mode=0o700, parents=True, exist_ok=True)
        env = dict(env or self.env(), CLAUDE_PLUGIN_DATA=str(self.plugin_data))
        scripts = sorted({command.split("py.sh\" ", 1)[1].strip()
                          for groups in json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"].values()
                          for group in groups for hook in group["hooks"]
                          for command in [hook["command"]] if "py.sh\" " in command})
        self.test.assertIn("trim-session.py", scripts)
        started = time.monotonic()
        for script in scripts:
            completed = subprocess.run([bash(env), (self.plugin / "hooks/py.sh").as_posix(), script], input="{}",
                                       cwd=self.work, env=env, capture_output=True, text=True, timeout=120)
            self.test.assertEqual(completed.returncode, 0, script)
        self.test.assertTrue(list(Path(session.fs_path(self.plugin_data / "pycache")).rglob("*.pyc")),
                             "the warm-up filled the hooks' bytecode cache")
        return time.monotonic() - started

    def warm(self):
        """``warm_runtime`` and ``warm_hooks``: the data dir of a session
        that has run before."""
        return self.warm_runtime() + self.warm_hooks()

    def assert_credential_store_isolated(self):
        """Before the shipped runtime runs: on macOS its keychain lookups
        (``/usr/bin/security``) must search only this host's empty HOME and
        the read-only system keychain, never the developer's login keychain."""
        if sys.platform != "darwin":
            return
        import pwd
        real = pwd.getpwuid(os.getuid()).pw_dir
        listed = subprocess.run(["/usr/bin/security", "list-keychains"], env=self.env(), capture_output=True,
                                text=True, timeout=30).stdout
        self.test.assertNotIn(real, listed, "the runtime would search the developer's keychains")

    def assert_no_external_traffic(self):
        self.test.assertEqual(self.refused, [], "the CLI tried to reach a non-local host")
        self.test.assertTrue(all(path.startswith("/v1/messages") for _, path in self.paths), self.paths)

    # --- plugin state -----------------------------------------------------

    def stores(self):
        """session id -> plugin session store, in either layout (hooks/lib/session.py)."""
        candidates = list(Path(session.fs_path(self.scratch)).rglob(session.ORK_DIR))
        if hasattr(os, "getuid"):
            for root in {"/tmp", "/private/tmp", tempfile.gettempdir()}:
                for name in {re.sub(r"[^A-Za-z0-9]", "-", path) for path in (str(self.work), os.path.realpath(self.work))}:
                    project = Path(root) / ("claude-%d" % os.getuid()) / name
                    if project.is_dir():
                        candidates += list(project.glob("*/scratchpad/" + session.ORK_DIR))
        found = {}
        for path in candidates:
            if not path.is_dir():
                continue
            store = session.Store(str(path))
            info = session.read_json(store.policy_path, {}) or session.read_json(store.session_path, {})
            sid = info.get("session_id") if isinstance(info, dict) else None
            if sid:
                found[sid] = store
        return found


def bash(env=None):
    """The bash Claude Code runs hooks with: Git Bash on Windows (a bare
    "bash" there is System32's WSL launcher, exit 1 without a distribution),
    as Host.env names it in CLAUDE_CODE_GIT_BASH_PATH."""
    if os.name != "nt":
        return "bash"
    return (env or {}).get("CLAUDE_CODE_GIT_BASH_PATH") or "C:/Program Files/Git/bin/bash.exe"


def claude_version():
    binary = shutil.which("claude")
    if not binary:
        return "unknown"
    return subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()


def wait_for(predicate, timeout=60, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
