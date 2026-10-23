"""Shared setup for the Python regression tests (#193).

Importing this module:

* puts ``tests/`` and ``hooks/lib/`` on ``sys.path``, so a test imports the
  hook libraries and the test fixtures (``entitled``, ``host_retry``) by name;
* removes the developer's ``ORK_METER_*`` settings from ``os.environ`` (#133),
  so a file run on its own (``python3 -I -B tests/x_regressions.py``) is as
  clean as under ``tests/run.sh``, whose shell scrub does the same.

It also holds the module loader for hooks and scripts (their file names have
dashes, so they can't be imported) and the hook payload and context builders.
"""
import contextlib
from http.server import ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "tests", ROOT / "hooks/lib"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import hook  # noqa: E402
import session  # noqa: E402


def clean_environ(environ=None):
    """A copy of ``environ`` (default ``os.environ``) without ORK_METER_*."""
    environ = os.environ if environ is None else environ
    return {key: value for key, value in environ.items() if not key.startswith("ORK_METER_")}


for _name in [name for name in os.environ if name.startswith("ORK_METER_")]:
    del os.environ[_name]


def git_bash():
    """The shell Claude Code's Bash tool uses, or None if it is missing. On
    Windows that is Git Bash: a bare "bash" resolves to System32's WSL
    launcher first, which exits 1 without output on a runner with no distro."""
    if os.name != "nt":
        return "bash"
    path = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or "C:/Program Files/Git/bin/bash.exe"
    return path if os.path.isfile(path) else None


class LocalHTTPServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that skips HTTPServer.server_bind's
    socket.getfqdn(host): on GitHub's macOS runners the reverse lookup of
    127.0.0.1 takes about 35 s per process (#140). Nothing here reads
    server_name."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def load(relative, name=None):
    """Execute ``ROOT/relative`` as a new module and return it. Each call is a
    fresh copy, not registered in ``sys.modules``. The default name is the
    path with ``/``, ``-`` and ``.`` turned into ``_``."""
    name = name or relative.replace("/", "_").replace("-", "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_like_bash_tool(argv, **options):
    """``subprocess.run`` with stdout and stderr in regular files, as Claude
    Code's Bash tool gives a command (2.1.302): scripts/command.py reduces
    only then. Returns both streams as bytes."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        result = subprocess.run(argv, stdout=out, stderr=err, **options)
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(argv, result.returncode, out.read(), err.read())


def payload(scratch, tool, tool_input=None, response=None, **fields):
    """A tool hook payload in ``scratch``: session ``session``, call ``id``.
    ``fields`` add keys or override these; tool_input and tool_response are
    left out when None."""
    value = {"session_id": "session", "scratchpad_dir": str(scratch), "tool_name": tool, "tool_use_id": "id"}
    if tool_input is not None:
        value["tool_input"] = tool_input
    if response is not None:
        value["tool_response"] = response
    value.update(fields)
    return value


def context(payload, env=None, event="PostToolUse", seconds=10):
    """A ``hook.Context`` for ``payload`` with ``seconds`` of budget left."""
    return hook.Context({} if env is None else env, payload, event, time.monotonic() + seconds)


def run_hook(payload, handler, events=("PostToolUse",), env=None, raw=False, **kwargs):
    """Run ``hook.run`` as the hook process does: ``payload`` as JSON on
    stdin, and for the call ``os.environ`` without any ORK_METER_* plus
    ``env``, restored after it. Returns the reply parsed (None if the hook
    printed nothing), or its text with ``raw``."""
    captured = io.StringIO()
    with patch.dict(os.environ, dict(clean_environ(), **(env or {})), clear=True), \
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(captured):
        hook.run(events, handler, **kwargs)
    text = captured.getvalue()
    if raw:
        return text
    return json.loads(text) if text.strip() else None


class StoreCase(unittest.TestCase):
    """A private temporary ``self.root``, ``self.scratch`` under it, and the
    plugin store in that scratchpad (``self.store``, not created)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scratch = self.root / "scratch"
        self.store = session.Store.for_scratchpad(str(self.scratch))


# Release SBOM fixtures (#31) -------------------------------------------------

GO_SENTINEL_START = b"0w\xaf\x0c\x92t\x08\x02A\xe1\xc1\x07\xe6\xd6\x18\xe6"
GO_SENTINEL_END = b"\xf92C1\x86\x18 r\x00\x82B\x10A\x16\xd8\xf2"


def _uvarint(value):
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def fake_go_binary(body, revision="f" * 40, modified="false", deps=None, go="go1.26.9", target="linux-amd64"):
    """``body`` followed by Go 1.18+ build info in the layout the Go linker
    embeds (16-byte aligned header, inline strings), so build-release-sbom.py
    reads it as it reads a real binary. A ``revision`` or ``modified`` of None
    leaves that vcs setting out."""
    system, arch = target.split("-")
    deps = deps if deps is not None else [("golang.org/x/sys", "v0.47.0", "h1:o7XGOvZQCADBQQ4Y7VNq2dRWQR7JmOUW8Kxx4ZsNgWs=")]
    lines = ["path\tgithub.com/EDOS-Engineering/ork-meter/cmd/claude-plugin-runtime",
             "mod\tgithub.com/EDOS-Engineering/ork-meter\tv0.0.0-20261016214347-ffffffffffff\t"]
    lines += ["dep\t%s\t%s\t%s" % dep for dep in deps]
    lines += ["build\t-trimpath=true", "build\tCGO_ENABLED=0", f"build\tGOARCH={arch}", f"build\tGOOS={system}",
              "build\tvcs=git"]
    lines += [f"build\tvcs.revision={revision}"] if revision is not None else []
    lines += [f"build\tvcs.modified={modified}"] if modified is not None else []
    mod = GO_SENTINEL_START + ("\n".join(lines) + "\n").encode() + GO_SENTINEL_END
    head = body + b"\0" * (-len(body) % 16)
    header = b"\xff Go buildinf:" + bytes([8, 2]) + b"\0" * 16
    return head + header + _uvarint(len(go)) + go.encode() + _uvarint(len(mod)) + mod


def sbom_plugin_tree(root):
    """The files build-release-sbom.py reads from a plugin tree, with a tiny
    vocabulary and a token_gate.py that pins it."""
    import gzip
    import hashlib
    raw = b"IQ== 0\nIg== 1\n"
    data = root / "hooks/lib/data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "o200k_base.tiktoken.gz").write_bytes(gzip.compress(raw, mtime=0))
    (data / "LICENSE-tiktoken.txt").write_text("MIT License\n")
    (root / "hooks/lib/token_gate.py").write_text('VERSION = "0.14.0"\nVOCAB_SHA256 = "%s"\n' % hashlib.sha256(raw).hexdigest())
    (root / "requirements-hooks.in").write_text("# direct\ntiktoken==0.14.0\n")
    (root / "requirements-hooks.txt").write_text(hook_lock())


# What pip_report() resolves: (name, version), each with its wheel's hash
# (hashlib_hex(name + version)) in hook_lock().
LOCKED = (("idna", "3.20"), ("regex", "2026.9.29"), ("requests", "2.34.2"), ("tiktoken", "0.14.0"))


def hook_lock(locked=LOCKED):
    """A requirements-hooks.txt in uv pip compile --generate-hashes layout: each
    package with its report wheel's hash and an sdist hash."""
    out = ["# Generated by .github/scripts/lock-hook-requirements.py from requirements-hooks.in; do not edit."]
    for name, version in locked:
        out += [f"{name}=={version} \\", f"    --hash=sha256:{hashlib_hex(name + version)} \\",
                f"    --hash=sha256:{hashlib_hex(name + version + '.tar.gz')}", "    # via tiktoken"]
    return "\n".join(out) + "\n"


def pip_report(tiktoken="0.14.0"):
    """A minimal ``pip install --dry-run --report`` for the hook_lock() requirements-hooks.txt."""
    def item(name, version, requested, requires=None, license=None):
        return {"metadata": {"name": name, "version": version, "requires_dist": requires,
                             **({"license_expression": license} if license else {})},
                "requested": requested,
                "download_info": {"url": f"https://files.pythonhosted.org/{name}-{version}-py3-none-any.whl",
                                  "archive_info": {"hashes": {"sha256": hashlib_hex(name + version)}}}}
    return {"version": "1", "pip_version": "26.2.1",
            "environment": {"python_full_version": "3.10.18", "sys_platform": "linux", "platform_machine": "x86_64"},
            "install": [item("tiktoken", tiktoken, True, ["regex", "requests", 'blobfile>=3; extra == "blobfile"']),
                        item("regex", "2026.9.29", False, None, "Apache-2.0 AND CNRI-Python"),
                        item("requests", "2.34.2", False, ["idna<4,>=2.5", 'PySocks!=1.5.7; extra == "socks"']),
                        item("idna", "3.20", False, None, "BSD-3-Clause")]}


def hashlib_hex(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()
