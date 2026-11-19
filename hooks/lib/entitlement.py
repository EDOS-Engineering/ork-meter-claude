"""Is this computer's Ork Meter account allowed to run the savings levers,
and under which lever policy?

Optimizations are an Ork Meter Plus feature (ork-meter #261); free Ork Meter
is visibility only. The plugin runtime (ork-meter ``internal/pluginruntime``)
asks the platform at most every 15 minutes for a **signed policy envelope**
(``GET /v1/plus/policy/claude-code``; the contract is ork-meter
docs/plus-policy.md and its verifier is vendored verbatim as
hooks/lib/plus_policy.py) and keeps, in ``$CLAUDE_PLUGIN_DATA``, owner-only:

* ``plus-policy.json``: the envelope bytes verbatim, at most 8 KiB;
* ``connection.json``: exactly ``{"reference": "<public pairing reference>",
  "origin": "https://..."}``, the pairing the envelope is bound to; the
  runtime removes both files on disconnect;
* ``entitlement.json``: the four plain fields of the verified entitlement,
  ``{"optimize", "state", "until", "checked_at"}``, for status lines and
  older readers.

The hooks never touch the network. ``resolve`` reads those files and verifies
the envelope on every read (Ed25519 in pure Python, about 4 ms); nothing on
disk is trusted without it:

* no plugin data directory, no files at all, or ``connection.json`` alone
  (paired, no answer cached yet) means not connected: not entitled;
* an envelope that fails verification (signature, key, issuer other than the
  pairing origin, audience other than the pairing reference, another
  plugin's, stale, malformed), one without ``connection.json``, or any of
  the files that is not a small owner-only regular file of the right shape:
  not entitled, every lever off;
* entitled means the envelope verifies, ``optimize`` is true and now is at
  most ``OFFLINE_GRACE`` past ``until`` (``plus_policy.entitled``); the
  envelope's own life (a day and a quarter from issue) bounds any replay;
* legacy: no envelope but ``entitlement.json`` (an older runtime that still
  asks ``GET /v1/entitlement``) keeps today's rules on that file until
  ``LEGACY_UNTIL`` (2026-11-30T00:00:00Z); from then on it is not entitled.

The verified envelope's ``policy`` document (``policy``) is the lever
policy hooks/lib/features.py applies while entitled: which levers are
available and the tunables' defaults. Metering and capture never read these
files: usage keeps flowing either way.

Ork Meter Enterprise. A connection whose origin is not a compiled public cell
(hooks/lib/plugin_origins.py, vendored from ork-meter) is to an enterprise
deployment. The hooks verify for themselves, on every read, that EDOS vouches
for exactly that origin now: the runtime keeps the deployment's attestation in
``$CLAUDE_PLUGIN_DATA/attestations/<host>.json`` and
hooks/lib/deployment_attestation.py (vendored, the contract is ork-meter
docs/deployment-attestation.md) verifies it against the compiled root keys.
The envelope then verifies only with the policy keys that attestation names,
each bound to that origin. No compiled key signs for an enterprise origin, and
there is no unsigned legacy answer for one. Without a current attestation the
connection is not entitled. Entitlement there comes from the organization, so
nothing shown for it mentions Plus or billing.
"""

import calendar
import datetime
import json
import os
import time

import deployment_attestation
import plugin_origins
import plus_policy
import session

FILE_NAME = "entitlement.json"
POLICY_FILE = "plus-policy.json"
CONNECTION_FILE = "connection.json"
ATTESTATION_DIR = "attestations"
PLUGIN = "claude-code"
MAX_BYTES = 1024
CONNECTION_MAX_BYTES = 1024
OFFLINE_GRACE = 24 * 3600
# The unsigned entitlement.json alone entitles only before this instant.
LEGACY_UNTIL_TEXT = "2026-11-30T00:00:00Z"
LEGACY_UNTIL = calendar.timegm((2026, 11, 30, 0, 0, 0))
STATES = ("promo", "trial", "active", "past_due", "free")
FIELDS = {"optimize", "state", "until", "checked_at"}

ENTITLED = "entitled"
NOT_ENTITLED = "not_entitled"
NOT_CONNECTED = "not_connected"

MESSAGE = ("Ork Meter savings are off: they are part of Ork Meter Plus. "
           "Upgrade at https://orkmeter.com/billing")
# Ork Meter Enterprise: entitlement comes from the organization. Never Plus,
# purchasing or a billing address.
ENTERPRISE_MESSAGE = ("Ork Meter savings are off: your organization has not turned them on for this account, "
                      "or its Ork Meter Enterprise deployment could not be verified. Ask your administrator.")


def data_dir(env):
    data = env.get("CLAUDE_PLUGIN_DATA")
    if not isinstance(data, str) or not data or not os.path.isabs(data):
        return None
    return data


def path(env):
    """The legacy ``entitlement.json`` path, or None without a data directory."""
    directory = data_dir(env)
    return None if directory is None else os.path.join(directory, FILE_NAME)


def _timestamp(value):
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ValueError("invalid time")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None or "T" not in value.upper():
        raise ValueError("time without zone")
    return parsed.timestamp()


def _private(file_path, limit, what):
    """The bytes of ``file_path``: a regular, owner-only file of this user's
    holding at most ``limit`` bytes, or raise."""
    info = os.lstat(file_path)
    problem = session.check_private(info)
    if problem == "type" or info.st_size > limit:
        raise ValueError("%s must be a small regular file" % what)
    if problem:
        raise PermissionError("%s must be owner-only" % what)
    return session.read_bounded(file_path, limit)


def _unique(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate field")
    return dict(pairs)


def read(file_path):
    """The validated legacy cached answer as a dict, or raise."""
    value = json.loads(_private(file_path, MAX_BYTES, "entitlement"), object_pairs_hook=_unique)
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("entitlement fields")
    if type(value["optimize"]) is not bool or value["state"] not in STATES:
        raise ValueError("entitlement values")
    if value["optimize"] and value["state"] == "free":
        raise ValueError("free does not optimize")
    checked = _timestamp(value["checked_at"])
    until = None if value["until"] is None else _timestamp(value["until"])
    return {"optimize": value["optimize"], "state": value["state"], "until": until, "checked_at": checked}


def read_connection(file_path):
    """``(reference, origin)`` from ``connection.json``: exactly those two
    keys, a non-empty printable reference and an ``https`` origin with no
    path, or raise. Whether the origin may be trusted is decided by
    ``policy_keys``, not here."""
    value = json.loads(_private(file_path, CONNECTION_MAX_BYTES, "connection"), object_pairs_hook=_unique)
    if not isinstance(value, dict) or set(value) != {"reference", "origin"}:
        raise ValueError("connection fields")
    reference, origin = value["reference"], value["origin"]
    if (not isinstance(reference, str) or not 0 < len(reference) <= 128 or not reference.isascii()
            or not reference.isprintable() or any(c.isspace() for c in reference)):
        raise ValueError("connection reference")
    host = origin[len("https://"):] if isinstance(origin, str) and origin.startswith("https://") else ""
    if not host or len(origin) > 256 or not origin.isascii() or any(c in host for c in "/?#\\") or any(c.isspace() for c in host):
        raise ValueError("connection origin")
    return reference, origin


def enterprise(origin):
    """Whether ``origin`` is an Ork Meter Enterprise deployment: any origin
    that is neither a compiled public cell nor the issuer of a compiled
    policy key (the contract's test origin). Only an attestation admits it."""
    if plugin_origins.allowed(origin):
        return False
    return not any(issuer == origin for _, issuer in plus_policy.PUBLIC_KEYS.values())


def attestation(directory, origin, now):
    """The verified deployment attestation for exactly ``origin`` from the
    file the runtime keeps for it, or raise. Nothing is fetched here."""
    if not plugin_origins.canonical(origin):
        raise ValueError("connection origin")
    host = origin[len("https://"):]
    raw = _private(os.path.join(directory, ATTESTATION_DIR, host + ".json"),
                   deployment_attestation.MAX_ENVELOPE_BYTES, "deployment attestation")
    return deployment_attestation.verify(raw, origin, now)


def policy_keys(directory, origin, now):
    """The only keys that may sign this connection's lever policy: None (the
    compiled table, each key bound to its own cell) for a public cell, and
    for an enterprise deployment the keys its attestation names, or raise
    when EDOS does not vouch for that origin now."""
    if not enterprise(origin):
        return None
    return attestation(directory, origin, now)["policy_keys"]


def verify(env, now):
    """The verified envelope (``plus_policy.verify``) for this pairing, or raise."""
    directory = data_dir(env)
    if directory is None:
        raise ValueError("no plugin data directory")
    reference, origin = read_connection(os.path.join(directory, CONNECTION_FILE))
    keys = policy_keys(directory, origin, now)
    raw = _private(os.path.join(directory, POLICY_FILE), plus_policy.MAX_ENVELOPE_BYTES, "policy envelope")
    return plus_policy.verify(raw, PLUGIN, reference, origin, now, keys=keys)


def connected_to_enterprise(env):
    """Whether the saved connection is to an Ork Meter Enterprise deployment,
    for wording only. Never raises; an unreadable connection is not one."""
    try:
        directory = data_dir(env)
        if directory is None:
            return False
        return enterprise(read_connection(os.path.join(directory, CONNECTION_FILE))[1])
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return False


def message(env):
    """What to tell the person when the levers are off for lack of
    entitlement: the Plus line, or for an enterprise connection a line that
    names neither Plus nor billing."""
    return ENTERPRISE_MESSAGE if connected_to_enterprise(env) else MESSAGE


def _current(answer, now):
    bound = answer["until"] if answer["until"] is not None else answer["checked_at"]
    return bool(answer["optimize"]) and now <= bound + OFFLINE_GRACE


def resolve(env, now=None):
    """``(status, policy)``: ``ENTITLED``, ``NOT_ENTITLED`` (connected, no
    Plus, a failed envelope or too long offline) or ``NOT_CONNECTED``, and
    the verified envelope's policy document (None without a verified
    envelope, entitled or not). The one place the files are read, once
    per call. Never raises."""
    now = time.time() if now is None else now
    try:
        directory = data_dir(env)
        if directory is None:
            return NOT_CONNECTED, None
        if os.path.lexists(os.path.join(directory, POLICY_FILE)):
            verified = verify(env, now)
            state = ENTITLED if plus_policy.entitled(verified, now, OFFLINE_GRACE) else NOT_ENTITLED
            return state, verified["policy"]
        legacy = os.path.join(directory, FILE_NAME)
        if os.path.lexists(legacy):
            if now >= LEGACY_UNTIL or connected_to_enterprise(env):
                # An enterprise deployment never had unsigned answers.
                return NOT_ENTITLED, None
            return (ENTITLED if _current(read(legacy), now) else NOT_ENTITLED), None
        return NOT_CONNECTED, None
    except (OSError, ValueError, TypeError, OverflowError, KeyError, RecursionError):
        return NOT_ENTITLED, None


def status(env, now=None):
    """``ENTITLED``, ``NOT_ENTITLED`` or ``NOT_CONNECTED``. Never raises."""
    return resolve(env, now)[0]


def policy(env, now=None):
    """The verified envelope's lever policy document, or None. Never raises."""
    return resolve(env, now)[1]
