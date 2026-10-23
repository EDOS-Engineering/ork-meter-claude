"""Print the Ork Meter token-trim savings for a session: estimates next to
measurements, each labelled.

Usage:
  savings.py --session <session-id>      find the session's store (scratchpad or tempdir fallback)
  savings.py --scratchpad <dir>          use this scratchpad directory
  savings.py --all [--scratchpad <dir>]  aggregate every session found (both layouts)
  savings.py ... --json                  machine-readable output

Reads only local files from the session store (hooks/lib/session.py):
``savings.jsonl`` (estimates), ``session.json`` (the transcript path) and
``session-summary.json``. Sends nothing.
"""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks/lib"))

import ledger    # noqa: E402
import measure   # noqa: E402
import policy    # noqa: E402
import session   # noqa: E402

HEURISTIC = ("Estimated with a 4-bytes-per-token heuristic from avoided tool-output bytes. "
             "Not measured by the model's tokenizer and not a billing figure.")
COLUMNS = "%-12s %13s %14s %13s %15s %20s"


def load_session(store, measure_live=True):
    scan = ledger.scan(store)
    rows = scan["rows"]
    stored = session.read_json(store.summary_path)
    measured = None
    if isinstance(stored, dict) and isinstance(stored.get("measured_from_transcript"), dict):
        measured = stored["measured_from_transcript"]
    info = store.session_info()
    transcript = info.get("transcript_path")
    if measure_live:
        # Resumed sessions can grow after a successful summary was written.
        measured = (measure.measure_transcript(transcript) if transcript else
                    measure.unavailable("no transcript path recorded for this session yet"))
    if measured is None:
        measured = measure.unavailable("no transcript path recorded for this session yet")
    return {"session_id": info.get("session_id"), "rows": rows, "measured": measured,
            "accounting_gaps": store.accounting_gaps(), "ledger_coverage": scan["status"],
            "ledger_read_issues": scan["issues"], "policy": policy.report(store, os.environ, rows)}


def measured_total(sessions):
    """The sessions' measurements added up; unavailable when none had one."""
    total = {"status": "ok", "assistant_messages": 0, "prompt_tokens": 0, "sessions_measured": 0,
             "sessions_total": len(sessions), "sessions_partial": 0, "sessions_unavailable": 0}
    for k in measure.USAGE_KEYS:
        total[k] = 0
    for s in sessions:
        m = s["measured"]
        if m.get("status") == "partial":
            total["sessions_partial"] += 1
        elif m.get("status") != "ok":
            total["sessions_unavailable"] += 1
        if m.get("status") in ("ok", "partial"):
            total["sessions_measured"] += 1
            total["assistant_messages"] += int(m.get("assistant_messages", 0))
            total["prompt_tokens"] += int(m.get("prompt_tokens", 0))
            for k in measure.USAGE_KEYS:
                total[k] += int(m.get(k, 0))
    if total["sessions_measured"] == 0:
        return measure.unavailable("no session had a readable transcript")
    if total["sessions_partial"] or total["sessions_unavailable"]:
        total["status"] = "partial"
    return total


def fmt(n):
    return "{:,}".format(int(n))


def print_table(summary):
    """One line per byte-intervention kind and the summary's totals. Count-only
    events are neither lines nor part of the total (``ledger.summarise``)."""
    header = COLUMNS % ("kind", "interventions", "bytes before", "bytes after", "gross bytes avoided",
                        "gross est. tokens avoided")
    print(header)
    print("-" * len(header))
    for kind, b in summary["by_kind"].items():
        if b["counts_as"] == "intervention":
            print(COLUMNS % (ledger.label(kind), fmt(b["count"]), fmt(b["bytes_before"]), fmt(b["bytes_after"]),
                             fmt(b["bytes_avoided"]), fmt(b["est_tokens_avoided"])))
    t = summary["totals"]
    print(COLUMNS % ("total", fmt(t["interventions"]), fmt(t["bytes_before"]), fmt(t["bytes_after"]),
                     fmt(t["bytes_avoided"]), fmt(t["est_tokens_avoided"])))


def print_measured(measured):
    status = measured.get("status")
    if status in ("ok", "partial"):
        print("Measured from transcript%s: %s assistant messages; input %s, cache read %s, cache creation %s, "
              "output %s tokens (prompt tokens = %s)."
              % (" (partial measurement)" if status == "partial" else "", fmt(measured.get("assistant_messages", 0)),
                 fmt(measured.get("input_tokens", 0)), fmt(measured.get("cache_read_input_tokens", 0)),
                 fmt(measured.get("cache_creation_input_tokens", 0)), fmt(measured.get("output_tokens", 0)),
                 fmt(measured.get("prompt_tokens", 0))))
    else:
        print("Measured from transcript: unavailable (%s)." % measured.get("reason", "unknown"))


def print_events(summary, rows):
    """The count-only events: counts from the summary, details from the rows."""
    runs = ledger.count(summary, "haiku_agent")
    if runs:
        measured_runs = [r for r in rows if r.get("kind") == "haiku_agent"
                         and r.get("measured_status") in ("ok", "partial")]
        total_in = sum(int(r.get("measured_input_tokens", 0)) + int(r.get("measured_cache_read_input_tokens", 0))
                       + int(r.get("measured_cache_creation_input_tokens", 0)) for r in measured_runs)
        total_out = sum(int(r.get("measured_output_tokens", 0)) for r in measured_runs)
        agent_types = sorted(set(str(r.get("agent_type")) for r in rows if r.get("kind") == "haiku_agent"))
        print("Haiku agents: %d runs (%s). Measured inside the agents' own transcripts: %s prompt tokens, %s output "
              "tokens over %d runs; that work stayed out of the main context, which is not itself a measured saving."
              % (runs, ", ".join(agent_types), fmt(total_in), fmt(total_out), len(measured_runs)))
    nudges = ledger.count(summary, "cache_nudge")
    accepted = ledger.count(summary, "cache_nudge_accepted")
    if nudges or accepted:
        usd = sum(float(r.get("est_cache_write_usd_avoided", 0) or 0) for r in rows
                  if r.get("kind") == "cache_nudge_accepted")
        print("Cache nudges: %d shown, %d followed by /clear within the window (estimated cache rewrite avoided: "
              "$%.2f, Claude Code's own estimate)." % (nudges, accepted, usd))
    warnings = ledger.count(summary, "loop_warning")
    stops = ledger.count(summary, "loop_stop")
    if warnings or stops:
        print("Loop guard: %d warnings, %d stops." % (warnings, stops))
    recoveries = ledger.count(summary, "recovery")
    if recoveries:
        returned = summary["by_kind"]["recovery"]["bytes_after"]
        print("Recovery: %d requests returned %s additional bytes; no avoided tokens claimed." %
              (recoveries, fmt(returned)))


def render(as_json, title, summary, measured, fields, rows=None):
    """Print one report from the one ledger summary: as JSON (``fields``, the
    summary as ``estimated``, the measurement), or as the table, the
    measurement and, given ``rows``, the session's events. The estimate's
    label, ``HEURISTIC``, is added here and nowhere else."""
    incomplete = fields.get("accounting_status") not in (None, "ok")
    if incomplete:
        summary = dict(summary, totals=dict(summary["totals"], net_bytes_avoided=None, est_net_tokens_avoided=None))
    if as_json:
        print(json.dumps(dict(fields, estimated=summary, measured=measured, heuristic=HEURISTIC),
                         indent=2, sort_keys=True))
        return
    print(title)
    pin = fields.get("policy")
    if isinstance(pin, dict):
        if pin["status"] == "pinned":
            print("Session optimization policy: %s; %d effective configurations observed in recorded rows." %
                  (pin["session_policy_sha256"][:12], len(pin["observed_policy_sha256"])))
        else:
            print("Session optimization policy: unavailable (%s)." % pin["reason"])
        health = pin.get("reducer_health", {})
        disabled = [entry["scope"] for entry in health.get("entries", []) if entry.get("disabled")]
        if disabled:
            print("Disabled automatic reducers: %s. Exact recovery remains available." % ", ".join(disabled))
        if health.get("status") == "partial":
            print("Reducer diagnostics are incomplete; affected paths use pass-through.")
    print_table(summary)
    duplicates = summary["totals"].get("duplicate_call_rows", 0)
    if duplicates:
        print("Duplicate rows: %d ledger rows repeat a tool call already counted for the same kind "
              "(%s bytes); left out of the table and totals." %
              (duplicates, fmt(summary["totals"].get("duplicate_call_bytes", 0))))
    if incomplete:
        if fields.get("accounting_gap_count"):
            print("Accounting incomplete: %d invocations could not persist every stage; estimates cover recorded stages only." %
                  fields["accounting_gap_count"])
        else:
            print("Accounting incomplete: ledger read coverage is %s; estimates cover valid rows only." %
                  fields.get("ledger_coverage", "partial"))
        print("Net estimate after recovery: unavailable (partial accounting).")
    else:
        totals = summary["totals"]
        print("Net estimate after recorded recovery: %s bytes, %g estimated tokens (bytes/4)." %
              (fmt(totals["net_bytes_avoided"]), totals["est_net_tokens_avoided"]))
    print_measured(measured)
    if rows is not None:
        print_events(summary, rows)
    print(HEURISTIC)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", help="session id (found in the scratchpad layout or the tempdir fallback)")
    parser.add_argument("--scratchpad", help="scratchpad directory of the session")
    parser.add_argument("--all", action="store_true", help="aggregate every session found")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--no-measure", action="store_true", help="don't scan transcripts; use stored summaries only")
    args = parser.parse_args(argv)
    env = os.environ

    store = session.Store.for_scratchpad(args.scratchpad) if args.scratchpad else None
    if store is None and args.session:
        store = session.Store.find(args.session, env)
        if store is None:
            print("No scratchpad found for session %s under %s." % (
                args.session, ", ".join(session.scratchpad_roots(env) + session.fallback_roots(env))))
            return 1
    if store is None and not args.all:
        parser.error("give --session <id>, --scratchpad <dir> or --all")

    if args.all:
        # <root>/<project>/<session>/scratchpad: --scratchpad limits --all to its root
        roots = None
        if args.scratchpad:
            roots = [os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(args.scratchpad))))]
        sessions = [load_session(s, measure_live=not args.no_measure) for s in session.Store.all(env, roots)]
        rows = [row for s in sessions for row in s["rows"]]
        gaps = [gap for s in sessions for gap in s["accounting_gaps"]]
        read_issues = sum(len(s["ledger_read_issues"]) for s in sessions)
        render(args.json, "Ork Meter savings, all sessions found (%d with local records)" % len(sessions),
               ledger.summarise(rows), measured_total(sessions), {"sessions": len(sessions),
               "accounting_gap_count": len(gaps), "ledger_read_issue_count": read_issues,
               "accounting_status": "partial" if gaps or read_issues else "ok",
               "session_policies": [{"session_id": s["session_id"], **s["policy"]} for s in sessions]})
        return 0

    one = load_session(store, measure_live=not args.no_measure)
    summary = ledger.summarise(one["rows"])
    render(args.json,
           "Ork Meter savings, this session%s" % ((" (%s)" % one["session_id"]) if one["session_id"] else ""),
           summary, one["measured"],
           {"session_id": one["session_id"],
            "policy": one["policy"],
            "accounting_gap_count": len(one["accounting_gaps"]),
            "ledger_coverage": one["ledger_coverage"],
            "ledger_read_issues": one["ledger_read_issues"],
            "accounting_status": "partial" if one["accounting_gaps"] else one["ledger_coverage"],
            "haiku_agent_runs": ledger.count(summary, "haiku_agent"),
            "recovery_requests": ledger.count(summary, "recovery"),
            "cache_nudges": ledger.count(summary, "cache_nudge"),
            "cache_nudges_accepted": ledger.count(summary, "cache_nudge_accepted")},
           rows=one["rows"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
