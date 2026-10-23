"""Test fixture: sign Ork Meter Plus policy envelopes with the TEST key.

The hooks accept a lever policy only inside an envelope the platform signed
(hooks/lib/plus_policy.py; the contract is ork-meter docs/plus-policy.md).
Tests that need an entitled hook therefore need a signed envelope, issued
now, since the verifier bounds an envelope's life to a day. This module
signs one with the contract's **test key**, whose seed the contract
publishes (``sha256("ork-meter plus policy TEST key 2026; never
production")``) and which is bound to ``https://plus-policy-test.invalid``:
nothing it signs is accepted for a real pairing. The signer is RFC 8032
Ed25519 over the verifier's own curve arithmetic, so tests/plus-policy.test.sh
can check it reproduces the contract fixtures' signatures byte for byte.

``install(directory, ...)`` writes what the plugin runtime writes into
``$CLAUDE_PLUGIN_DATA``: ``plus-policy.json`` (the envelope), ``connection.json``
(the pairing reference and origin) and the legacy ``entitlement.json``.

Command line (tests/lib.sh ``write_entitlement``)::

    python3 -I tests/plus_policy_sign.py <directory> [optimize] [state] [until-json] [policy-json]
"""
import base64
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "lib"))
import features  # noqa: E402
import plus_policy  # noqa: E402

KEY_ID = "orkmeter-test-2026"
ORIGIN = "https://plus-policy-test.invalid"
REFERENCE = "fff8bc443e61f3d6b4e78c55eff5ba01"
SEED = hashlib.sha256(b"ork-meter plus policy TEST key 2026; never production").digest()
# The contract's claude-code policy document (docs/plus-policy.md).
DEFAULT_POLICY = {
    "id": "claude-code-defaults-2026-10-22",
    "levers": {"trim": True, "test_filter": True, "dedupe": True, "read_guard": True, "haiku_agents": True,
               "cache_nudge": True, "loop_guard": True, "audit": True, "context_hygiene": True, "stale_guard": True,
               "delta": True},
    "tunables": {"trim.budget_bash": 12288, "trim.budget_read": 24576, "trim.budget_grep": 8192,
                 "trim.budget_glob": 4096, "trim.budget_webfetch": 16384, "read_guard.bytes": 98304,
                 "read_guard.lines": 300, "read_guard.tokens": 6000, "cache_nudge.tokens": 40000,
                 "context_hygiene.tokens": 120000, "stale_guard.tokens": 100000},
}
assert set(DEFAULT_POLICY["levers"]) == set(features.ALL_FEATURES), "the contract policy names every lever in LEVERS"


def _secret(seed):
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:], plus_policy._encode_point(plus_policy._mul(plus_policy._B, scalar))


def public_key(seed=SEED):
    return _secret(seed)[2].hex()


def sign(message, seed=SEED):
    """The 64-byte RFC 8032 Ed25519 signature of ``message``."""
    scalar, prefix, public = _secret(seed)
    r = int.from_bytes(hashlib.sha512(prefix + bytes(message)).digest(), "little") % plus_policy._L
    R = plus_policy._encode_point(plus_policy._mul(plus_policy._B, r))
    k = int.from_bytes(hashlib.sha512(R + public + bytes(message)).digest(), "little") % plus_policy._L
    return R + ((r + k * scalar) % plus_policy._L).to_bytes(32, "little")


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def stamp(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def payload(optimize=True, state="active", until="2099-01-01T00:00:00Z", checked_at=None, policy=None,
            plugin="claude-code", audience=REFERENCE, issuer=ORIGIN, issued_at=None, expires_at=None, now=None):
    """The payload document: the contract's eight keys."""
    now = time.time() if now is None else now
    issued = now if issued_at is None else issued_at
    return {
        "version": 1, "issuer": issuer, "audience": audience, "plugin": plugin,
        "issued_at": stamp(issued), "expires_at": stamp(issued + 24 * 3600 if expires_at is None else expires_at),
        "entitlement": {"optimize": optimize, "state": state, "until": until,
                        "checked_at": stamp(now) if checked_at is None else checked_at},
        "policy": DEFAULT_POLICY if policy is None else policy,
    }


def envelope(body=None, key_id=KEY_ID, seed=SEED, **kw):
    """The envelope bytes, as the platform sends them and the runtime caches them."""
    body = payload(**kw) if body is None else body
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return json.dumps({"key_id": key_id, "payload": b64(raw), "signature": b64(sign(raw, seed))},
                      separators=(",", ":")).encode("utf-8")


def _write(path, raw):
    with open(path, "wb") as stream:
        stream.write(raw)
    os.chmod(path, 0o600)


def install(directory, reference=REFERENCE, origin=ORIGIN, connection=True, legacy=True, **kw):
    """Write ``plus-policy.json`` (the contract policy unless ``policy`` is
    given), ``connection.json`` and (``legacy``) the four-field
    ``entitlement.json`` into ``directory``; returns ``directory``."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    body = payload(**kw)
    _write(os.path.join(directory, "plus-policy.json"), envelope(body))
    if connection:
        _write(os.path.join(directory, "connection.json"),
               json.dumps({"reference": reference, "origin": origin}).encode("utf-8"))
    if legacy:
        _write(os.path.join(directory, "entitlement.json"), (json.dumps(body["entitlement"]) + "\n").encode("utf-8"))
    return directory


def main(argv):
    directory = argv[0]
    optimize = argv[1] != "false" if len(argv) > 1 else True
    state = argv[2] if len(argv) > 2 else "active"
    until = json.loads(argv[3]) if len(argv) > 3 and argv[3] else "2099-01-01T00:00:00Z"
    policy = json.loads(argv[4]) if len(argv) > 4 and argv[4] else None
    install(directory, optimize=optimize, state=state, until=until, policy=policy)


if __name__ == "__main__":
    main(sys.argv[1:])
