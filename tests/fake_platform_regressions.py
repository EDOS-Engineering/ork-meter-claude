"""Self-test of tests/fake_platform.py, the connected suite's fake Ork Meter.

The connected suite runs only on Linux CI. This checks, everywhere and with
no trust-store change (the client trusts the throwaway CA through ``cafile``
only), that the MITM proxy terminates TLS for the platform hostnames, refuses
everything else, and that the fake answers as ork-meter's client requires:
exact ``Content-Type: application/json``, strict receipts, idempotent replays,
and each failure mode. Pairing material is made the way
collectorpairing.NewClientMaterial makes it.
"""
import base64
from datetime import timedelta
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import shutil
import ssl
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import support  # noqa: E402,F401  (scrubs ORK_METER_*)
import fake_platform  # noqa: E402

ORIGIN = "https://orkmeter.com"


def material():
    raw = secrets.token_bytes(80)
    device = "omp_" + base64.urlsafe_b64encode(raw[16:48]).rstrip(b"=").decode()
    token = "omc_" + base64.urlsafe_b64encode(raw[48:80]).rstrip(b"=").decode()
    return raw[:16].hex(), device, token


@unittest.skipUnless(shutil.which("openssl"), "openssl makes the throwaway CA")
class FakePlatformWire(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ork-fake-platform-"))
        fake_platform.make_ca(cls.tmp / "ca")
        cls.context = fake_platform.leaf_context(cls.tmp / "ca", cls.tmp / "leaf")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.platform = fake_platform.FakePlatform()
        self.proxy = fake_platform.MitmProxy(self.platform, self.context)
        self.addCleanup(self.proxy.close)
        self.client_context = ssl.create_default_context(cafile=str(self.tmp / "ca/ca.pem"))

    def call(self, method, path, body=None, auth=None, host="orkmeter.com"):
        connection = http.client.HTTPSConnection("127.0.0.1", self.proxy.server.server_port,
                                                 context=self.client_context, timeout=10)
        connection.set_tunnel(host, 443)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            headers["Authorization"] = auth
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read(), response
        finally:
            connection.close()

    def pair(self, grant_seconds=None):
        if grant_seconds:
            self.platform.grant_seconds = grant_seconds
        identifier, device, token = material()
        return_url = "http://127.0.0.1:43210/plugin/claim/" + "ab" * 32
        proposal = {"id": identifier, "source": "claude", "client": "claude-code",
                    "device_sha256": fake_platform.secret_digest(device, "omp_"),
                    "token_sha256": fake_platform.secret_digest(token, "omc_"),
                    "return_sha256": hashlib.sha256(return_url.encode()).hexdigest()}
        code = self.platform.approve(ORIGIN, json.dumps(proposal, separators=(",", ":")), return_url)
        self.assertRegex(code, r"^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
        body = json.dumps({"id": identifier, "claim_code": code.replace("-", "")})
        status, kind, raw, _ = self.call("POST", "/v1/collectors/claim", body, "Device " + device)
        self.assertEqual((status, kind), (200, "application/json"))
        receipt = json.loads(raw)
        self.assertEqual(sorted(receipt), ["audience", "client", "expires_at", "feed", "id", "scope", "source"])
        self.assertEqual((receipt["id"], receipt["feed"], receipt["audience"], receipt["scope"]),
                         (identifier, identifier, ORIGIN, "usage:ingest"))
        self.assertRegex(receipt["expires_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        replay = self.call("POST", "/v1/collectors/claim", body, "Device " + device)
        self.assertEqual(json.loads(replay[2]), receipt, "a claim replay returns the same grant")
        wrong = self.call("POST", "/v1/collectors/claim", json.dumps({"id": identifier, "claim_code": "00000000"}),
                          "Device " + device)
        self.assertEqual(wrong[0], 403)
        return identifier, "Collector " + token, receipt

    def test_tls_terminates_for_platform_hosts_only(self):
        for host in fake_platform.PLATFORM_HOSTS:
            status, kind, raw, _ = self.call("GET", "/auth/config", host=host)
            self.assertEqual((status, kind), (200, "application/json"))
            self.assertTrue(all(json.loads(raw)[key] for key in ("plugin_return", "native_observations")))
        with self.assertRaises(OSError):
            self.call("GET", "/", host="example.com")
        self.assertEqual(self.platform.refused, [("CONNECT", "example.com:443")])

    def test_pairing_uploads_status_and_idempotent_replay(self):
        identifier, auth, receipt = self.pair()
        status, _, raw, _ = self.call("POST", "/v1/collectors/native-status", "{}", auth)
        self.assertEqual(json.loads(raw), {"authorization": receipt}, "no latest_export key before an export")
        observation = json.dumps({"id": "c" * 32, "client": "claude-code", "captured_at": "2026-10-18T12:00:00Z",
                                  "samples": [{"kind": "cumulative_counter_snapshot", "counters": {"input_tokens": "17"}}]})
        first = self.call("POST", "/v1/collectors/observations", observation, auth)
        again = self.call("POST", "/v1/collectors/observations", observation, auth)
        self.assertEqual((json.loads(first[2]), json.loads(again[2])), ({"created": True}, {"created": False}))
        self.assertEqual(self.call("POST", "/v1/collectors/observations", observation.replace("17", "18"), auth)[0], 409)
        latest = json.loads(self.call("POST", "/v1/collectors/native-status", "{}", auth)[2])["latest_export"]
        self.assertEqual(sorted(latest), ["additive", "captured_at", "client", "included_in_spend", "received_at",
                                          "samples", "samples_with_counters"])
        entitlement = json.loads(self.call("GET", "/v1/entitlement", None, auth)[2])
        self.assertEqual(sorted(entitlement), ["checked_at", "optimize", "state", "until"])
        self.assertEqual(self.call("GET", "/v1/entitlement", None, "Collector omc_" + "A" * 43)[0], 403)

    def test_failure_modes(self):
        _, auth, _ = self.pair()
        event = json.dumps({"id": "claude-observed-" + "d" * 32, "source": "claude"})
        self.platform.mode = "5xx"
        self.assertEqual(self.call("POST", "/v1/collectors/events", event, auth)[0], 503)
        self.platform.mode = "429"
        status, _, _, response = self.call("POST", "/v1/collectors/events", event, auth)
        self.assertEqual((status, response.getheader("Retry-After")), (429, "60"))
        self.platform.mode = "403"
        self.assertEqual(self.call("POST", "/v1/collectors/events", event, auth)[0], 403)
        self.platform.mode = "lose_reply"
        with self.assertRaises((http.client.HTTPException, OSError)):
            self.call("POST", "/v1/collectors/events", event, auth)
        self.assertEqual(json.loads(self.call("POST", "/v1/collectors/events", event, auth)[2]), {"created": False},
                         "the committed event's replay is acknowledged, not stored twice")
        self.assertEqual(list(self.platform.events), ["claude-observed-" + "d" * 32])
        self.platform.mode = "reject_first_event"
        other = event.replace("d" * 32, "e" * 32)
        self.assertEqual(self.call("POST", "/v1/collectors/events", other, auth)[0], 422)
        self.assertEqual(self.call("POST", "/v1/collectors/events", other, auth)[0], 422)
        self.platform.mode = "down"
        with self.assertRaises(OSError):
            self.call("GET", "/auth/config")
        self.platform.mode = "up"
        self.platform.revoke_all()
        self.assertEqual(self.call("POST", "/v1/collectors/native-status", "{}", auth)[0], 403)

    def test_a_fresh_credential_is_kept_not_extended(self):
        """collectorpairing.Renew counts the once-a-day rule from the claim
        when the credential was never renewed: none in its first 24 hours."""
        identifier, auth, receipt = self.pair(grant_seconds=3 * 24 * 3600)
        status, _, raw, _ = self.call("POST", "/v1/collectors/renew", "{}", auth)
        self.assertEqual((status, json.loads(raw)), (200, receipt), "kept: claimed less than a day ago")
        self.assertNotIn("renewed", self.platform.grants[identifier])
        self.assertEqual(self.platform.renewals, [(identifier, receipt["expires_at"])])

    def test_renewal_extends_once_a_day_keeps_or_refuses(self):
        self.platform.issued_ago = 2 * 24 * 3600   # claimed two days ago
        identifier, auth, receipt = self.pair(grant_seconds=3 * 24 * 3600)
        self.assertEqual(self.call("POST", "/v1/collectors/renew", '{"x":1}', auth)[0], 400)
        status, kind, raw, _ = self.call("POST", "/v1/collectors/renew", "{}", auth)
        renewed = json.loads(raw)
        self.assertEqual((status, kind), (200, "application/json"))
        self.assertEqual(sorted(renewed), sorted(receipt))
        self.assertGreater(renewed["expires_at"], receipt["expires_at"], "extended to 30 days ahead")
        again = json.loads(self.call("POST", "/v1/collectors/renew", "{}", auth)[2])
        self.assertEqual(again, renewed, "at most once a day: the current receipt")
        status_reply = json.loads(self.call("POST", "/v1/collectors/native-status", "{}", auth)[2])
        self.assertEqual(status_reply["authorization"], renewed, "status carries the renewed expiry")
        self.platform.grants[identifier]["renewed"] -= timedelta(days=2)
        self.platform.renew = "keep"
        self.assertEqual(json.loads(self.call("POST", "/v1/collectors/renew", "{}", auth)[2]), renewed)
        self.platform.renew = "refuse"
        self.assertEqual(self.call("POST", "/v1/collectors/renew", "{}", auth)[0], 403)
        self.assertEqual(self.platform.renewals[-1], (identifier, None))

    def test_typed_rejections_carry_the_platforms_bodies(self):
        _, auth, _ = self.pair()
        event = json.dumps({"id": "claude-observed-" + "f" * 32, "source": "claude"})
        self.platform.mode = "reject_first_event"
        status, _, raw, _ = self.call("POST", "/v1/collectors/events", event, auth)
        self.assertEqual((status, json.loads(raw)), (422, {"error": "collector event cannot be recorded; do not retry it"}))
        self.platform.mode = "up"
        self.call("POST", "/v1/collectors/events", event.replace("f" * 32, "a" * 32), auth)
        status, _, raw, _ = self.call("POST", "/v1/collectors/events",
                                      event.replace("f" * 32, "a" * 32).replace("claude\"}", "claude\", \"x\": 1}"), auth)
        self.assertEqual(status, 409)
        self.platform.mode = "429"
        status, _, raw, response = self.call("POST", "/v1/collectors/events", event, auth)
        self.assertEqual((status, json.loads(raw)["error"], response.getheader("Retry-After")),
                         (429, "collector request allowance exceeded", "60"))

    def test_expired_grant_is_refused(self):
        _, auth, receipt = self.pair(grant_seconds=1)
        self.platform.grants[receipt["id"]]["expires"] = fake_platform.now()
        self.assertEqual(self.call("POST", "/v1/collectors/native-status", "{}", auth)[0], 403)

    def test_host_header_must_match_the_tunnel(self):
        connection = http.client.HTTPSConnection("127.0.0.1", self.proxy.server.server_port,
                                                 context=self.client_context, timeout=10)
        connection.set_tunnel("orkmeter.com", 443)
        try:
            connection.putrequest("GET", "/auth/config", skip_host=True)
            connection.putheader("Host", "ork-meter.edos.io")
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
