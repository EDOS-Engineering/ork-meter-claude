"""Installed-CLI failure behaviour, not connected (#45, docs/failure-behaviour.md).

The real ``claude`` CLI and the shipped runtime (Host(runtime=True)) run
against the localhost fixture while the network the runtime would use is
broken in one of four ways, all through HTTP(S)_PROXY:

* refused: the proxy answers every request, CONNECT included, 403 at once;
* slow: the proxy holds every request for 30 s before refusing;
* offline: HTTP(S)_PROXY names a closed localhost port;
* 5xx: the proxy answers 502.

In every mode the SessionStart hook finishes in under 3 s with exit 0 and
prints nothing (hooks/session-start.sh keeps only its failure warning), the
runtime's own session start (``scripts/runtime.sh --if-installed
session-start``, whose stdout that hook discards) still tells the person to
run ``/ork-meter:connect``, every plugin hook reports exit 0,
``/ork-meter:status``
(skills/status/SKILL.md: ``scripts/runtime.sh --plugin-data … status``)
answers not-connected or unverified within its bound with the runtime's own
recovery text as its next step, and a fixed one-turn
``claude --print`` takes no more than MARGIN seconds longer than the same
run with the proxy refusing instantly (the baseline). Not connected, the
runtime has nothing to send, so these runs also show it does not try: the
proxy must see no request at all.

Connected-credential failures (401/403, expiry, replay, a slow Ork Meter
holding Claude's export) are ork-meter's and
tests/native_failure_connected_qualification.py's (Linux CI only).
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import host_retry  # noqa: E402
from native_canary_qualification import SESSION_START_BOUND, run_session_start, runtime_sha256, runtime_target  # noqa: E402
from native_host import Host, assert_hooks_fail_open, bash  # noqa: E402

MARGIN = 2.0          # seconds over the baseline one-turn run
STATUS_BOUND = 5.0
NOT_CONNECTED = {"not_connected", "connection_unverified"}
# status's next_step is the runtime's own recovery text (skills/status/SKILL.md),
# and which one depends on the isolated credential store, not on the failure
# mode: an empty, readable store (macOS keychain of the private HOME, Windows
# Credential Manager) reads not_connected; a Secret Service that cannot be
# reached (Linux, D-Bus pointed at a missing socket; docs/failure-behaviour.md
# "OS credential store locked or missing") reads connection_unverified.
NEXT_STEPS = {"Run /ork-meter:connect.",
              "Unlock the OS credential store and retry. Do not share credentials."}
TIMINGS = {}


def closed_port():
    """A localhost port nothing listens on (bound, then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
class NativeFailure(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        if TIMINGS:
            print("\nFailure-mode timings (seconds): " + json.dumps(TIMINGS, sort_keys=True), flush=True)

    def setUp(self):
        manifest, actual = runtime_sha256()
        self.assertEqual(manifest, actual, "runtime/%s/SHA256 does not match the shipped binary" % runtime_target())

    def one_turn(self, host, env):
        started = time.monotonic()
        completed = host.run("Say the word.", env=env, timeout=120)
        elapsed = time.monotonic() - started
        assert_hooks_fail_open(self, completed.stdout)
        self.assertIn("QUALIFIED", completed.stdout)
        return elapsed

    def runtime(self, host, env, *args):
        completed = subprocess.run([bash(env), (host.plugin / "scripts/runtime.sh").as_posix(), "--plugin-data", str(host.plugin_data)]
                                   + list(args), cwd=host.work, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr[-500:])
        return completed.stdout

    def session_start_notice(self, host, env):
        """What the runtime prints at session start, read from the runtime
        itself: hooks/session-start.sh sends its stdout to /dev/null, so the
        hook can no longer show it (as Connected.session_start_notice in
        tests/native_failure_connected_qualification.py)."""
        return self.runtime(host, env, "--if-installed", "session-start").strip()

    def status(self, host, env):
        started = time.monotonic()
        stdout = self.runtime(host, env, "status")
        elapsed = time.monotonic() - started
        report = json.loads(stdout)
        self.assertIn(report.get("state"), NOT_CONNECTED, report)
        self.assertEqual(report.get("authorization"), "unverified", report)
        self.assertIn(report.get("next_step"), NEXT_STEPS, report)
        self.assertLess(elapsed, STATUS_BOUND, "status took %.2f s" % elapsed)
        return elapsed, report["state"]

    def check(self, mode):
        with Host(self, "ork-native-failure-", "Read", runtime=True) as host:
            host.assert_credential_store_isolated()
            # Warm-up: install the runtime and compile the hooks' bytecode
            # into the plugin data dir untimed (#140), then one run; none of
            # it belongs in the timings.
            host.warm()
            self.one_turn(host, host.env())
            baseline = self.one_turn(host, host.env())
            env = host.env()
            if mode == "offline":
                proxy = "http://127.0.0.1:%d" % closed_port()
                env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy)
            else:
                host.proxy_mode = mode
            hook_seconds, message = run_session_start(self, host, env=env, bound=SESSION_START_BOUND)
            # hooks/session-start.sh starts silently (the runtime's notices are
            # discarded); the runtime itself still says how to get connected,
            # whatever the credential store reads (docs/failure-behaviour.md).
            self.assertEqual(message, "", "not connected, SessionStart prints nothing")
            self.assertIn("/ork-meter:connect", self.session_start_notice(host, env))
            measured = self.one_turn(host, env)
            status_seconds, state = self.status(host, env)
            TIMINGS[mode] = {"baseline_print": round(baseline, 2), "print": round(measured, 2),
                             "session_start_hook": round(hook_seconds, 2), "status": round(status_seconds, 2),
                             "status_state": state}
            print("\n  %s: %s" % (mode, json.dumps(TIMINGS[mode], sort_keys=True)), flush=True)
            # Not connected, the runtime and the hooks send nothing in any mode.
            host.assert_no_external_traffic()
            self.assertLessEqual(measured, baseline + MARGIN,
                                 "%s: one-turn run %.2f s vs baseline %.2f s" % (mode, measured, baseline))

    def test_refused(self):
        self.check("refuse")

    def test_slow(self):
        self.check("slow")

    def test_offline(self):
        self.check("offline")

    def test_5xx(self):
        self.check("5xx")


if __name__ == "__main__":
    unittest.main()
