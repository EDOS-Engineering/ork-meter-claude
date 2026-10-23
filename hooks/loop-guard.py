"""PostToolUseFailure hook: notice the same tool call failing again and again.

Counts failures per session in the store's ``failures.json``, keyed by the
SHA-256 of ``tool_name`` plus the JSON of ``tool_input`` (the input itself is
not stored), under ``failures.json.lock``. At the ``loop_guard`` lever's
``warn`` count it tells the person and Claude; at its optional ``stop`` count
it returns ``continue: false``. Thresholds: hooks/lib/features.py. Counters
reset at session start and after compaction (hooks/trim-session.py).
"""

import json
import time

import features
import hook
import ledger
import session

EVENTS = ("PostToolUseFailure",)
FEATURE = "loop_guard"


def handle(ctx):
    payload, env = ctx.payload, ctx.env
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or payload.get("is_interrupt") is True:
        return None
    key = ledger.sha256_hex(tool + "\x00" + json.dumps(payload.get("tool_input"), sort_keys=True, default=str))
    path = ctx.store().failures_path
    # Parallel tool calls fail in parallel: without the lock two hooks read
    # the same count and one increment is lost.
    with session._lock(path + ".lock"):
        counts = session.read_json(path, {})
        if not isinstance(counts, dict):
            counts = {}
        entry = counts.get(key) if isinstance(counts.get(key), dict) else {"count": 0, "tool": tool}
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["tool"] = tool
        entry["last_ts"] = int(time.time())
        counts[key] = entry
        session.write_json(path, counts)

    count = entry["count"]
    warn_at = features.value(env, "loop_guard", "warn")
    stop_at = features.value(env, "loop_guard", "stop")
    reply = {}
    if count == warn_at or (count > warn_at and (count - warn_at) % warn_at == 0):
        reply.update(hook.system_message(
            "Ork Meter loop guard: the same %s call (identical input) has now failed %d times "
            "this session. Each retry re-sends the failure to the model." % (tool, count)))
        reply.update(hook.additional_context(
            ctx.event,
            "This exact %s input has failed %d times in this session with the same error "
            "path. A different approach is more likely to work than another identical retry."
            % (tool, count)))
        ctx.record(ledger.loop(tool, count, stopped=False))
    if stop_at is not None and count >= stop_at:
        reply.update(hook.system_message(None, stop=(
            "Ork Meter loop guard stopped the session: the same %s call failed %d times "
            "(ORK_METER_LOOP_STOP=%d). Review the failure before continuing." % (tool, count, stop_at))))
        ctx.record(ledger.loop(tool, count, stopped=True))
    return reply or None
