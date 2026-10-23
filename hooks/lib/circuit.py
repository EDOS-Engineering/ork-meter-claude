"""Session-local, tool-specific reduction circuit breakers; no tool content.

Two independent calls with the same internal failure category disable that
reducer path. Immutable fault files preserve concurrent observations without
read/modify/write races. Uncertain diagnostics also restore pass-through for
the affected path. Exact recovery remains outside this automatic mechanism.
"""
import hashlib
import json
import os
import time

import features
import session

THRESHOLD = 2
MAX_RECORDS = 32
MAX_BYTES = 4096
SCHEMA_VERSION = 1
SCOPES = {**{"trim-tool-output:" + name: ("trim",) for name in ("Bash", "Grep", "Glob", "WebFetch")},
          "dedupe-reads:Read": ("trim", "dedupe"), "read-guard:Read": ("read_guard",),
          "command-capture:Bash": ("trim",),
          # No lever family: when the Write guard is stopped, Read compaction
          # stops too (dedupe-reads.py, read-guard.py ask), so no compacted view
          # is left without its guard.
          "write-guard:Write": ()}


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def scope(reducer, tool):
    key = str(reducer) + ":" + str(tool)
    return key if key in SCOPES else None


def read(store, key):
    result = {"scope": key, "status": "available", "disabled": False, "faults": 0}
    try:
        directory = session.fs_path(store.reducer_health_path(sha(key)))
        if not os.path.lexists(directory):
            return result
        if (session.check_private(os.lstat(directory), directory=True)
                or session.check_private(os.lstat(os.path.dirname(directory)), directory=True)):
            raise ValueError("invalid diagnostic directory")
        groups = {}
        records = []
        latched = False
        scanned = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_RECORDS * 2 + 1:
                    raise ValueError("diagnostic scan limit reached")
                if entry.name.startswith(".ork-meter-") and entry.name.endswith(".tmp"):
                    continue  # Atomic publication has not made this observation visible.
                is_latch = entry.name == session.REDUCER_DISABLED_NAME
                if len(records) >= MAX_RECORDS and not is_latch:
                    raise ValueError("diagnostic limit reached")
                if not is_latch and (not entry.name.endswith(".json") or len(entry.name) != 69):
                    raise ValueError("unknown diagnostic entry")
                info = entry.stat(follow_symlinks=False)
                if session.check_private(info) or info.st_size > MAX_BYTES:
                    raise ValueError("invalid diagnostic file")
                row = json.loads(session.read_bounded(entry.path, MAX_BYTES))
                if is_latch:
                    if (not isinstance(row, dict) or type(row.get("schema_version")) is not int
                            or row["schema_version"] != SCHEMA_VERSION or row.get("scope") != key or row.get("disabled") is not True):
                        raise ValueError("invalid disable marker")
                    latched = True
                    continue
                if (not isinstance(row, dict) or type(row.get("schema_version")) is not int or row["schema_version"] != SCHEMA_VERSION
                        or row.get("scope") != key or row.get("request_sha256") + ".json" != entry.name
                        or not isinstance(row.get("category"), str) or not isinstance(row.get("error_type"), str)
                        or not isinstance(row.get("policy_sha256"), (str, type(None)))):
                    raise ValueError("invalid diagnostic record")
                signature = (row["category"], row["error_type"], row["policy_sha256"])
                groups[signature] = groups.get(signature, 0) + 1
                records.append(row)
        result.update(faults=len(records), disabled=latched or any(count >= THRESHOLD for count in groups.values()))
        result["status"] = "disabled" if result["disabled"] else ("watching" if records else "available")
        result["failure_categories"] = sorted({row["category"] + ":" + row["error_type"] for row in records})
    except (OSError, ValueError, TypeError, KeyError):
        result.update(status="unavailable", disabled=True, faults=None)
    return result


def state(store):
    entries = [read(store, key) for key in sorted(SCOPES)]
    disabled = {entry["scope"] for entry in entries if entry["disabled"]}
    families = sorted(feature for feature in {f for values in SCOPES.values() for f in values}
                      if all(key in disabled for key, values in SCOPES.items() if feature in values))
    affected = [entry for entry in entries if entry["status"] != "available"]
    digest = sha(json.dumps(affected, sort_keys=True, separators=(",", ":")))
    return {"status": "partial" if any(entry["status"] == "unavailable" for entry in entries) else "ok",
            "entries": affected, "disabled_features": families, "sha256": digest}


def environment(store, env):
    value = state(store)
    return features.RuntimeEnvironment(env, value["disabled_features"]), value


def record(store, key, identity, policy_hash, category, error_type, session_id=None):
    """One immutable observation per tool-call identity, never raw error text."""
    if key not in SCOPES or not isinstance(identity, str) or not identity:
        return
    directory = store.reducer_health_path(sha(key), create=True)
    request_hash = sha(identity)
    row = {"schema_version": SCHEMA_VERSION, "session_id": session_id, "scope": key,
           "request_sha256": request_hash, "policy_sha256": policy_hash,
           "category": category, "error_type": error_type, "observed_at_ns": time.time_ns()}
    session.initialize_json(os.path.join(directory, request_hash + ".json"), row)
    if read(store, key)["status"] == "disabled":
        session.initialize_json(store.reducer_disabled_path(sha(key)),
                                {"schema_version": SCHEMA_VERSION, "session_id": session_id,
                                 "scope": key, "disabled": True, "threshold": THRESHOLD})


def fault(ctx, key, category, error):
    if key not in SCOPES:
        return
    call = ctx.payload.get("tool_use_id")
    if not isinstance(call, str) or not call:
        return  # Missing identity cannot prove an independent failing tool call.
    try:
        identity = json.dumps([ctx.payload.get("agent_id"), call])
        record(ctx.store(), key, identity, ctx.session_policy_sha256, category, type(error).__name__,
               ctx.payload.get("session_id"))
        effective, value = environment(ctx.store(), ctx.env)
        features.refresh_features_file(effective, ctx.payload["session_id"])
    except (OSError, ValueError, TypeError, KeyError):
        pass


def bind(ctx, reducer):
    key = scope(reducer, ctx.payload.get("tool_name"))
    ctx.reducer_scope = key
    if not isinstance(ctx.payload.get("session_id"), str) or not ctx.payload["session_id"]:
        return False
    ctx.env, value = environment(ctx.store(), ctx.env)
    ctx.reducer_health_sha256 = value["sha256"]
    try:
        features.refresh_features_file(ctx.env, ctx.payload["session_id"])
    except (OSError, ValueError):
        pass
    return key is not None and disabled(value, key)


def disabled(value, key):
    return any(row["scope"] == key and row["disabled"] for row in value["entries"])


class InvariantError(ValueError):
    pass


def validate_reply(ctx, reply):
    """Reject changes to execution status before any replacement is emitted."""
    if reply is None or reply == {}:
        return
    if not isinstance(reply, dict):
        raise InvariantError("invalid hook reply shape")
    specific = reply.get("hookSpecificOutput")
    if not isinstance(specific, dict) or "updatedToolOutput" not in specific:
        return
    original, updated = ctx.payload.get("tool_response"), specific["updatedToolOutput"]
    if type(original) is not type(updated):
        raise InvariantError("replacement response shape changed")
    if isinstance(original, dict):
        for name in ("exitCode", "exit_code", "code", "status", "interrupted", "is_interrupt", "isImage", "is_error", "isError",
                     "backgroundTaskId", "timedOutAfterMs", "noOutputExpected"):
            if name in original and (name not in updated or type(original[name]) is not type(updated[name]) or original[name] != updated[name]):
                raise InvariantError("replacement execution status changed")
