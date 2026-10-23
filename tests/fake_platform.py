"""A fake hosted Ork Meter for the connected failure suite (#45).

tests/native_failure_connected_qualification.py runs the shipped runtime
against this fake. The runtime reaches it the way it reaches the real
platform: HTTPS to orkmeter.com (or ork-meter.edos.io,
dev.ork-meter.edos.io) through HTTPS_PROXY. ``MitmProxy`` is that proxy: it
answers CONNECT for the platform hostnames, terminates TLS with a certificate
from a throwaway CA, and hands each request to ``FakePlatform``. Any other
destination is refused and recorded. Plain-HTTP proxy requests are refused.

The wire contract mirrors ork-meter at the commit the runtime was built from
(cited per route below): every accepted reply is 200 with exactly
``Content-Type: application/json`` (localpairing/transport.go), strict JSON
with exactly the expected keys, and whole-second UTC times. Every request
body is kept in ``requests`` so the suite can scan it for canaries.

``mode`` breaks the platform the ways docs/failure-behaviour.md tables:
"up", "down" (CONNECT refused), "slow" (each reply held ``SLOW`` seconds),
"5xx" (503), "429" (429 with Retry-After: 60), "403" (collector authorization
denied, as for a revoked or expired credential), "lose_reply" (commit, then
drop the connection without answering, once per observation or event ID),
"reject_first_event" (422 for the first event ID it sees, forever).

``make_ca`` / ``leaf_context`` create the CA and the server certificate with
openssl. Nothing here installs a CA: the connected-failure CI job does, on
its runner only (``python3 tests/fake_platform.py make-ca DIR``).
"""
import base64
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import LocalHTTPServer  # noqa: E402

PLATFORM_HOSTS = ("orkmeter.com", "ork-meter.edos.io", "dev.ork-meter.edos.io")
CLAIM_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SLOW = 30
# collectorpairing.Scope; the receipt keys of localpairing/claim.go.
SCOPE = "usage:ingest"


# --- TLS -------------------------------------------------------------------

def _openssl():
    binary = shutil.which("openssl")
    if not binary:
        raise RuntimeError("openssl is required")
    return binary


def make_ca(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "ca.cnf"
    config.write_text("[req]\nprompt = no\ndistinguished_name = dn\nx509_extensions = ext\n"
                      "[dn]\nCN = Ork Meter plugin fake platform test CA (CI only)\n"
                      "[ext]\nbasicConstraints = critical,CA:TRUE,pathlen:0\n"
                      "keyUsage = critical,keyCertSign,cRLSign\nsubjectKeyIdentifier = hash\n")
    subprocess.run([_openssl(), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-config", str(config),
                    "-keyout", str(directory / "ca.key"), "-out", str(directory / "ca.pem")],
                   check=True, capture_output=True, timeout=60)
    (directory / "ca.key").chmod(0o600)
    return directory / "ca.pem"


def leaf_context(ca_directory, work, hosts=PLATFORM_HOSTS):
    """A server SSLContext (HTTP/1.1 only) for ``hosts``, signed by the CA."""
    ca_directory, work = Path(ca_directory), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    key, request, cert, extensions = (work / name for name in ("leaf.key", "leaf.csr", "leaf.pem", "leaf.ext"))
    extensions.write_text("basicConstraints = critical,CA:FALSE\nkeyUsage = critical,digitalSignature,keyEncipherment\n"
                          "extendedKeyUsage = serverAuth\nsubjectKeyIdentifier = hash\n"
                          "authorityKeyIdentifier = keyid,issuer\nsubjectAltName = "
                          + ",".join("DNS:" + host for host in hosts) + "\n")
    subprocess.run([_openssl(), "req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=" + hosts[0],
                    "-keyout", str(key), "-out", str(request)], check=True, capture_output=True, timeout=60)
    subprocess.run([_openssl(), "x509", "-req", "-in", str(request), "-CA", str(ca_directory / "ca.pem"),
                    "-CAkey", str(ca_directory / "ca.key"), "-CAcreateserial", "-days", "1", "-extfile", str(extensions),
                    "-out", str(cert)], check=True, capture_output=True, timeout=60)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert), str(key))
    context.set_alpn_protocols(["http/1.1"])
    return context


# --- the platform ----------------------------------------------------------

def now():
    return datetime.now(timezone.utc)


def stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def secret_digest(secret, prefix):
    """collectorpairing.digest: sha256(prefix NUL secret) of a 47-char secret."""
    if not isinstance(secret, str) or len(secret) != 47 or not secret.startswith(prefix):
        return None
    try:
        raw = base64.urlsafe_b64decode(secret[4:] + "=")
    except ValueError:
        return None
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != secret[4:]:
        return None
    return hashlib.sha256((prefix + "\x00" + secret).encode()).hexdigest()


def claim_digest(identifier, code):
    return hashlib.sha256(("ork-meter-claim-code-v1\x00%s\x00%s" % (identifier, code)).encode()).hexdigest()


def fingerprint(origin, proposal_json):
    return hashlib.sha256(("ork-meter-pairing-v1\x00%s\x00%s" % (origin, proposal_json)).encode()).hexdigest()


class FakePlatform:
    def __init__(self, grant_seconds=30 * 24 * 3600):
        self.lock = threading.Lock()
        self.mode = "up"
        # POST /v1/collectors/renew (ork-meter #362): "extend" moves expiry to
        # 30 days ahead, but only once a day has passed since the credential's
        # lifetime start (its claim, or its latest renewal), so a credential
        # in its first 24 hours keeps its expiry, as collectorpairing.Renew
        # decides (pairing.go LifetimeStart, RenewalInterval); "keep" returns
        # the current receipt (an account whose access ends then); "refuse"
        # answers 403, as for a revoked or expired credential.
        self.renew = "extend"
        # A test knob: claims are recorded as issued this many seconds ago, so
        # a test can pair a credential old enough for "extend" to renew it.
        # The expiry is still grant_seconds from the claim's real time.
        self.issued_ago = 0
        self.renewals = []           # (id, expires_at returned or None when refused)
        self.grant_seconds = grant_seconds
        self.requests = []           # (time, host, method, path, headers, body, outcome)
        self.refused = []            # CONNECT or plain-HTTP requests to anything else
        self.tickets = {}            # id -> approval (proposal, origin, code digest)
        self.grants = {}             # id -> receipt dict (claim replays return the same)
        self.observations = {}       # observation id -> body (one stored copy each)
        self.events = {}             # event id -> body
        self.attempts = {"observation": [], "event": []}   # ids of every upload attempt, any mode
        self.lost = set()
        self.rejected = None
        self.received_at = None
        self.latest = None

    # The human's approval on the hosted /pair page (the browser half).
    def approve(self, origin, proposal_json, return_url):
        proposal = json.loads(proposal_json)
        assert proposal["source"] == "claude" and proposal["client"] == "claude-code", proposal
        assert hashlib.sha256(return_url.encode()).hexdigest() == proposal["return_sha256"], "return URL digest"
        code = "".join(secrets.choice(CLAIM_ALPHABET) for _ in range(8))
        with self.lock:
            self.tickets[proposal["id"]] = {"proposal": proposal, "origin": origin, "code": claim_digest(proposal["id"], code),
                                            "approved": now()}
        return code[:4] + "-" + code[4:]

    def revoke_all(self):
        with self.lock:
            for grant in self.grants.values():
                grant["revoked"] = True

    def credential(self, headers, scheme):
        """The grant/ticket id an ``Authorization: <scheme> <secret>`` names, or None."""
        values = headers.get_all("Authorization") or []
        if len(values) != 1 or not values[0].startswith(scheme + " "):
            return None
        prefix = "omp_" if scheme == "Device" else "omc_"
        digest = secret_digest(values[0][len(scheme) + 1:], prefix)
        if digest is None:
            return None
        key = "device_sha256" if scheme == "Device" else "token_sha256"
        for identifier, ticket in self.tickets.items():
            if ticket["proposal"][key] == digest:
                return identifier
        return None

    def handle(self, host, method, path, headers, body):
        """(status, payload dict or None, extra headers) or "drop"."""
        mode = self.mode
        kind = {"/v1/collectors/observations": "observation", "/v1/collectors/events": "event"}.get(path)
        if method == "POST" and kind:
            try:
                self.attempts[kind].append(json.loads(body)["id"])
            except (ValueError, KeyError, TypeError):
                pass
        if mode == "slow":
            time.sleep(SLOW)
        if mode == "5xx":
            return 503, {"error": "collector authorization unavailable"}, {}
        if mode == "429":
            # httpapi/collector_usage.go collectorUsageError (ledger.ErrQuota).
            return 429, {"error": "collector request allowance exceeded"}, {"Retry-After": "60"}
        if method == "GET" and path == "/auth/config":
            # pluginruntime/platform.go CheckPlatform; httpapi/public.go:46.
            return 200, {key: True for key in ("plugin_return", "plugin_return_bound", "collector_pairing",
                                               "collector_ingestion", "native_observations")}, {}
        if method == "POST" and path == "/v1/collectors/claim":
            return self.claim(headers, body)
        identifier = self.credential(headers, "Collector")
        grant = self.grants.get(identifier)
        if grant is None or grant.get("revoked") or mode == "403" or now() >= grant["expires"]:
            return 403, {"error": "collector authorization denied"}, {}
        if method == "GET" and path == "/v1/entitlement":
            # localpairing/entitlement.go: exactly four keys.
            return 200, {"optimize": True, "state": "trial", "until": stamp(now() + timedelta(days=7)),
                         "checked_at": stamp(now())}, {}
        if method == "POST" and path == "/v1/collectors/native-status":
            reply = {"authorization": grant["receipt"]}
            if self.latest:
                reply["latest_export"] = self.latest
            return 200, reply, {}
        if method == "POST" and path == "/v1/collectors/renew":
            return self.renew_grant(identifier, grant, body)
        if method == "POST" and path == "/v1/collectors/observations":
            return self.store("observation", self.observations, body)
        if method == "POST" and path == "/v1/collectors/events":
            return self.store("event", self.events, body)
        return 404, {"error": "not found"}, {}

    def renew_grant(self, identifier, grant, body):
        """httpapi/collector_usage.go renewCollector: body exactly {}; extend
        to 30 days ahead when a day has passed since the lifetime start and
        that is later than the current expiry, else the current receipt
        (collectorpairing.Renew)."""
        try:
            empty = json.loads(body) == {}
        except ValueError:
            empty = False
        if not empty:
            return 400, {"error": "invalid collector renewal"}, {}
        if self.renew == "refuse":
            self.renewals.append((identifier, None))
            return 403, {"error": "collector authorization denied"}, {}
        renewed = grant.get("renewed")
        start = renewed if renewed is not None and renewed > grant["issued"] else grant["issued"]
        expires = (now() + timedelta(days=30)).replace(microsecond=0)
        if self.renew == "extend" and now() - start >= timedelta(days=1) and expires > grant["expires"]:
            grant.update(expires=expires, renewed=now())
            grant["receipt"] = dict(grant["receipt"], expires_at=stamp(expires))
        self.renewals.append((identifier, grant["receipt"]["expires_at"]))
        return 200, grant["receipt"], {}

    def claim(self, headers, body):
        # localpairing/claim.go and dynamostore/collector_pairing.go:358-404.
        identifier = self.credential(headers, "Device")
        try:
            request = json.loads(body)
        except ValueError:
            request = {}
        ticket = self.tickets.get(identifier)
        if (ticket is None or sorted(request) != ["claim_code", "id"] or request["id"] != identifier
                or claim_digest(identifier, request["claim_code"]) != ticket["code"]
                or now() > ticket["approved"] + timedelta(minutes=10)):
            return 403, {"error": "collector authorization denied"}, {}
        if identifier not in self.grants:
            expires = (now() + timedelta(seconds=self.grant_seconds)).replace(microsecond=0)
            receipt = {"id": identifier, "source": "claude", "client": "claude-code", "feed": identifier,
                       "audience": ticket["origin"], "scope": SCOPE, "expires_at": stamp(expires)}
            self.grants[identifier] = {"receipt": receipt, "expires": expires,
                                       "issued": now() - timedelta(seconds=self.issued_ago)}
        return 200, self.grants[identifier]["receipt"], {}

    def store(self, kind, table, body):
        """Idempotent receipt: the same ID and body again is ``created: false``;
        the same ID with other content is 409 (dynamostore/store.go:194-214)."""
        try:
            document = json.loads(body)
            identifier = document["id"]
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "invalid collector metadata event"}, {}
        if kind == "event" and self.mode == "reject_first_event":
            if self.rejected in (None, identifier):
                self.rejected = identifier
                return 422, {"error": "collector event cannot be recorded; do not retry it"}, {}
        if identifier in table:
            if table[identifier] != body:
                return 409, {"error": "event identity already contains different data"}, {}
            created = False
        else:
            table[identifier] = body
            created = True
            if kind == "observation":
                received = now()
                self.latest = {"client": "claude-code", "captured_at": document["captured_at"],
                               "received_at": stamp(received), "samples": len(document["samples"]),
                               "samples_with_counters": len(document["samples"]), "additive": False,
                               "included_in_spend": False}
        if self.mode == "lose_reply" and (kind, identifier) not in self.lost:
            self.lost.add((kind, identifier))
            return "drop"
        return 200, {"created": created}, {}


class _PlatformHandler(BaseHTTPRequestHandler):
    """One request inside a terminated TLS tunnel."""
    protocol_version = "HTTP/1.0"

    def __init__(self, request, address, server, platform, host):
        self.platform, self.tunnel_host = platform, host
        super().__init__(request, address, server)

    def log_message(self, *_):
        pass

    def respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        host = (self.headers.get("Host") or "").split(":")[0]
        record = {"time": time.time(), "host": host, "method": self.command, "path": self.path,
                  "headers": {key: value for key, value in self.headers.items() if key.lower() != "authorization"},
                  "body": body.decode("utf-8", "replace")}
        if host != self.tunnel_host:
            result = (403, {"error": "request refused"}, {})
        else:
            with self.platform.lock if self.platform.mode != "slow" else _NoLock():
                result = self.platform.handle(host, self.command, self.path.split("?")[0], self.headers, record["body"])
        record["outcome"] = result if result == "drop" else result[0]
        self.platform.requests.append(record)
        if result == "drop":
            self.close_connection = True
            return
        status, payload, extra = result
        raw = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_DELETE = respond


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class MitmProxy:
    """HTTP(S)_PROXY for the runtime: platform hostnames are terminated and
    served by ``platform``; everything else is refused and recorded."""

    def __init__(self, platform, context):
        self.platform = platform
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def refuse(self):
                platform.refused.append((self.command, self.path))
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
            do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = do_PATCH = do_OPTIONS = refuse

            def do_CONNECT(self):
                host, _, port = self.path.rpartition(":")
                if host not in PLATFORM_HOSTS or port != "443" or platform.mode == "down":
                    if host not in PLATFORM_HOSTS:
                        platform.refused.append(("CONNECT", self.path))
                    else:
                        platform.requests.append({"time": time.time(), "host": host, "method": "CONNECT",
                                                  "path": self.path, "headers": {}, "body": "", "outcome": "refused"})
                    return self.refuse_quietly()
                self.send_response(200, "Connection Established")
                self.end_headers()
                self.wfile.flush()
                try:
                    tunnel = context.wrap_socket(self.connection, server_side=True)
                except (ssl.SSLError, OSError):
                    self.close_connection = True
                    return
                try:
                    _PlatformHandler(tunnel, self.client_address, proxy.server, platform, host)
                except OSError:
                    pass
                finally:
                    try:
                        tunnel.close()
                    except OSError:
                        pass
                self.close_connection = True

            def refuse_quietly(self):
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
        self.server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    # python3 tests/fake_platform.py make-ca <dir>   (the connected-failure CI job only)
    if len(sys.argv) != 3 or sys.argv[1] != "make-ca":
        raise SystemExit("usage: fake_platform.py make-ca <directory>")
    print(make_ca(sys.argv[2]))
