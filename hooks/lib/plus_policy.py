"""Verify a signed Ork Meter Plus policy envelope; no network, no dependencies.

Canonical source: EDOS-Engineering/ork-meter docs/plus-policy.md. The Claude
Code and Codex plugins vendor this exact file; a contract test checks the
copies' digest. It verifies an Ed25519 signature (RFC 8032) over the exact
payload bytes the platform signed, then binds the payload to this pairing:
the issuer must be the pairing origin and the key's own origin, the audience
must be the saved public pairing reference, and the envelope must be fresh.
Pure Python verification takes about 4 ms, so a hook can afford it on every
event; nothing on disk is trusted without it.

An envelope is::

    {"payload": "<base64url, no padding>", "signature": "<base64url, 64 bytes>", "key_id": "<id>"}

and its payload bytes decode to::

    {"version": 1, "issuer": "https://orkmeter.com", "audience": "<reference>",
     "plugin": "codex" | "claude-code", "issued_at": "...Z", "expires_at": "...Z",
     "entitlement": {"optimize": true, "state": "promo", "until": "...Z", "checked_at": "...Z"},
     "policy": {"id": "...", ...}}
"""
import base64
import datetime
import hashlib
import json

VERSION = 1
MAX_ENVELOPE_BYTES = 8192
MAX_PAYLOAD_BYTES = 6144
FUTURE_SKEW = 300
# Longest an envelope is honoured after issue without a fresh one, offline.
MAX_AGE = 15 * 60 + 24 * 3600
PLUGINS = ("codex", "claude-code")
STATES = ("promo", "trial", "active", "past_due", "free")

# key_id -> (Ed25519 public key, hex; the only issuer origin it may sign for).
# A key never signs for another origin, so a leaked test key cannot forge a
# production envelope. Add a key in a plugin release before the platform
# switches to it; drop it one release after the platform stops using it.
PUBLIC_KEYS = {
    "orkmeter-test-2026": ("ffa893a58ac455446992bc1b266cac7dd92c00181ffd4a62f02ad11ab5e775bb", "https://plus-policy-test.invalid"),
    "orkmeter-2026-11": ("fc67f540c784aacb13365cfd10252190ef1af2ae50f21fc773a06cfdf599d935", "https://orkmeter.com"),
    "orkmeter-staging-2026-11": ("b126d5709cb9f09d7b9413224a40a169671b86840dedfac01207e4b984f7421d", "https://ork-meter.edos.io"),
}

_Q = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _inv(x):
    return pow(x, _Q - 2, _Q)


def _recover_x(y, sign):
    if y >= _Q:
        raise ValueError("non-canonical point")
    xx = (y * y - 1) * _inv(_D * y * y + 1) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = x * _I % _Q
    if (x * x - xx) % _Q != 0:
        raise ValueError("not on curve")
    if x == 0 and sign:
        raise ValueError("invalid point")
    if x & 1 != sign:
        x = _Q - x
    return x


def _decode_point(raw):
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return (x, y, 1, x * y % _Q)


_BY = 4 * _inv(5) % _Q
_BX = _recover_x(_BY, 0)
_B = (_BX, _BY, 1, _BX * _BY % _Q)


def _add(p, q):
    (x1, y1, z1, t1), (x2, y2, z2, t2) = p, q
    a = (y1 - x1) * (y2 - x2) % _Q
    b = (y1 + x1) * (y2 + x2) % _Q
    c = t1 * 2 * _D * t2 % _Q
    d = z1 * 2 * z2 % _Q
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _Q, g * h % _Q, f * g % _Q, e * h % _Q)


def _mul(p, n):
    out = (0, 1, 1, 0)
    while n:
        if n & 1:
            out = _add(out, p)
        p = _add(p, p)
        n >>= 1
    return out


def _encode_point(p):
    x, y, z, _ = p
    zi = _inv(z)
    x, y = x * zi % _Q, y * zi % _Q
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def ed25519_verify(public_key, message, signature):
    """True only for a valid RFC 8032 signature; never raises."""
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        r = _decode_point(signature[:32])
        a = _decode_point(public_key)
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            return False
        h = int.from_bytes(hashlib.sha512(signature[:32] + bytes(public_key) + bytes(message)).digest(), "little") % _L
        return _encode_point(_mul(_B, s)) == _encode_point(_add(r, _mul(a, h)))
    except (ValueError, TypeError):
        return False


def _b64decode(text, size=None):
    if not isinstance(text, str) or not text or len(text) > MAX_ENVELOPE_BYTES or "=" in text:
        raise ValueError("base64url field")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if size is not None and len(raw) != size:
        raise ValueError("field size")
    return raw


def _timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z") or not 20 <= len(value) <= 30:
        raise ValueError("timestamp")
    parsed = datetime.datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed.timestamp()


def _unique_pairs(pairs):
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("duplicate field")
    return dict(pairs)


def _entitlement(value):
    if not isinstance(value, dict) or set(value) != {"optimize", "state", "until", "checked_at"}:
        raise ValueError("entitlement fields")
    if type(value["optimize"]) is not bool or value["state"] not in STATES:
        raise ValueError("entitlement values")
    if value["optimize"] and value["state"] == "free":
        raise ValueError("free does not optimize")
    until = None if value["until"] is None else _timestamp(value["until"])
    if value["optimize"] and until is None:
        raise ValueError("optimize without a period")
    return {"optimize": value["optimize"], "state": value["state"], "until": until,
            "checked_at": _timestamp(value["checked_at"])}


def verify(raw, plugin, reference, origin, now, keys=None):
    """The verified payload as a dict, with ``entitlement`` times as epoch
    seconds, or raise ValueError. ``raw`` is the envelope bytes exactly as the
    platform sent them (and as the plugin cached them)."""
    keys = PUBLIC_KEYS if keys is None else keys
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_ENVELOPE_BYTES:
        raise ValueError("envelope size")
    envelope = json.loads(raw, object_pairs_hook=_unique_pairs)
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature", "key_id"}:
        raise ValueError("envelope fields")
    key = keys.get(envelope["key_id"]) if isinstance(envelope["key_id"], str) else None
    if key is None:
        raise ValueError("unknown key")
    public_key, key_origin = bytes.fromhex(key[0]), key[1]
    payload = _b64decode(envelope["payload"])
    signature = _b64decode(envelope["signature"], 64)
    if len(payload) > MAX_PAYLOAD_BYTES or not ed25519_verify(public_key, payload, signature):
        raise ValueError("signature")
    body = json.loads(payload, object_pairs_hook=_unique_pairs)
    expected = {"version", "issuer", "audience", "plugin", "issued_at", "expires_at", "entitlement", "policy"}
    if not isinstance(body, dict) or set(body) != expected or body["version"] != VERSION:
        raise ValueError("payload fields")
    if body["issuer"] != origin or body["issuer"] != key_origin or body["plugin"] != plugin or plugin not in PLUGINS:
        raise ValueError("issuer or plugin")
    if not isinstance(reference, str) or not reference or body["audience"] != reference:
        raise ValueError("audience")
    issued, expires = _timestamp(body["issued_at"]), _timestamp(body["expires_at"])
    if issued > now + FUTURE_SKEW or expires < issued or now > min(expires, issued + MAX_AGE):
        raise ValueError("stale")
    policy = body["policy"]
    if not isinstance(policy, dict) or not isinstance(policy.get("id"), str) or not policy["id"]:
        raise ValueError("policy")
    return {"issuer": body["issuer"], "audience": body["audience"], "plugin": plugin,
            "issued_at": issued, "expires_at": expires,
            "entitlement": _entitlement(body["entitlement"]), "policy": policy}


def entitled(verified, now, offline_grace=24 * 3600):
    """The same rule as the unsigned contract: optimize and at most
    ``offline_grace`` past ``until``, on top of the envelope's own freshness."""
    answer = verified["entitlement"]
    bound = answer["until"] if answer["until"] is not None else answer["checked_at"]
    return bool(answer["optimize"]) and now <= bound + offline_grace
