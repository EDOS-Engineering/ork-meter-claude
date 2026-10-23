"""SessionStart hook: reset state, publish levers, nudge, status.

On every SessionStart it resets the repeat-read index, the loop-guard counters
and delta bases, and writes the active-lever file
(hooks/lib/features.py). On SessionStart it also prunes saved tool output
(hooks/lib/retention.py) and, on startup, per-session state files untouched
for ``features.STATE_PRUNE_SECONDS`` (30 days). It shows the person one
``systemMessage``: the trim status line on startup and resume (``features.status_line``), the Plus notice,
project settings it ignored (hooks/lib/project_env.py), and the cache-miss
nudge when Claude Code reports ``prompt_cache_likely_expired`` on a large
context (``cache_nudge`` lever). It logs nudges, and a ``clear`` or
``compact`` start soon after a nudge as accepted. Design:
docs/token-optimization.md, "Cache-miss nudge". The metering notice stays in
hooks/session-start.sh.

Compaction is covered by SessionStart alone: Claude Code 2.1.302 fires
``SessionStart`` with source ``compact`` after both a manual ``/compact`` and
an automatic compaction, so the state resets once per compaction. The hook is
not on ``PostCompact`` (#268): it reset the store a second time, and the CLI
appends every PostCompact hook's command text to the ``/compact`` result
("Compacted PostCompact [...] completed successfully").
"""

import os
import time

import features
import hook
import ledger
import project_env
import retention
import session

EVENTS = ("SessionStart",)
NUDGE_DIR = "cache-nudges"


def nudge_path(env, session_id):
    """Pending nudges are independent for every originating session."""
    return features.session_state_path(env, NUDGE_DIR, session_id)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def cache_nudge(ctx, now):
    """The nudge text, or None. Logs the nudge and remembers it for 'accepted'."""
    payload, env = ctx.payload, ctx.env
    if not features.enabled(env, "cache_nudge"):
        return None
    expired = payload.get("prompt_cache_likely_expired")
    tokens = payload.get("context_tokens")
    if expired is not True or not _number(tokens):
        return None
    threshold = features.value(env, "cache_nudge", "tokens")
    if tokens < threshold:
        return None
    usd = payload.get("estimated_cache_write_usd")
    cost = (" Claude Code estimates the cache rewrite at $%.2f." % usd) if _number(usd) else ""
    text = (
        "Ork Meter: this resumed conversation re-sends about %s tokens of context and its prompt cache has likely "
        "expired, so the next turn writes all of it to the cache again.%s Options: /clear and restate the task "
        "(cheapest when the earlier context isn't needed), or continue and pay the rewrite once."
        % ("{:,}".format(int(tokens)), cost)
    )
    ctx.record(ledger.cache_nudge(tokens, usd if _number(usd) else None))
    state = {"ts": now, "session_id": payload.get("session_id"), "context_tokens": int(tokens)}
    if _number(usd):
        state["est_cache_write_usd"] = float(usd)
    path = nudge_path(env, payload.get("session_id"))
    if path:
        session.write_json(path, state)
    return text


def nudge_accepted(ctx, now):
    """A /clear soon after a nudge counts as accepting it (an estimate)."""
    env = ctx.env
    path = nudge_path(env, ctx.payload.get("session_id"))
    if path is None:
        return
    state = session.read_json(path)
    if not isinstance(state, dict):
        return
    sid = ctx.payload.get("session_id")
    if not isinstance(sid, str) or not sid or state.get("session_id") != sid:
        return
    try:
        os.unlink(path)
    except OSError:
        pass
    window = features.value(env, "cache_nudge", "accept_seconds")
    ts = state.get("ts")
    if not _number(ts) or now - ts > window or now < ts:
        return
    usd = state.get("est_cache_write_usd")
    ctx.record(ledger.cache_nudge_accepted(state.get("context_tokens") or 0, state.get("session_id"),
                                           usd if _number(usd) else None))


CONTEXT_NUDGE_DIR = "context-nudges"
# Every hook's per-session state (features.session_state_path), pruned at
# startup once untouched for features.STATE_PRUNE_SECONDS.
STATE_DIRS = (NUDGE_DIR, CONTEXT_NUDGE_DIR, features.STALE_GUARD_DIR)


def context_nudge_accepted(ctx, now, source):
    """A /clear or /compact soon after a context nudge (hooks/context-nudge.py) counts as accepting it, an inference from timing."""
    env = ctx.env
    sid = ctx.payload.get("session_id")
    if session.safe_name(sid) is None or not features.enabled(env, "context_hygiene"):
        return
    path = features.session_state_path(env, CONTEXT_NUDGE_DIR, sid, create=False)
    state = session.read_json(path)
    if not isinstance(state, dict) or state.get("session_id") != sid or state.get("accepted_ts"):
        return
    ts = state.get("ts")
    if not _number(ts) or now < ts or now - ts > features.value(env, "context_hygiene", "accept_seconds"):
        return
    state["accepted_ts"] = now   # one acceptance per nudge
    session.write_json(path, state)
    ctx.record(ledger.context_nudge_accepted(state.get("nudged_at_tokens") or 0, sid, source))


def handle(ctx):
    payload, env = ctx.payload, ctx.env
    ctx.store().reset()
    try:
        features.write_features_file(env, session_id=payload.get("session_id"))
    except OSError:
        pass
    if ctx.event != "SessionStart":
        return None
    # Saved tool output of other sessions past its age or over the size cap
    # (hooks/lib/retention.py); time-bounded and never raises.
    retention.at_session_start(ctx)
    now = time.time()
    source = payload.get("source")
    if source == "startup":
        features.prune_session_state(env, STATE_DIRS, now)
    messages = []
    if source in ("startup", "resume"):
        line = features.status_line(env)
        if line:
            messages.append(line)
        ignored = project_env.notice(getattr(ctx, "ignored_project_settings", ()))
        if ignored:
            messages.append(ignored)
    if source == "clear":
        nudge_accepted(ctx, now)
    if source in ("clear", "compact"):
        context_nudge_accepted(ctx, now, source)
    nudge = cache_nudge(ctx, now)
    if nudge:
        messages.append(nudge)
    if messages:
        return hook.system_message("\n".join(messages))
    return None
