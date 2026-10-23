"""UserPromptSubmit hook: hold a prompt that would re-send an expired cache.

The stale-context guard (``stale_guard`` lever, opt-in; tunables in
hooks/lib/features.py). When the last main-conversation turn
(``measure.last_turn``: token counts, time and cache lifetime, no message
content) is older than its cache lifetime and its context is over the
threshold, the hook blocks this one prompt and offers ``/clear``, a focused
``/compact`` or resubmitting. At most once per idle episode, keyed by the last
turn's time, so a resubmission goes through (``stale_guard_continued``). An
unattended prompt (``features.unattended``: a non-interactive session, a
background task's notification, another session's message) is never held:
nobody would resubmit it. An error never blocks a prompt. Design: docs/token-optimization.md,
"Stale-context guard".
"""

import time

import features
import ledger
import measure
import session

EVENTS = ("UserPromptSubmit",)
FEATURE = "stale_guard"
STATE_DIR = features.STALE_GUARD_DIR


def state_path(env, session_id):
    return features.session_state_path(env, STATE_DIR, session_id)


def idle_text(minutes):
    if minutes < 120:
        return "%d minutes" % minutes
    if minutes < 48 * 60:
        return "%d hours" % (minutes // 60)
    return "%d days" % (minutes // (24 * 60))


def handle(ctx):
    payload, env = ctx.payload, ctx.env
    if features.unattended(env, payload):
        return None
    session_id = payload.get("session_id")
    path = state_path(env, session_id)
    if path is None:
        return None
    turn = measure.last_turn(payload.get("transcript_path"), time.monotonic() + ctx.remaining() * 0.5)
    idle = measure.stale_idle(turn, features.value(env, "stale_guard", "tokens"))
    if idle is None:
        return None
    minutes = int(idle // 60)
    state = session.read_json(path)
    if isinstance(state, dict) and state.get("session_id") == session_id and state.get("episode") == turn["timestamp"]:
        # Already held once for this idle episode: the person chose to continue.
        if not state.get("continued"):
            session.write_json(path, dict(state, continued=True, ts=time.time()))
            ctx.record(ledger.stale_guard_continued(turn["tokens"], minutes))
        return None
    session.write_json(path, {"session_id": session_id, "episode": turn["timestamp"], "continued": False,
                              "ts": time.time()})
    ctx.record(ledger.stale_guard(turn["tokens"], minutes))
    return {"decision": "block", "reason": (
        "Ork Meter: this conversation's prompt cache expired (last reply %s ago), so this prompt would re-send "
        "about %s tokens at the cache-write rate. Cheapest: /clear and restate the task. To keep the thread: "
        "/compact with a focus (re-sends once, then continues small). To continue as is: submit the prompt again "
        "(press Up to recall it); it won't be held twice. ORK_METER_STALE_GUARD=off turns this off."
        % (idle_text(minutes), "{:,}".format(int(turn["tokens"]))))}
