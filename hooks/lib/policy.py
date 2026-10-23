"""Immutable per-session configuration, separate from the hosted feature contract.

Atomic first-writer publication makes concurrent hooks use the same winner.
Immediate opt-outs only reduce activity; a changed mode never starts content
capture or reduction. Broken or incompatible state restores pass-through.
"""
import hashlib
import json
import os

import billing
import features
import ledger
import project_env
import session
import circuit

MAX_BYTES = 16 * 1024
SCHEMA_VERSION = 1


def digest(version, settings):
    raw = json.dumps({"plugin_version": version, "settings": settings}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def snapshot(env, session_id):
    version = features.plugin_version(env)
    settings = features.policy_settings(env)
    return {"schema_version": SCHEMA_VERSION, "session_id": session_id, "plugin_version": version,
            "settings": settings, "policy_sha256": digest(version, settings)}


def read(store, session_id, env):
    """Strict, bounded load; never silently replace a damaged policy with defaults."""
    info = os.lstat(session.fs_path(store.policy_path))
    problem = session.check_private(info)
    if problem == "type" or info.st_size > MAX_BYTES:
        raise ValueError("policy must be a bounded private regular file")
    if problem:
        raise PermissionError("policy must be owner-only")
    value = json.loads(session.read_bounded(session.fs_path(store.policy_path), MAX_BYTES))
    if (not isinstance(value, dict) or set(value) != {"schema_version", "session_id", "plugin_version", "settings", "policy_sha256"}
            or type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION
            or value["session_id"] != session_id or value["plugin_version"] != features.plugin_version(env)):
        raise ValueError("policy identity, version or schema mismatch")
    settings = value["settings"]
    if (not isinstance(settings, dict) or features.MODE_ENV not in settings
            or any(not isinstance(key, str) or not isinstance(item, str) for key, item in settings.items())
            or value["policy_sha256"] != digest(value["plugin_version"], settings)):
        raise ValueError("invalid policy settings or integrity")
    adapted = adapt(settings)
    if adapted != features.policy_settings(adapted):
        raise ValueError("invalid policy settings or integrity")
    return dict(value, settings=adapted)


def switches():
    """Every lever on/off setting: opt-out switches and opt-in switches."""
    return {name for name, (kind, tunable) in features.SETTING_KINDS.items()
            if kind in ("opt_out", "opt_in") and tunable is None}


def switch_tunables():
    """Tunables that act as on/off switches (unit ``"switch"``, 0 = off)."""
    return {name for name, (kind, tunable) in features.SETTING_KINDS.items() if kind == "opt_in" and tunable}


def adapt(settings):
    """Read a pin written by a build with a different set of levers (#105).

    A plugin update that adds a lever setting must not turn every running
    session to pass-through. Pinned settings keep their values; a lever
    switch the pin doesn't have reads ``off``, so an update never starts a
    lever mid-session; a tunable the pin doesn't have reads its current
    default; settings this build doesn't know are ignored. Integrity has
    already been checked against the settings exactly as pinned.

    Missing switches read off on purpose, including a new opt-out added to
    an existing lever: that lever stops for the rest of the session rather
    than changing behaviour mid-session. This relies on every lever with
    tunables having a switch (tests/features.test.sh checks): a tunable-only
    lever would read its default here and could start mid-session. A tunable
    that is itself an on/off control (unit ``"switch"``) reads ``"0"`` when
    missing, whatever its current default, for the same reason. Pins from
    another plugin version are not adapted at all; ``read`` rejects them and
    the session passes through (logged once as ``policy_fallback``)."""
    current = features.policy_settings({})
    named, switched = switches(), switch_tunables()
    def missing(key, default):
        if key in named:
            return "off"
        return "0" if key in switched else default
    return {key: settings[key] if key in settings else missing(key, default) for key, default in current.items()}


def pin_environment(env, payload):
    """The values a new pin records: ``env`` with the per-token defaults
    (issue #110) applied when a lever that has one is left unset. Billing is
    detected only then, once per session, from metadata (hooks/lib/billing.py);
    any failure leaves the lever off."""
    if not features.needs_billing(env):
        return env
    mode, _basis = billing.detect(env, payload)
    return features.carried(features.with_billing_defaults(env, mode == billing.PER_TOKEN), env)


def resolve(store, env, session_id, initialize=False, payload=None):
    """Return effective environment, saved pin and actual configuration hash."""
    try:
        if initialize and not os.path.lexists(session.fs_path(store.policy_path)):
            session.initialize_json(store.policy_path, snapshot(pin_environment(env, payload), session_id))
        saved = read(store, session_id, env)
        effective = features.apply_policy_settings(env, saved["settings"])
        return effective, saved["policy_sha256"], digest(saved["plugin_version"], features.policy_settings(effective))
    except (OSError, ValueError, TypeError) as exc:
        record_fallback(store, session_id, exc)
        return features.pass_through_environment(env), None, None


FALLBACK_MARKER = "policy-fallback-recorded"


def record_fallback(store, session_id, exc):
    """Say once per session that its pin could not be used (#105), so a
    session never runs without savings silently. Best effort; never raises."""
    try:
        if not os.path.lexists(session.fs_path(store.policy_path)):
            return
        marker = os.path.join(os.path.dirname(store.policy_path), FALLBACK_MARKER)
        fd = os.open(session.fs_path(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        ledger.append(store, ledger.stamp(ledger.policy_fallback(exc.__class__.__name__), session_id))
    except Exception:
        pass


def bind(ctx, feature=None, publish=True):
    # hooks/lib/hook.py gates first (features.gate): the Plus verdict and the
    # signed policy ride on ctx.env and are kept through the plain dicts the
    # project rule and the pin produce, so a new pin records the policy's
    # defaults (features.policy_settings) and the gate survives the pin.
    gated = ctx.env
    # A project's settings may only turn levers off (#143 item 4): resolve
    # before the pin is written, so it never records a project-enabled lever.
    ctx.env, ctx.ignored_project_settings = project_env.trusted(ctx.env, ctx.payload)
    ctx.env = features.carried(ctx.env, gated)
    identity = ctx.payload.get("session_id")
    if not isinstance(identity, str) or not 0 < len(identity) <= 256:
        return
    live = ctx.env
    store = session.Store.existing_for_payload(ctx.payload, live)
    if store is None:
        store = ctx.store()
    ctx.env, ctx.session_policy_sha256, ctx.policy_sha256 = resolve(store, live, identity, initialize=True,
                                                                    payload=ctx.payload)
    ctx.env = features.carried(ctx.env, gated)
    # Meter flags follow effective behavior even when opt-outs skip a handler.
    try:
        if publish:
            features.refresh_features_file(ctx.env, identity)
    except (OSError, ValueError):
        pass


def for_store(store, env):
    """Explicit command capture shares an existing session pin when available.
    Lever settings a project tried to turn on are resolved as in ``bind``."""
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = None
    payload = {"cwd": cwd}
    env = project_env.trusted(env, payload)[0]
    if store is None:
        return env
    identity = store.session_info().get("session_id")
    if not isinstance(identity, str) or not identity:
        return features.pass_through_environment(env) if os.path.lexists(session.fs_path(store.policy_path)) else env
    return resolve(store, env, identity, initialize=True, payload=payload)[0]


def report(store, env, rows=()):
    """Read-only pin/observation diagnostics; no current-behavior inference."""
    def valid(value):
        return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
    hashes = sorted({row["policy_sha256"] for row in rows if valid(row.get("policy_sha256"))})
    result = {"status": "unavailable", "observed_policy_sha256": hashes,
              "rows_without_policy_identity": sum(not valid(row.get("policy_sha256")) for row in rows),
              "reducer_health": circuit.state(store)}
    try:
        saved = read(store, store.session_info().get("session_id"), env)
        result.update(status="pinned", session_policy_sha256=saved["policy_sha256"],
                      plugin_version=saved["plugin_version"], settings=saved["settings"])
    except (OSError, ValueError, TypeError) as exc:
        result["reason"] = exc.__class__.__name__
    return result
