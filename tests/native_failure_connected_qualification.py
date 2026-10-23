"""Connected failure modes (#45, docs/failure-behaviour.md). Linux CI only.

The real ``claude`` CLI and the shipped runtime, paired for real with a fake
Ork Meter (tests/fake_platform.py): ``connect`` opens its setup page through
an ``xdg-open`` shim that records the URL; the test then plays the person
(plan and permission on the loopback page, approval on the "hosted" page)
and posts the claim back to the loopback, and the runtime claims its grant
from the fake. The runtime reaches the fake as it would reach orkmeter.com:
HTTPS through HTTPS_PROXY, a TLS-terminating proxy whose throwaway CA the
``connected-failure`` CI job trusts on its runner only. Its credentials live
in a gnome-keyring inside that job's private D-Bus session.

This suite refuses to run unless ORK_CONNECTED_FAILURE_CI=1 (set by
that job only), on Linux, with the CA the job made: it must never run on a
workstation, whose trust store and keyring it would otherwise need.

Modes, after a successful pairing and a baseline session: up; down (CONNECT
refused); slow (every reply held 30 s); 5xx; 429; 403 (revoked); a grant
expiring, then expired; renewal, extended and refused; a lost reply after
commit (the replay must reuse the same observation ID and event ID, and the
fake keeps one copy). Every request the runtime
makes is captured and scanned for the canaries of tests/native_canary_
qualification.py; the result goes into the evidence JSON
(ORK_CONNECTED_REPORT, and the test log).

The runtime built from ork-meter cb4b2b9 fixed the four gaps this suite
first recorded as expected failures, and these tests now assert the fixes:
a 429's Retry-After is honoured and a permanently rejected event is set aside
instead of blocking the queue (#357, for #355); Claude's export is
acknowledged at once and delivered in the background, so a slow or failing
Ork Meter adds nothing to a session's exit (#359, for #356); status names a
refused credential (authorization_refused) and an expiring or expired one,
and session start says so once (#357, #358, for #354); a credential with
fewer than seven days left is renewed in the background and a refused
renewal is never retried (#362, for #353).

PINNED_RUNTIME_SHA256 pins the runtime this suite qualified. When runtime/
changes, re-run the job and update the pin.
"""
import datetime
import html
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import fake_platform  # noqa: E402
import host_retry  # noqa: E402
from native_canary_qualification import SESSION_START_BOUND, Canaries, run_session_start, runtime_sha256, runtime_target  # noqa: E402
from native_host import Host, assert_hooks_fail_open, claude_version, wait_for  # noqa: E402

# Candidate pins are specific to the plugin version. Keep the installed release
# and the workflow-built next release explicit so this test can land on main
# before the generated, immutable release branch is merged. Unknown versions
# retain an empty pin set and fail admission in setUp.
RUNTIME_CANDIDATES = {
    "0.5.2": ("cb4b2b9", {
        "linux-amd64": "7a20d9e2426f957703a3774b4013fb90dd28c97e85757484d8ad3e7c770f7fb7",
        "linux-arm64": "0f9f437a5d39c554b3170fbd25b3569499b19c97aebc8c88afb29cc43d4666f2",
    }),
    "0.5.3": ("17533e6a5670862b829f2f456329789a028f95e8", {
        "linux-amd64": "78b7ea65393a7749b346660fde851c23f278d30712501e13f95c924907e5f44b",
        "linux-arm64": "3b8bc82ee059568ae7fc7a2b06aba964122be6ba6d48b45e0af38696e5431a42",
    }),
    "0.5.4": ("c1e659ace905e99bbe2de0aeb34df7209b0e1b09", {
        "linux-amd64": "d1853283ef75d62b50cb01b622f23fc70de9399c11f9c0b113378244551f395f",
        "linux-arm64": "41c4fb999bd9a7a3bb506c1bd1ff0844e7343195ed686e29427ee7b05d975d74",
    }),
}
# A -dev version on main ships the last release's runtime until the next release PR replaces runtime/.
RUNTIME_CANDIDATES["0.5.4-dev"] = RUNTIME_CANDIDATES["0.5.3"]
_PLUGIN_VERSION = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"]
RUNTIME_ORK_METER_REF, PINNED_RUNTIME_SHA256 = RUNTIME_CANDIDATES.get(
    _PLUGIN_VERSION, ("unregistered", {}))
ORIGIN = "https://orkmeter.com"
MARGIN = 2.0
# Each side of the exit bound is the best of this many one-turn sessions, so
# one slow sample on a shared runner does not decide it. A best-of compares
# like with like: an exit cost such as the 1.6-2.2 s that #359 removed is in
# every sample of the broken side and still shows.
SAMPLES = 2
STATUS_BOUND = 20.0       # the runtime's own command context
EXPORT_BOUND = 2.0
EVIDENCE = {"runs": []}
ENABLED = (os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1" and os.environ.get("ORK_CONNECTED_FAILURE_CI") == "1"
           and sys.platform.startswith("linux") and bool(os.environ.get("ORK_FAKE_PLATFORM_CA")))


def no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Connected:
    """A Host with the shipped runtime paired to a FakePlatform."""

    def __init__(self, test, grant_seconds=30 * 24 * 3600, renew="extend", issued_ago=0):
        self.test = test
        self.grant_seconds = grant_seconds
        self.renew = renew
        self.issued_ago = issued_ago

    def __enter__(self):
        self.host = Host(self.test, "ork-native-connected-", "Bash", runtime=True, credential_store="inherit").__enter__()
        try:
            self.platform = fake_platform.FakePlatform(self.grant_seconds)
            self.platform.renew = self.renew
            self.platform.issued_ago = self.issued_ago
            context = fake_platform.leaf_context(os.environ["ORK_FAKE_PLATFORM_CA"], self.host.base / "leaf")
            self.proxy = fake_platform.MitmProxy(self.platform, context)
            self.browser = self.host.base / "browser"
            self.browser.mkdir()
            self.opened = self.host.base / "opened-urls.txt"
            shim = self.browser / "xdg-open"
            shim.write_text("#!/bin/sh\nprintf '%%s\\n' \"$1\" >> %s\n" % shlex.quote(str(self.opened)))
            shim.chmod(0o700)
            self.canaries = Canaries(self.host, reseed=False)
            self.started = len(self.platform.requests)
            self.pair()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        try:
            if getattr(self, "platform", None) is not None:
                self.platform.mode = "up"
                subprocess.run(self.runtime_command("disconnect"), cwd=self.host.work, env=self.env(),
                               capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            subprocess.run(["pkill", "-f", str(self.host.plugin_data)], capture_output=True)
            if getattr(self, "proxy", None) is not None:
                self.proxy.close()
            self.host.__exit__(*exc)
        return False

    # --- environment and commands ------------------------------------------

    def env(self, **extra):
        env = self.host.env(**extra)
        env.update(HTTP_PROXY=self.proxy.url, HTTPS_PROXY=self.proxy.url, http_proxy=self.proxy.url,
                   https_proxy=self.proxy.url, PATH=str(self.browser) + os.pathsep + env["PATH"])
        return env

    def runtime_command(self, *args):
        return ["bash", str(self.host.plugin / "scripts/runtime.sh"), "--plugin-data", str(self.host.plugin_data)] + list(args)

    def runtime(self, *args, timeout=60):
        completed = subprocess.run(self.runtime_command(*args), cwd=self.host.work, env=self.env(), capture_output=True,
                                   text=True, timeout=timeout)
        return completed

    def status(self):
        started = time.monotonic()
        completed = self.runtime("status")
        elapsed = time.monotonic() - started
        self.test.assertEqual(completed.returncode, 0, completed.stderr[-500:])
        self.test.assertLess(elapsed, STATUS_BOUND, "status took %.2f s" % elapsed)
        return json.loads(completed.stdout), elapsed

    # --- pairing, driven for real ------------------------------------------

    def diagnose(self, step, completed=None):
        """Everything that explains a failed pairing: the runtime prints only
        its generic ErrSetup and has no verbose flag (cmd/claude-plugin-
        runtime main.go), so show what reached the fake and the proxy, what
        status says about the credential store, and whether the job's
        Secret Service answers at all."""
        lines = ["pairing failed at %s" % step]
        if completed is not None:
            lines.append("runtime exit %s; stdout %r; stderr %r" % (completed.returncode, completed.stdout[-800:],
                                                                  completed.stderr[-800:]))
        lines.append("fake platform requests: %s" % json.dumps(
            [(r["method"], r["host"], r["path"], r["outcome"]) for r in self.platform.requests]))
        lines.append("proxy refused (non-platform): %s" % self.platform.refused)
        try:
            status = self.runtime("status", timeout=30)
            lines.append("status: %s %s" % (status.stdout.strip()[:600], status.stderr.strip()[-300:]))
        except (OSError, subprocess.SubprocessError) as error:
            lines.append("status failed: %r" % error)
        env = self.env()
        lines.append("DBUS_SESSION_BUS_ADDRESS set: %s" % bool(env.get("DBUS_SESSION_BUS_ADDRESS")))
        try:
            probe = subprocess.run(["secret-tool", "store", "--label=ork-meter-ci-probe", "service", "ork-meter-ci-probe",
                                    "account", "probe"], input="probe", env=env, capture_output=True, text=True, timeout=20)
            looked = subprocess.run(["secret-tool", "lookup", "service", "ork-meter-ci-probe", "account", "probe"],
                                    env=env, capture_output=True, text=True, timeout=20)
            lines.append("secret-tool store exit %s %r; lookup exit %s %r" % (
                probe.returncode, probe.stderr.strip()[-300:], looked.returncode, looked.stdout.strip()[:20]))
        except (OSError, subprocess.SubprocessError) as error:
            lines.append("secret-tool unavailable: %r" % error)
        lines.append("opened URLs: %r" % (self.opened.read_text() if self.opened.is_file() else None))
        return "\n  ".join(lines)

    def pair(self):
        connect = self.runtime("connect")
        if connect.returncode != 0 or '"setup_opened"' not in connect.stdout:
            self.test.fail(self.diagnose("connect", connect))
        if not wait_for(lambda: self.opened.is_file() and self.opened.read_text().strip(), timeout=10):
            self.test.fail(self.diagnose("xdg-open (connect must open the setup page through the shim)"))
        setup = self.opened.read_text().split()[0]
        found = re.match(r"^(http://127\.0\.0\.1:\d+)/setup/([a-f0-9]{64})$", setup)
        self.test.assertTrue(found, setup)
        local, nonce = found.groups()
        opener = no_proxy_opener()
        form = {"Origin": local, "Content-Type": "application/x-www-form-urlencoded"}
        page = opener.open(urllib.request.Request(local + "/prepare/" + nonce, data=b"plan=pro-max&permitted=yes",
                                                  headers=form, method="POST"), timeout=30).read().decode()
        link = re.search(r'href="(https://[^"]+/pair#[^"]+)"', page)
        self.test.assertTrue(link, page[-800:])
        approval = urllib.parse.urlsplit(html.unescape(link.group(1)))
        fragment = urllib.parse.parse_qs(approval.fragment)
        origin = "%s://%s" % (approval.scheme, approval.netloc)
        self.test.assertEqual(origin, ORIGIN)
        proposal, return_url = fragment["proposal"][0], fragment["return_url"][0]
        code = self.platform.approve(origin, proposal, return_url)
        self.reference = json.loads(proposal)["id"]
        body = urllib.parse.urlencode({"reference": self.reference, "claim_code": code}).encode()
        done = opener.open(urllib.request.Request(return_url, data=body, headers=form, method="POST"), timeout=30).read().decode()
        if "Connected" not in done or self.reference not in self.platform.grants:
            self.test.fail(self.diagnose("claim; loopback page: %r" % re.sub(r"<[^>]+>", " ", done)[-600:]))
        settings = json.loads((self.host.config / "settings.json").read_text(encoding="utf-8"))
        self.test.assertIn("otelHeadersHelper", settings)
        self.endpoint = settings["env"]["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"]
        self.test.assertRegex(self.endpoint, r"^http://127\.0\.0\.1:\d+/v1/metrics$")

    # --- sessions ----------------------------------------------------------

    def session(self, prompt=None):
        """One ``claude --print`` with the runtime's user settings (export
        through otelHeadersHelper to the receiver). Returns seconds."""
        command = self.host.command()
        command[command.index("--setting-sources") + 1] = "user"
        command.remove("--restricted")   # it would ignore the user settings the runtime wrote
        started = time.monotonic()
        completed = subprocess.run(command, input=prompt or self.canaries.prompt, cwd=self.host.work, env=self.env(),
                                   capture_output=True, text=True, encoding="utf-8", timeout=180)
        elapsed = time.monotonic() - started
        self.test.assertEqual(completed.returncode, 0, completed.stdout[-3000:] + completed.stderr[-1000:])
        assert_hooks_fail_open(self.test, completed.stdout)
        return elapsed

    def timed(self, prompt="Say the word.", samples=SAMPLES):
        """The best of ``samples`` one-turn sessions, in seconds."""
        return min(self.session(prompt) for _ in range(samples))

    def baseline(self, upload=True):
        """Pairing done: one canary session (the Bash tool, full output),
        then the timed one-turn baseline with Ork Meter up. ``upload=False``
        skips waiting for the snapshot upload, for a grant too short to
        afford it."""
        self.session()
        if upload:
            self.test.assertTrue(wait_for(lambda: self.platform.observations, timeout=30),
                                 "the receiver uploads a snapshot")
        return self.timed("Say the word. " + self.canaries.values["PROMPT"])

    def probe_export(self):
        """A minimal Claude-shaped token export straight to the receiver,
        authenticated as Claude's otelHeadersHelper would. Returns (status, s)."""
        headers = json.loads(self.runtime("headers").stdout)
        nanos = str(time.time_ns())
        body = {"resourceMetrics": [{"resource": {"attributes": []}, "scopeMetrics": [{
            "scope": {"name": "com.anthropic.claude_code"}, "metrics": [{
                "name": "claude_code.token.usage", "unit": "tokens", "sum": {
                    "aggregationTemporality": 2, "isMonotonic": True, "dataPoints": [{
                        "attributes": [{"key": "type", "value": {"stringValue": "input"}}],
                        "startTimeUnixNano": nanos, "timeUnixNano": nanos, "asInt": str(1000 + len(self.platform.requests))}]}}]}]}]}
        request = urllib.request.Request(self.endpoint, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **headers})
        started = time.monotonic()
        try:
            status = no_proxy_opener().open(request, timeout=40).status
        except urllib.error.HTTPError as error:
            status = error.code
        return status, time.monotonic() - started

    def since(self, mark):
        return self.platform.requests[mark:]

    def record(self, label, extra=None):
        hits = []
        for request in self.platform.requests:
            hits += self.canaries.hits(request["body"] + request["path"])
        run = {"case": label, "requests": len(self.platform.requests), "canary_hits": len(hits),
               "paths": sorted({"%s %s" % (r["method"], r["path"]) for r in self.platform.requests}),
               "refused_non_platform": len(self.platform.refused)}
        if hits:
            run["hit_kinds"] = sorted(set(hits))
        run.update(extra or {})
        EVIDENCE["runs"] = [item for item in EVIDENCE["runs"] if item["case"] != label] + [run]
        print("\n  %s: %s" % (label, json.dumps(run, sort_keys=True)), flush=True)
        self.test.assertEqual(hits, [], "a canary reached the platform")
        self.test.assertEqual(self.platform.refused, [], "the CLI or runtime tried to reach another host")
        self.host.assert_no_external_traffic()


def tearDownModule():
    if not EVIDENCE["runs"]:
        return
    manifest, actual = runtime_sha256()
    version = claude_version()
    report = {"schema_version": 1, "kind": "ork-meter-plugin-connected-failure-record",
              "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "claude_version": version, "runtime_target": runtime_target(), "runtime_sha256": manifest,
              "runtime_sha256_verified": manifest == actual, "inference": "localhost-synthetic",
              "platform": "tests/fake_platform.py behind a TLS-terminating proxy",
              "runs": sorted(EVIDENCE["runs"], key=lambda item: item["case"]),
              "canary_hits": sum(item["canary_hits"] for item in EVIDENCE["runs"])}
    print("\nConnected failure evidence: " + json.dumps(report, sort_keys=True), flush=True)
    target = os.environ.get("ORK_CONNECTED_REPORT")
    if target:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class ConnectedBase(unittest.TestCase):
    def setUp(self):
        manifest, actual = runtime_sha256()
        self.assertEqual(manifest, actual)
        self.assertEqual(manifest, PINNED_RUNTIME_SHA256.get(runtime_target()),
                         "runtime/ is not the runtime this suite qualifies (ork-meter %s): re-qualify it against "
                         "the new runtime and update PINNED_RUNTIME_SHA256" % RUNTIME_ORK_METER_REF)

    def assert_print_bounded(self, timings):
        """Claude's export is acknowledged at once and delivered in the
        background (ork-meter #359), so a failing Ork Meter adds nothing to a
        session's exit beyond the margin. Both sides are best-of-SAMPLES."""
        self.assertLessEqual(timings["print"], timings["baseline_print"] + MARGIN, timings)

    def assert_status(self, connected, states, authorization=None):
        report, elapsed = connected.status()
        self.assertIn(report["state"], states, report)
        if authorization:
            self.assertEqual(report["authorization"], authorization, report)
        return report, elapsed


@unittest.skipUnless(ENABLED, "Linux CI connected-failure job only (ORK_CONNECTED_FAILURE_CI=1)")
@host_retry.retry_once
class NativeConnectedFailure(ConnectedBase):
    def broken(self, connected, mode):
        """Baseline with Ork Meter up, then ``mode``: SessionStart bound,
        one-turn time against the baseline, hooks fail open."""
        baseline = connected.baseline()
        connected.platform.mode = mode
        hook, _ = run_session_start(self, connected.host, env=connected.env(), bound=SESSION_START_BOUND)
        measured = connected.timed("Say the word. " + connected.canaries.values["PROMPT"])
        return {"baseline_print": round(baseline, 2), "print": round(measured, 2), "session_start_hook": round(hook, 2)}

    # --- up ------------------------------------------------------------------

    def test_up(self):
        with Connected(self) as connected:
            baseline = connected.baseline()
            hook, message = run_session_start(self, connected.host, env=connected.env(), bound=SESSION_START_BOUND)
            self.assertEqual(message, "", "connected and healthy, SessionStart prints nothing")
            self.assertTrue(wait_for(lambda: connected.platform.events, timeout=70), "an observed-usage event arrives")
            report, status_seconds = self.assert_status(connected, {"receiving_usage"}, "verified")
            self.assertNotIn("authorization_expiry", report, "a 30-day grant is not expiring")
            self.assertEqual(report.get("last_delivery"), "acknowledged", report)
            entitlement = json.loads((connected.host.plugin_data / "entitlement.json").read_text())
            self.assertTrue(entitlement["optimize"])
            code, export_seconds = connected.probe_export()
            self.assertEqual(code, 200, "the probe export has Claude's shape and is acknowledged")
            self.assertLess(export_seconds, EXPORT_BOUND)
            connected.record("up", {"baseline_print": round(baseline, 2), "session_start_hook": round(hook, 2),
                                    "status": round(status_seconds, 2), "probe_export": round(export_seconds, 2)})

    # --- down, 5xx, 429 ------------------------------------------------------

    def test_down(self):
        with Connected(self) as connected:
            timings = self.broken(connected, "down")
            self.assert_print_bounded(timings)
            self.assert_status(connected, {"authorization_unverified"})
            stored = len(connected.platform.observations)
            connected.platform.mode = "up"
            # The pending snapshot is retried 5, 15 and 45 s after a failure.
            self.assertTrue(wait_for(lambda: len(connected.platform.observations) > stored, timeout=70),
                            "capture recovers once Ork Meter is back")
            connected.record("down", timings)

    def test_5xx(self):
        with Connected(self) as connected:
            timings = self.broken(connected, "5xx")
            self.assert_print_bounded(timings)
            self.assert_status(connected, {"authorization_unverified"})
            mark = len(connected.platform.attempts["observation"])
            self.assertTrue(wait_for(lambda: len(connected.platform.attempts["observation"]) >= mark + 1, timeout=20))
            retried = connected.platform.attempts["observation"][mark - 1:]
            self.assertEqual(len(set(retried)), 1, "a retried snapshot keeps its observation ID: %s" % retried)
            connected.record("5xx", timings)

    def test_429_retry_after_is_honoured(self):
        """ork-meter #357 (#355): a 429's Retry-After: 60 is honoured; the
        snapshot comes back later with the same observation ID."""
        with Connected(self) as connected:
            timings = self.broken(connected, "429")
            self.assert_print_bounded(timings)
            self.assertTrue(wait_for(lambda: [r for r in connected.platform.requests
                                              if r["path"] == "/v1/collectors/observations" and r["outcome"] == 429],
                                     timeout=20), "precondition: an upload was answered 429")
            first = connected.platform.attempts["observation"][-1]
            attempts = len(connected.platform.attempts["observation"])
            started = time.monotonic()
            self.assertFalse(wait_for(lambda: len(connected.platform.attempts["observation"]) > attempts, timeout=20),
                             "retried within 20 s of Retry-After: 60")
            connected.platform.mode = "up"
            self.assertTrue(wait_for(lambda: first in connected.platform.observations, timeout=90),
                            "the same snapshot is delivered after the requested delay")
            timings["retry_after_wait"] = round(time.monotonic() - started, 1)
            self.assert_status(connected, {"receiving_usage", "connected_waiting_for_usage"}, "verified")
            connected.record("429", timings)

    # --- slow ----------------------------------------------------------------

    def test_slow_ork_meter_does_not_hold_the_export(self):
        """ork-meter #359 (#356): the receiver acknowledges Claude's export at
        once and delivers in the background, so a slow Ork Meter holds nothing."""
        with Connected(self) as connected:
            timings = self.broken(connected, "slow")
            self.assert_print_bounded(timings)
            code, held = connected.probe_export()
            self.assertEqual(code, 200, "the export is acknowledged before delivery")
            self.assertLess(held, EXPORT_BOUND, "the receiver held the export %.2f s" % held)
            report, status_seconds = self.assert_status(connected, {"authorization_unverified"})
            timings.update(status=round(status_seconds, 2), probe_export=round(held, 2))
            connected.record("slow", timings)

    # --- revoked, expiring, expired, renewed --------------------------------

    def test_revoked_403(self):
        """ork-meter #357 (#354): status names a refused credential and warns
        that disconnecting deletes undelivered usage; the refused snapshot is
        not retried (localcapture capture.go: ErrDenied stops the automatic
        5/15/45 s retries) and observed delivery is parked (observed.go park)."""
        with Connected(self) as connected:
            baseline = connected.baseline()
            connected.platform.revoke_all()
            hook, _ = run_session_start(self, connected.host, env=connected.env(), bound=SESSION_START_BOUND)
            measured = connected.timed()
            self.assert_print_bounded({"baseline_print": baseline, "print": measured})

            def refused(path):
                return [r for r in connected.platform.requests if r["path"] == path and r["outcome"] == 403]
            self.assertTrue(wait_for(lambda: refused("/v1/collectors/observations"), timeout=30),
                            "precondition: a snapshot upload was refused")
            # The observed tracker delivers on its one-minute tick.
            self.assertTrue(wait_for(lambda: refused("/v1/collectors/events"), timeout=90),
                            "precondition: an observed-event upload was refused")
            attempts = list(connected.platform.attempts["observation"])
            # Past the first automatic retry (5 s) with room to spare; no new
            # export arrives, as no session runs.
            time.sleep(10)
            self.assertEqual(connected.platform.attempts["observation"], attempts,
                             "a refused snapshot is not retried automatically")
            report, _ = self.assert_status(connected, {"authorization_refused"}, "refused")
            self.assertIn("Disconnecting deletes", report["next_step"], report)
            self.assertEqual(report.get("last_delivery"), "refused", report)
            self.assertEqual(report.get("observed_delivery"), "parked", report)
            connected.record("403 revoked", {"baseline_print": round(baseline, 2), "print": round(measured, 2),
                                             "session_start_hook": round(hook, 2),
                                             "entitlement_file_after": (connected.host.plugin_data / "entitlement.json").exists()})

    def test_expiring_then_expired(self):
        """ork-meter #358 (#353, #354): an expiring grant is named in status
        and once at session start; once expired, status says so without asking
        Ork Meter, and session start says so once more. The fake keeps the
        grant (renew="keep": an account whose access ends then).

        Everything that needs the platform (status, the expiring notice) runs
        first, right after pairing; the baseline needs only the local
        receiver, so it does not wait for an upload, and a slow runner that
        overruns the grant cannot turn a precondition into a 403."""
        grant_seconds = 120
        with Connected(self, grant_seconds=grant_seconds, renew="keep") as connected:
            report, _ = self.assert_status(connected, {"connected_waiting_for_usage", "receiving_usage"}, "verified")
            self.assertEqual(report.get("authorization_expiry"), "authorization_expiring", report)
            self.assertTrue(report.get("authorization_expires_at"), report)
            self.assertIn("expires on", report["next_step"], report)
            notices = []
            for _ in range(2):
                notices.append(run_session_start(self, connected.host, env=connected.env(), bound=SESSION_START_BOUND)[1])
            baseline = connected.baseline(upload=False)
            grant = connected.platform.grants[connected.reference]
            # A baseline session's own SessionStart would otherwise take the
            # expired notice this test counts below.
            self.assertLess(fake_platform.now(), grant["expires"], "precondition: the baseline ran inside the grant")
            self.assertTrue(wait_for(lambda: fake_platform.now() >= grant["expires"], timeout=grant_seconds + 30))
            for _ in range(2):
                notices.append(run_session_start(self, connected.host, env=connected.env(), bound=SESSION_START_BOUND)[1])
            self.assertEqual(["expires on" in notice for notice in notices], [True, False, False, False],
                             "session start names the coming expiry exactly once: %r" % notices)
            self.assertEqual(["expired on" in notice for notice in notices], [False, False, True, False],
                             "and the expiry exactly once: %r" % notices)
            mark = len(connected.platform.requests)
            report, _ = self.assert_status(connected, {"authorization_expired"}, "expired")
            self.assertFalse([r for r in connected.platform.requests[mark:] if r["path"] == "/v1/collectors/native-status"],
                             "an expired credential is not offered to Ork Meter")
            measured = connected.timed()
            self.assert_print_bounded({"baseline_print": baseline, "print": measured})
            connected.record("expired grant", {"baseline_print": round(baseline, 2), "print": round(measured, 2)})

    def test_renewal_extends_a_credential_near_expiry(self):
        """ork-meter #362 (#353): fewer than seven days left, the detached
        entitlement refresh renews the credential to 30 days ahead. The
        platform renews only once a day has passed since the claim, so the
        fake records this claim as two days old."""
        with Connected(self, grant_seconds=3 * 24 * 3600, issued_ago=2 * 24 * 3600) as connected:
            report, _ = connected.status()
            self.assertEqual(report.get("authorization_expiry"), "authorization_expiring", report)
            # What the header callback spawns in the background; it renews
            # before it exits.
            connected.runtime("entitlement")
            self.assertTrue(connected.platform.renewals, "a renewal was requested")
            report, _ = self.assert_status(connected, {"connected_waiting_for_usage", "receiving_usage"}, "verified")
            self.assertNotIn("authorization_expiry", report, report)
            self.assertEqual(report.get("authorization_expires_at", "")[:19],
                             connected.platform.grants[connected.reference]["receipt"]["expires_at"][:19], report)
            connected.record("renewed", {"renewals": len(connected.platform.renewals)})

    def test_refused_renewal_is_never_retried(self):
        """ork-meter #362: a refused renewal is recorded and not asked again
        until the computer is paired again."""
        with Connected(self, grant_seconds=3 * 24 * 3600, renew="refuse") as connected:
            connected.runtime("entitlement")
            self.assertTrue(connected.platform.renewals, "a renewal was requested")
            connected.runtime("entitlement")
            connected.runtime("entitlement")
            self.assertEqual(len(connected.platform.renewals), 1, connected.platform.renewals)
            connected.record("renewal refused", {"renewals": len(connected.platform.renewals)})

    # --- lost reply and permanent rejection ----------------------------------

    def test_lost_reply_replay(self):
        with Connected(self) as connected:
            connected.baseline()
            self.assertTrue(wait_for(lambda: connected.platform.events, timeout=70))
            observations, events = set(connected.platform.observations), set(connected.platform.events)
            connected.platform.mode = "lose_reply"
            connected.session("Say the word.")
            attempts = connected.platform.attempts

            def replayed(kind, known):
                new = [identifier for identifier in attempts[kind] if identifier not in known]
                return new and new.count(new[0]) >= 2
            # The snapshot comes back on the 5 s automatic retry.
            self.assertTrue(wait_for(lambda: replayed("observation", observations), timeout=30),
                            "the snapshot is replayed: %s" % attempts["observation"])
            # The observed event is retried at the next delivery: a new export wakes it.
            self.assertTrue(wait_for(lambda: [i for i in attempts["event"] if i not in events], timeout=70))
            connected.session("Say it again.")
            self.assertTrue(wait_for(lambda: replayed("event", events), timeout=130),
                            "the event is replayed: %s" % attempts["event"])
            lost_observation = [i for i in attempts["observation"] if i not in observations][0]
            lost_event = [i for i in attempts["event"] if i not in events][0]
            self.assertIn(lost_observation, connected.platform.observations)
            self.assertIn(lost_event, connected.platform.events)
            replies = [r["outcome"] for r in connected.platform.requests if lost_event in r["body"]]
            self.assertEqual(replies[:2], ["drop", 200], "committed, reply lost, then acknowledged as a duplicate")
            self.assert_status(connected, {"receiving_usage", "connected_waiting_for_usage"}, "verified")
            connected.record("lost reply after commit", {"observation_attempts": attempts["observation"].count(lost_observation),
                                                         "event_attempts": attempts["event"].count(lost_event)})

    def test_permanent_rejection_does_not_block_the_queue(self):
        """ork-meter #357 (#355): a 422 event is set aside, not dropped, and
        later events are delivered past it."""
        with Connected(self) as connected:
            connected.baseline()
            self.assertTrue(wait_for(lambda: connected.platform.events, timeout=70), "precondition: events are delivered")
            known = set(connected.platform.events)
            connected.platform.mode = "reject_first_event"
            connected.session("Say the word.")
            self.assertTrue(wait_for(lambda: connected.platform.rejected, timeout=70), "precondition: an event got 422")
            connected.session("Say it again.")
            self.assertTrue(wait_for(lambda: set(connected.platform.events) - known, timeout=130),
                            "a later event is delivered past the rejected one")
            report, _ = connected.status()
            self.assertGreaterEqual(report.get("observed_set_aside", 0) + report.get("observed_rejected", 0), 1, report)
            connected.record("permanent rejection", {"rejected_attempts": connected.platform.attempts["event"].count(
                connected.platform.rejected)})

if __name__ == "__main__":
    # The CI job sets ORK_CONNECTED_FAILURE_CI; there a skip would pass
    # vacuously, so a missing prerequisite fails instead.
    if os.environ.get("ORK_CONNECTED_FAILURE_CI") == "1" and not ENABLED:
        raise SystemExit("connected suite prerequisites missing: RUN_NATIVE_CLAUDE_TESTS=1, Linux and "
                         "ORK_FAKE_PLATFORM_CA are required")
    unittest.main()
