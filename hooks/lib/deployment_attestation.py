"""Verify the attestation that admits an Ork Meter Enterprise deployment.

Canonical source: EDOS-Engineering/ork-meter docs/deployment-attestation.md.
The Claude Code and Codex plugins vendor this exact file beside
plugin_origins.py and plus_policy.py; a contract test in each checks the
copy's digest. No network, no dependencies beyond those two files.

The compiled origin registry (plugin_origins.py) lists the public cells only.
EDOS runs many enterprise deployments, so none is listed. Each serves, at
PATH, an envelope EDOS signed with a deployment-signing root key::

    {"payload": "<base64url, no padding>", "signature": "<base64url, 64 bytes>", "key_id": "<root key id>"}

whose payload bytes decode to exactly::

    {"version": 1, "origin": "https://ome.example", "organization": "<name>",
     "kind": "enterprise", "policy_keys": [{"id": "<key id>", "public": "<64 hex>"}],
     "issued_at": "...Z", "not_after": "...Z"}

A plugin sends a device proof or any usage to an origin outside the registry
only while it holds a verified attestation for exactly that origin, and it
verifies that deployment's lever policy with the attested keys only. Nothing
here trusts the deployment: the envelope is checked against ROOT_KEYS, whose
private halves are held offline or in KMS, never on a deployment.
"""
import base64
import datetime
import json
import re

try:
    from . import plugin_origins, plus_policy
except ImportError:  # a hooks directory on sys.path, not a package
    import plugin_origins
    import plus_policy

VERSION = 1
PATH = "/.well-known/ork-meter-deployment"
MAX_ENVELOPE_BYTES = 8192
# The payload bound is also the most AWS KMS signs as a raw message.
MAX_PAYLOAD_BYTES = 4096
FUTURE_SKEW = 300
# No revocation list: lapse is how a deployment is cut off, so validity is short.
MAX_LIFETIME = 90 * 24 * 3600
MAX_ORGANIZATION = 120
MAX_POLICY_KEYS = 2
KIND = "enterprise"

# root key id -> Ed25519 public key, hex: the deployment-signing root keys
# EDOS holds, the same ids and keys as the Go table (DefaultRoots). Only an
# attestation one of them signed admits an origin outside the registry. At
# most two, for rotation. A test root is never added here.
#
# edos-ome-deployment-root-1: created 2026-11-13 in AWS KMS
# (ECC_NIST_EDWARDS25519, non-exportable; the id is the key's alias). The DER
# SubjectPublicKeyInfo from `aws kms get-public-key` has SHA-256
# f1f37079b9024c4a403e11c5042fe55df180bac58b4b6b12f7f7afa21d3a53b4.
ROOT_KEYS = {
    "edos-ome-deployment-root-1": "f6c3ca076bdf7aa97ec07f13207517af5187270cbb3ed0bd307f985fc1e6706e",
}

_KEY_ID = re.compile(r"[a-z0-9-]{1,64}")
_PUBLIC = re.compile(r"[0-9a-f]{64}")
_TIME = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,9})?Z")
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


class _Refused(ValueError):
    """The reason is a fixed string and never quotes the envelope."""


def _unique(pairs):
    out = {}
    for name, value in pairs:
        if name in out:
            raise _Refused("repeated key")
        out[name] = value
    return out


def _constant(name):
    raise _Refused("not JSON")


def _object(raw):
    """One JSON object from strict UTF-8 bytes: no byte order mark, no other
    encoding (json.loads would detect UTF-16 by itself), no NaN or Infinity."""
    try:
        text = bytes(raw).decode("utf-8")
    except (UnicodeDecodeError, TypeError, ValueError):
        return None
    if text.startswith("\ufeff"):
        return None
    try:
        value = json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _decode(text):
    """Strict unpadded base64url, as Go's RawURLEncoding.Strict()."""
    if not isinstance(text, str) or not text or not _BASE64URL.fullmatch(text) or len(text) % 4 == 1:
        return None
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != text:
        return None
    return raw


def _nanoseconds(value):
    """Nanoseconds since the epoch for an RFC 3339 UTC timestamp ending in Z, or None.

    Exactly YYYY-MM-DDTHH:MM:SS, an optional fraction of one to nine ASCII
    digits after a full stop, and Z. Integer arithmetic, so one nanosecond
    past a bound is past it."""
    if not isinstance(value, str) or not 20 <= len(value) <= 30:
        return None
    match = _TIME.fullmatch(value)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    try:
        parsed = datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    whole = int((parsed - _EPOCH).total_seconds())
    fraction = (match.group(7) or ".")[1:]
    return whole * 1000000000 + int(fraction.ljust(9, "0") or "0")


def parse_time(value):
    """Seconds since the epoch for an RFC 3339 UTC timestamp ending in Z, or None."""
    nanoseconds = _nanoseconds(value)
    return None if nanoseconds is None else nanoseconds / 1000000000


# The repertoire an organization name may use: fixed code point ranges,
# inclusive, the same list as the Go verifier's. Not "printable characters":
# that depends on the Unicode tables of the interpreter this file runs on (a
# user's own python3), so one signed name could be accepted by one verifier
# and refused by another. The ranges hold no control, format, combining,
# private-use or unassigned code point in any Unicode version, and no
# right-to-left script. A script outside them needs a new verifier release.
_ORGANIZATION_RANGES = (
    (0x0020, 0x007E),  # Basic Latin
    (0x00A1, 0x00AC),  # Latin-1 punctuation and symbols, without the no-break space
    (0x00AE, 0x00FF),  # Latin-1, without the soft hyphen
    (0x0100, 0x024F),  # Latin Extended-A and -B
    (0x0386, 0x038A),  # Greek
    (0x038C, 0x038C),
    (0x038E, 0x03A1),
    (0x03A3, 0x03CE),
    (0x0400, 0x045F),  # Cyrillic
    (0x0490, 0x04FF),
    (0x2013, 0x2014),  # en dash, em dash
    (0x2018, 0x2019),  # single quotation marks
    (0x201C, 0x201D),  # double quotation marks
    (0x3041, 0x3096),  # Hiragana
    (0x30A1, 0x30FC),  # Katakana
    (0x4E00, 0x9FA5),  # CJK Unified Ideographs, the original block
    (0xAC00, 0xD7A3),  # Hangul syllables
)


def _organization_letter(code):
    """A letter or digit of the repertoire: punctuation alone is no name."""
    if 0x30 <= code <= 0x39 or 0x41 <= code <= 0x5A or 0x61 <= code <= 0x7A:
        return True
    if code < 0x00C0 or code in (0x00D7, 0x00F7, 0x0387, 0x30FB, 0x30FC) or 0x2000 <= code <= 0x206F:
        return False
    return True


def _organization(name):
    if not isinstance(name, str) or not 0 < len(name) <= MAX_ORGANIZATION or name[0] == " " or name[-1] == " ":
        return False
    codes = [ord(ch) for ch in name]
    if not all(any(low <= code <= high for low, high in _ORGANIZATION_RANGES) for code in codes):
        return False
    return any(_organization_letter(code) for code in codes)


def _policy_keys(value, origin):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_POLICY_KEYS:
        raise _Refused("policy keys")
    keys = {}
    for item in value:
        if not isinstance(item, dict) or sorted(item) != ["id", "public"]:
            raise _Refused("policy keys")
        key_id, public = item["id"], item["public"]
        if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id) or key_id in keys:
            raise _Refused("policy keys")
        # A deployment's key never takes the name of a compiled key.
        if key_id in plus_policy.PUBLIC_KEYS:
            raise _Refused("policy keys")
        if not isinstance(public, str) or not _PUBLIC.fullmatch(public):
            raise _Refused("policy keys")
        keys[key_id] = (public, origin)
    return keys


def verify(raw, origin, now, roots=None):
    """Return the verified attestation for ``origin`` or raise ValueError.

    ``raw`` is the envelope bytes exactly as served (bytes, never text), ``origin`` the destination
    the user or administrator configured and ``now`` seconds since the epoch.
    ``roots`` defaults to ROOT_KEYS (key id -> public key, bytes or hex).

    The result is ``{"origin", "organization", "root_key_id", "policy_keys",
    "issued_at", "not_after"}``; ``policy_keys`` has the shape of
    plus_policy.PUBLIC_KEYS, each key bound to this origin only, and is what
    to pass as ``keys`` to plus_policy.verify for this connection.
    """
    try:
        return _verify(raw, origin, now, ROOT_KEYS if roots is None else roots)
    except _Refused as refusal:
        raise ValueError(str(refusal)) from None


def _verify(raw, origin, now, roots):
    # 0. The destination itself. A compiled public cell is never attested.
    if not plugin_origins.canonical(origin):
        raise _Refused("destination")
    if plugin_origins.allowed(origin):
        raise _Refused("compiled origin")
    # 1. Envelope shape and bounds.
    if not isinstance(raw, (bytes, bytearray)):
        raise _Refused("envelope fields")
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise _Refused("envelope size")
    try:
        envelope = _object(raw)
    except _Refused:
        envelope = None
    if envelope is None or sorted(envelope) != ["key_id", "payload", "signature"] or not isinstance(envelope["key_id"], str):
        raise _Refused("envelope fields")
    payload = _decode(envelope["payload"])
    if payload is None or len(payload) > MAX_PAYLOAD_BYTES:
        raise _Refused("payload size")
    signature = _decode(envelope["signature"])
    if signature is None or len(signature) != 64:
        raise _Refused("signature size")
    # 2. A compiled root signed exactly these payload bytes.
    root = roots.get(envelope["key_id"])
    if isinstance(root, str):
        root = bytes.fromhex(root) if re.fullmatch(r"[0-9a-f]{64}", root) else None
    if root is None or len(root) != 32:
        raise _Refused("unknown root")
    if not plus_policy.ed25519_verify(bytes(root), payload, signature):
        raise _Refused("signature")
    verified = _check_payload(payload, origin, now)
    verified["root_key_id"] = envelope["key_id"]
    return verified


def check_payload(payload, origin, now):
    """Apply every payload rule of ``verify`` with no envelope or signature.

    For a signing tool to run BEFORE a root key signs: a root key must never
    sign bytes a plugin would refuse, or bytes that are not an attestation.
    ``payload`` is the exact bytes to be signed. Returns what ``verify``
    returns, without ``root_key_id``, or raises ValueError."""
    try:
        if not plugin_origins.canonical(origin):
            raise _Refused("destination")
        if plugin_origins.allowed(origin):
            raise _Refused("compiled origin")
        if not isinstance(payload, (bytes, bytearray)) or not 0 < len(payload) <= MAX_PAYLOAD_BYTES:
            raise _Refused("payload size")
        return _check_payload(payload, origin, now)
    except _Refused as refusal:
        raise ValueError(str(refusal)) from None


def _check_payload(payload, origin, now):
    # 3. Exactly the seven keys, each once, and version 1.
    try:
        body = _object(payload)
    except _Refused:
        body = None
    if body is None or sorted(body) != ["issued_at", "kind", "not_after", "organization", "origin", "policy_keys", "version"]:
        raise _Refused("payload fields")
    if type(body["version"]) is not int or body["version"] != VERSION:
        raise _Refused("payload version")
    if not all(isinstance(body[name], str) for name in ("origin", "organization", "kind")):
        raise _Refused("payload fields")
    # 4. The statement is about this destination and nothing else.
    if body["origin"] != origin:
        raise _Refused("origin")
    if body["kind"] != KIND:
        raise _Refused("kind")
    if not _organization(body["organization"]):
        raise _Refused("organization")
    # 5. One or two policy keys, each named once, none shadowing a compiled key.
    try:
        keys = _policy_keys(body["policy_keys"], origin)
    except _Refused:
        raise _Refused("policy keys") from None
    # 6. Validity, in whole nanoseconds.
    issued = _nanoseconds(body["issued_at"])
    if issued is None:
        raise _Refused("issued_at")
    not_after = _nanoseconds(body["not_after"])
    if not_after is None:
        raise _Refused("not_after")
    current = int(round(now * 1000000000))
    if not not_after > issued or not_after - issued > MAX_LIFETIME * 1000000000:
        raise _Refused("lifetime")
    if issued > current + FUTURE_SKEW * 1000000000:
        raise _Refused("not yet valid")
    if not current < not_after:
        raise _Refused("expired")
    issued, not_after = issued / 1000000000, not_after / 1000000000
    return {"origin": origin, "organization": body["organization"], "policy_keys": keys,
            "issued_at": issued, "not_after": not_after}
