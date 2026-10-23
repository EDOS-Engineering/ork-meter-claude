"""UserPromptSubmit hook: suggest /clear or /compact when the context is large.

The context-size nudge (``context_hygiene`` lever; switch, default and
tunables in hooks/lib/features.py, per-token default in hooks/lib/billing.py).
It reads the current context size from the transcript tail
(``measure.context_tokens``, no message content) and, past the threshold,
shows the person a ``systemMessage``; nothing is added to the model's context,
so the prompt cache is untouched. Later nudges wait for the context to grow by
``step``, at most ``MAX_PER_SESSION`` a session. Logged as ``context_nudge``;
hooks/trim-session.py logs a following clear or compact as accepted.
Design: docs/token-optimization.md, "Context-size nudge".
"""

import time

import features
import hook
import ledger
import measure
import session

EVENTS = ("UserPromptSubmit",)
FEATURE = "context_hygiene"
NUDGE_DIR = "context-nudges"
MAX_PER_SESSION = 3


def nudge_path(env, session_id):
    return features.session_state_path(env, NUDGE_DIR, session_id)


def stale_guard_holds(ctx, lines):
    """True when the stale-context guard (hooks/stale-context.py)
    is on and would hold this prompt: it speaks for this prompt, so this hook
    neither shows a second message nor takes credit for its /clear or
    /compact advice. ``lines`` is the transcript tail already read."""
    env = ctx.env
    if not features.enabled(env, "stale_guard") or features.unattended(env, ctx.payload):
        return False
    turn = measure.last_turn(ctx.payload.get("transcript_path"), time.monotonic() + ctx.remaining() * 0.5, lines)
    return measure.stale_idle(turn, features.value(env, "stale_guard", "tokens")) is not None


def handle(ctx):
    payload, env = ctx.payload, ctx.env
    session_id = payload.get("session_id")
    path = nudge_path(env, session_id)
    if path is None:
        return None
    lines = measure.tail_lines(payload.get("transcript_path"))   # read once for both measures
    if lines is None:
        return None
    tokens = measure.context_tokens(payload.get("transcript_path"), time.monotonic() + ctx.remaining() * 0.5, lines)
    if tokens is None or tokens < features.value(env, "context_hygiene", "tokens"):
        return None
    if stale_guard_holds(ctx, lines):
        return None
    state = session.read_json(path)
    if not isinstance(state, dict) or state.get("session_id") != session_id:
        state = {"session_id": session_id, "nudges": 0, "nudged_at_tokens": 0}
    nudges = state.get("nudges") if isinstance(state.get("nudges"), int) else 0
    last = state.get("nudged_at_tokens") if isinstance(state.get("nudged_at_tokens"), int) else 0
    if nudges >= MAX_PER_SESSION or (nudges and tokens < last + features.value(env, "context_hygiene", "step")):
        return None
    state.update(nudges=nudges + 1, nudged_at_tokens=int(tokens), ts=time.time())
    session.write_json(path, state)
    ctx.record(ledger.context_nudge(tokens))
    return hook.system_message(
        "Ork Meter: this conversation now re-sends about %s tokens of context on every turn. "
        "If your next task is unrelated, /clear and restate it; to keep going, /compact with a focus, "
        "for example \"/compact keep the current task's files and decisions\". "
        "ORK_METER_CONTEXT_HYGIENE=off turns these suggestions off." % "{:,}".format(int(tokens)))
