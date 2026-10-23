"""SessionEnd hook: delete delta bases, apply tool-output retention
(hooks/lib/retention.py), publish levers and write the session's savings
summary.

Writes the session store's ``session-summary.json`` (0600): the ledger summary
(``ledger.summarise``, estimates) and the session's measured token usage from
the transcript (hooks/lib/measure.py). SessionEnd hooks share a 1.5-second
budget, so the scan stops after one second and says ``partial``;
``scripts/savings.py`` can finish it later.
"""

import json
import time

import features
import circuit
import ledger
import measure
import retention
import session

EVENTS = ("SessionEnd",)


def build(payload, store, deadline, env=None):
    scan = ledger.scan(store)
    estimated = ledger.summarise(scan["rows"])
    gaps = store.accounting_gaps()
    if gaps or scan["status"] != "ok":
        estimated["totals"]["net_bytes_avoided"] = None
        estimated["totals"]["est_net_tokens_avoided"] = None
    measured = measure.measure_transcript(payload.get("transcript_path"), deadline)
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": payload.get("session_id"),
        "reason": payload.get("reason"),
        "transcript_path": payload.get("transcript_path"),
        "features": features.enabled_features(env),
        "estimated_tokens_avoided": estimated,
        "measured_from_transcript": measured,
        "accounting_gaps": gaps,
        "accounting_status": "partial" if gaps else scan["status"],
        "ledger_read_issues": scan["issues"],
    }


def handle(ctx):
    deadline = time.monotonic() + min(1.0, ctx.remaining())
    # Delta bases are full copies of tool output, useful only while the
    # conversation is live; a resumed session starts with none (reset on
    # SessionStart). Delete them first, whatever the lever setting.
    try:
        ctx.store().forget_delta_bases()
    except (OSError, ValueError):
        pass
    # Saved tool output: this session's on clear/logout, everyone's past its
    # retention (hooks/lib/retention.py). Bounded; never raises.
    retention.at_session_end(ctx)
    try:
        features.write_features_file(ctx.env, session_id=ctx.payload.get("session_id"), ended=True)
    except OSError:
        pass
    store = ctx.store()
    summary = build(ctx.payload, store, deadline, env=ctx.env)
    summary["session_policy_sha256"] = ctx.session_policy_sha256
    summary["policy_sha256"] = ctx.policy_sha256
    summary["reducer_health"] = circuit.state(store)
    session.write_private(store.summary_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return None
