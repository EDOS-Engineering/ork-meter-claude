"""The compiled Ork Meter origins a plugin may pair with and send to.

Canonical source: EDOS-Engineering/ork-meter docs/plugin-origins.md. The Claude
Code and Codex plugins vendor this exact file; a contract test in each checks
the copy's digest. The registry below is byte for byte the file the Go runtime
and helper embed (internal/pluginorigins/origins.json); a test there fails if
the two differ. No network, no dependencies.

This registry lists the public cells. An origin is added in a reviewed change
to that repository and reaches users in a plugin release. An exact string
match is the only test: a trailing slash, a port, another scheme or a
look-alike host is refused. Nothing here is learned from the network, from a
server response or from a project's settings.

An Ork Meter Enterprise deployment is not listed. It is admitted, for one
connection, by a deployment attestation EDOS signed for exactly that origin
(docs/deployment-attestation.md); this file knows nothing about those.

Kinds: "public" is an Ork Meter cell with accounts and billing; "enterprise"
is reserved for an EDOS-operated deployment that is compiled in, where
entitlement comes from membership. A "legacy" origin is an earlier hostname kept so
existing connections still load; a new pairing never starts against it.
"policy_keys" are the ids of the keys that may sign a lever policy for the
origin (plus_policy.PUBLIC_KEYS); none means no policy can verify, so every
lever stays off and usage tracking still works.
"""
import json
import re

VERSION = 1
KIND_PUBLIC = "public"
KIND_ENTERPRISE = "enterprise"
DEFAULT_ENVIRONMENT = "production"

# BEGIN REGISTRY (internal/pluginorigins/origins.json, verbatim)
REGISTRY_JSON = r'''
{
  "version": 1,
  "origins": [
    {"origin": "https://orkmeter.com", "kind": "public", "environment": "production", "legacy": false, "policy_keys": ["orkmeter-2026-11"]},
    {"origin": "https://ork-meter.edos.io", "kind": "public", "environment": "dev", "legacy": false, "policy_keys": ["orkmeter-staging-2026-11"]},
    {"origin": "https://dev.ork-meter.edos.io", "kind": "public", "environment": "dev", "legacy": true, "policy_keys": []}
  ]
}
'''
# END REGISTRY

# A lower-case DNS name of at least two labels whose last begins with a
# letter: never an IP address, a single-label host, a port or a path.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(r"(?=.{1,253}\Z)(?:%s\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?" % _LABEL)
_FIELDS = ("origin", "kind", "environment", "legacy", "policy_keys")


def canonical(origin):
    """Whether ``origin`` is exactly "https://" plus a lower-case DNS name.

    It says nothing about whether the origin is allowed."""
    return (isinstance(origin, str) and origin.startswith("https://")
            and _HOST.fullmatch(origin[len("https://"):]) is not None)


def parse(text):
    """Validate a version 1 registry and return its entries, in order."""
    document = json.loads(text)
    if not isinstance(document, dict) or sorted(document) != ["origins", "version"]:
        raise ValueError("registry must hold exactly version and origins")
    if type(document["version"]) is not int or document["version"] != VERSION:
        raise ValueError("registry version is not %d" % VERSION)
    entries = document["origins"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("registry has no origins")
    seen, current = set(), {}
    for entry in entries:
        if not isinstance(entry, dict) or sorted(entry) != sorted(_FIELDS):
            raise ValueError("registry entry has the wrong fields")
        origin, keys = entry["origin"], entry["policy_keys"]
        if not canonical(origin):
            raise ValueError("%r is not an exact https origin" % (origin,))
        if origin in seen:
            raise ValueError("%s listed twice" % origin)
        seen.add(origin)
        if entry["kind"] not in (KIND_PUBLIC, KIND_ENTERPRISE):
            raise ValueError("%s: kind %r" % (origin, entry["kind"]))
        environment = entry["environment"]
        if not isinstance(environment, str) or not environment or environment.strip() != environment:
            raise ValueError("%s: environment %r" % (origin, environment))
        if type(entry["legacy"]) is not bool:
            raise ValueError("%s: legacy is not a boolean" % origin)
        if (not isinstance(keys, list) or len(keys) > 2
                or any(not isinstance(key, str) or not key for key in keys)):
            raise ValueError("%s: policy_keys" % origin)
        if entry["legacy"] and keys:
            raise ValueError("%s: a legacy origin has no policy key" % origin)
        if not entry["legacy"]:
            current[environment] = current.get(environment, 0) + 1
    for entry in entries:
        if current.get(entry["environment"]) != 1:
            raise ValueError("environment %r needs exactly one origin that is not legacy" % entry["environment"])
    return tuple(entries)


_ENTRIES = parse(REGISTRY_JSON)
_BY_ORIGIN = {entry["origin"]: entry for entry in _ENTRIES}


def _copy(entry):
    return dict(entry, policy_keys=list(entry["policy_keys"]))


def allowed(origin):
    """Whether ``origin`` is in the registry: an exact string match."""
    return isinstance(origin, str) and origin in _BY_ORIGIN


def lookup(origin):
    """The entry for ``origin``, or None."""
    entry = _BY_ORIGIN.get(origin) if isinstance(origin, str) else None
    return _copy(entry) if entry else None


def is_enterprise(origin):
    """Whether ``origin`` is an Ork Meter Enterprise deployment."""
    entry = lookup(origin)
    return bool(entry) and entry["kind"] == KIND_ENTERPRISE


def for_environment(environment):
    """The origin a new pairing starts against for ``environment``, or None.

    A legacy origin is never returned."""
    for entry in _ENTRIES:
        if entry["environment"] == environment and not entry["legacy"]:
            return entry["origin"]
    return None


def origins_for_environment(environment):
    """Every origin of ``environment``, the current one first, then legacy ones.

    For reading a saved connection that names only its environment."""
    rows = [entry for entry in _ENTRIES if entry["environment"] == environment]
    return tuple(entry["origin"] for entry in sorted(rows, key=lambda entry: entry["legacy"]))


def default_origin():
    """The origin used when nothing selects another."""
    return for_environment(DEFAULT_ENVIRONMENT)


def policy_key_ids(origin):
    """The ids of the keys that may sign a lever policy for ``origin``."""
    entry = lookup(origin)
    return tuple(entry["policy_keys"]) if entry else ()


def all_origins():
    """Every entry, in registry order."""
    return tuple(_copy(entry) for entry in _ENTRIES)
