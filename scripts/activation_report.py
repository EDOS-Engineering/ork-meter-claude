"""Which Ork Meter features fire in real use: aggregates over every session
store on this machine (issue #300, Phase 0).

Usage: activation_report.py [--since <date>] [--until <date>] [--json] [--temp-base <dir> ...]

Finds every session store in both layouts (hooks/lib/session.py,
``Store.all``), ledger or not, and reads three files from each with the
bounded, link-refusing reader (``session.read_bounded``), only when the
store directory is owner-only and the file is this user's own 0600 file:

* the savings ledger: ``kind``, ``ts``, ``est_tokens_avoided``,
  ``reducer_version`` and ``policy_version`` of each line;
* the session policy pin: ``plugin_version``;
* the session summary: ``ts`` and ``features`` (the levers on at the end).

It prints, per plugin version: sessions seen, sessions with a firing per
kind, events per kind, the sum, median and 90th percentile of
``est_tokens_avoided`` per firing, nudges shown and followed up, Haiku agent
runs, and the time range covered. Counts only: no percentages, no savings
claims, and nothing that names a session, a path, a hash or any content.
Token figures are the ledger's ceil(bytes/4) estimates, not measurements.

``--since`` and ``--until`` take a UTC date (``2026-10-20``, ``--until``
inclusive of that day) or a time (``2026-10-20T12:00:00Z``). A ledger line
counts when its ``ts`` is in range; a session counts when any of its
recorded times is. ``--temp-base`` replaces the temp directories searched
(repeatable): for reading a copy of another machine's stores, and for tests.

Not recorded by the plugin, so not reported: skill invocations
(``/ork-meter:explore``, ``/ork-meter:map`` ...) and runs of agents other
than the parked Haiku agents. Reads local files only; sends nothing.
"""

import argparse
import datetime
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks/lib"))

import features  # noqa: E402
import ledger    # noqa: E402
import policy    # noqa: E402
import session   # noqa: E402

LEDGER_LIMIT = 64 * 1024 * 1024
SUMMARY_LIMIT = 4 * 1024 * 1024
SCHEMA = "ork-meter-activation/1"
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,31}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
UNKNOWN = "unknown"
OTHER_KIND = "other"
AGENTS = ("ork-meter:explore", "ork-meter:logs", "ork-meter:test-runner")
# (nudge, shown kind, follow-up kind, what the follow-up means)
NUDGES = (
    ("cache_nudge", "cache_nudge", "cache_nudge_accepted", "/clear within the window"),
    ("context_nudge", "context_nudge", "context_nudge_accepted", "/clear or /compact within the window"),
    ("stale_guard", "stale_guard", "stale_guard_continued", "prompt resubmitted as is"),
    ("loop_guard", "loop_warning", "loop_stop", "stopped after the warning"),
)
NOT_RECORDED = (
    "skill invocations: the plugin has no hook on skill use, so explore/map/savings invocations are not counted",
    "agent runs other than the parked Haiku agents (haiku_agent)",
    "static per-request cost of each feature (Phase 0 step 2 measures it separately)",
)
NOTE = ("Absolute counts. est_tokens_avoided is the ledger's ceil(bytes/4) estimate over avoided tool-output "
        "bytes: not the model's tokenizer, not a measured saving, not a billing figure.")


def parse_time(text, end=False):
    """A UTC datetime from ``YYYY-MM-DD`` (the day's end with ``end``) or
    ``YYYY-MM-DDTHH:MM:SS[Z]``; ValueError otherwise."""
    text = text.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        day = datetime.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        return day + datetime.timedelta(days=1, microseconds=-1) if end else day
    value = datetime.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def row_time(value):
    """The ledger's ``ts`` as a UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.strptime(value, TS_FORMAT).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def fmt_time(value):
    return value.strftime(TS_FORMAT) if value else None


def clean_version(value):
    return value if isinstance(value, str) and VERSION_RE.match(value) else UNKNOWN


def clean_kind(value):
    if value in ledger.KINDS:
        return value
    return value if isinstance(value, str) and KIND_RE.match(value) else OTHER_KIND


class Issues:
    """Counts of what could not be read, by reason; never which file."""

    def __init__(self):
        self.counts = {}

    def add(self, reason):
        self.counts[reason] = self.counts.get(reason, 0) + 1


def private_store(store, issues):
    try:
        problem = session.check_private(os.lstat(session.fs_path(store.path)), directory=True)
    except OSError:
        issues.add("store_unreadable")
        return False
    if problem:
        issues.add("store_not_private")
        return False
    return True


def read_private(path, limit, issues, what):
    """The bytes of an owner-only regular file, or None (absent, a link,
    group- or world-readable, over ``limit``, or unreadable)."""
    try:
        info = os.lstat(session.fs_path(path))
    except FileNotFoundError:
        return None
    except OSError:
        issues.add(what + "_unreadable")
        return None
    problem = session.check_private(info)
    if problem:
        issues.add(what + "_not_private")
        return None
    try:
        return session.read_bounded(session.fs_path(path), limit, follow=False)
    except ValueError:
        issues.add(what + "_over_limit_or_changed")
    except OSError:
        issues.add(what + "_unreadable")
    return None


def read_object(path, limit, issues, what):
    raw = read_private(path, limit, issues, what)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        issues.add(what + "_malformed")
        return None
    return value if isinstance(value, dict) else None


def read_rows(store, issues):
    """The ledger's valid lines (duplicates of one tool call removed as in
    ``ledger.summarise``) and how many lines were ignored."""
    raw = read_private(store.ledger_path, LEDGER_LIMIT, issues, "ledger")
    if raw is None:
        return [], 0, 0
    rows, ignored = [], 0
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            ignored += 1
            continue
        if not ledger.valid_row(row):
            ignored += 1
            continue
        rows.append(row)
    rows, duplicates = ledger.split_duplicates(rows)
    return rows, ignored, len(duplicates)


def load_store(store, issues):
    """One session reduced to what the report may use: no id, no path."""
    rows, ignored, duplicates = read_rows(store, issues)
    pin = read_object(store.policy_path, policy.MAX_BYTES, issues, "policy") or {}
    summary = read_object(store.summary_path, SUMMARY_LIMIT, issues, "summary")
    enabled = None
    times = [t for t in (row_time(r.get("ts")) for r in rows) if t]
    if isinstance(summary, dict):
        if isinstance(summary.get("features"), list):
            enabled = sorted({f for f in summary["features"] if f in features.ALL_FEATURES})
        if row_time(summary.get("ts")):
            times.append(row_time(summary["ts"]))
    if not times:
        try:
            mtime = os.lstat(session.fs_path(store.policy_path if pin else store.path)).st_mtime
            times.append(datetime.datetime.fromtimestamp(int(mtime), datetime.timezone.utc))
        except OSError:
            pass
    return {"plugin_version": clean_version(pin.get("plugin_version")),
            "rows": [{"kind": clean_kind(r["kind"]), "time": row_time(r.get("ts")),
                      "est_tokens_avoided": r["est_tokens_avoided"],
                      "versions": "%s/%s" % (clean_version(r.get("reducer_version")),
                                             clean_version(r.get("policy_version"))),
                      "agent": r.get("agent_type") if r.get("agent_type") in AGENTS else None}
                     for r in rows],
            "times": times, "features": enabled, "ignored": ignored, "duplicates": duplicates,
            "has_ledger": os.path.isfile(session.fs_path(store.ledger_path))}


def in_range(moment, since, until):
    return moment is not None and (since is None or moment >= since) and (until is None or moment <= until)


def percentile(values, fraction):
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def byte_kind(kind, rows):
    if kind in ledger.KINDS:
        return ledger.KINDS[kind].counts == "bytes"
    return any(r["est_tokens_avoided"] > 0 for r in rows)


def aggregate(sessions, since=None, until=None):
    """The report's JSON: ``sessions`` are ``load_store`` results."""
    filtered = since is not None or until is not None
    groups = {}
    first = last = None
    totals = {"sessions_seen": 0, "ledger_lines_ignored": 0, "duplicate_call_rows": 0,
              "ledger_rows_outside_range": 0}
    for one in sessions:
        if filtered and not any(in_range(t, since, until) for t in one["times"]):
            continue
        rows = [r for r in one["rows"] if not filtered or in_range(r["time"], since, until)]
        totals["sessions_seen"] += 1
        totals["ledger_lines_ignored"] += one["ignored"]
        totals["duplicate_call_rows"] += one["duplicates"]
        totals["ledger_rows_outside_range"] += len(one["rows"]) - len(rows)
        times = [t for t in one["times"] if not filtered or in_range(t, since, until)]
        group = groups.setdefault(one["plugin_version"], {
            "sessions": 0, "with_ledger": 0, "with_firing": 0, "kinds": {}, "versions": {},
            "features": {}, "with_feature_record": 0, "first": None, "last": None, "agents": {}})
        group["sessions"] += 1
        group["with_ledger"] += 1 if one["has_ledger"] else 0
        group["with_firing"] += 1 if rows else 0
        for moment in times:
            group["first"] = moment if group["first"] is None or moment < group["first"] else group["first"]
            group["last"] = moment if group["last"] is None or moment > group["last"] else group["last"]
        if one["features"] is not None:
            group["with_feature_record"] += 1
            for feature in one["features"]:
                group["features"][feature] = group["features"].get(feature, 0) + 1
        kinds_here = set()
        for row in rows:
            bucket = group["kinds"].setdefault(row["kind"], {"rows": [], "sessions": 0})
            bucket["rows"].append(row)
            kinds_here.add(row["kind"])
            group["versions"][row["versions"]] = group["versions"].get(row["versions"], 0) + 1
            if row["agent"]:
                group["agents"][row["agent"]] = group["agents"].get(row["agent"], 0) + 1
        for kind in kinds_here:
            group["kinds"][kind]["sessions"] += 1
    out_groups = []
    for version in sorted(groups, key=_version_key):
        group = groups[version]
        kinds = {}
        order = [k for k in ledger.ORDER if k in group["kinds"]] + sorted(k for k in group["kinds"]
                                                                         if k not in ledger.KINDS)
        for kind in order:
            rows = group["kinds"][kind]["rows"]
            entry = {"counts_as": "intervention" if byte_kind(kind, rows) else "event",
                     "events": len(rows), "sessions_with_firing": group["kinds"][kind]["sessions"]}
            if entry["counts_as"] == "intervention":
                values = [r["est_tokens_avoided"] for r in rows]
                entry["est_tokens_avoided"] = {"sum": sum(values), "median_per_firing": statistics.median(values),
                                               "p90_per_firing": percentile(values, 0.9)}
            kinds[kind] = entry
        count = lambda kind: kinds.get(kind, {}).get("events", 0)  # noqa: E731
        nudges = {name: {"shown": count(shown), "followed": count(followed), "follow_up": meaning}
                  for name, shown, followed, meaning in NUDGES if count(shown) or count(followed)}
        out_groups.append({
            "plugin_version": version, "ledger_versions": dict(sorted(group["versions"].items())),
            "sessions_seen": group["sessions"], "sessions_with_ledger": group["with_ledger"],
            "sessions_with_any_firing": group["with_firing"],
            "time_range": {"first": fmt_time(group["first"]), "last": fmt_time(group["last"])},
            "kinds": kinds, "nudges": nudges, "haiku_agent_runs": dict(sorted(group["agents"].items())),
            "sessions_with_feature_record": group["with_feature_record"],
            "features_enabled_sessions": dict(sorted(group["features"].items()))})
        for moment in (group["first"], group["last"]):
            if moment is not None:
                first = moment if first is None or moment < first else first
                last = moment if last is None or moment > last else last
    return {"schema": SCHEMA, "filter": {"since": fmt_time(since), "until": fmt_time(until)},
            "time_range": {"first": fmt_time(first), "last": fmt_time(last)}, "totals": totals,
            "groups": out_groups, "not_recorded": list(NOT_RECORDED), "note": NOTE}


def _version_key(version):
    parts = re.split(r"[.+-]", version)
    return (version == UNKNOWN, [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts])


def fmt(value):
    if value is None:
        return "—"
    if isinstance(value, float) and not value.is_integer():
        return "{:,.1f}".format(value)
    return "{:,}".format(int(value))


def markdown(report, issues=None):
    lines = []
    span = report["time_range"]
    lines.append("Ork Meter activation: %d sessions seen, %s to %s%s." % (
        report["totals"]["sessions_seen"], span["first"] or "—", span["last"] or "—",
        "" if not any(report["filter"].values()) else
        " (filter: since %s, until %s)" % (report["filter"]["since"] or "—", report["filter"]["until"] or "—")))
    lines.append("")
    lines.append("| plugin | reducer/policy | sessions seen | with a ledger | with a firing | first | last |")
    lines.append("|---|---|---:|---:|---:|---|---|")
    for g in report["groups"]:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            g["plugin_version"], ", ".join(g["ledger_versions"]) or "—", fmt(g["sessions_seen"]),
            fmt(g["sessions_with_ledger"]), fmt(g["sessions_with_any_firing"]),
            (g["time_range"]["first"] or "—")[:10], (g["time_range"]["last"] or "—")[:10]))
    lines.append("")
    lines.append("| plugin | kind | events | sessions with a firing | est. tokens avoided | median per firing | p90 per firing |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for g in report["groups"]:
        for kind, k in g["kinds"].items():
            est = k.get("est_tokens_avoided") or {}
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                g["plugin_version"], kind, fmt(k["events"]), fmt(k["sessions_with_firing"]),
                fmt(est.get("sum")), fmt(est.get("median_per_firing")), fmt(est.get("p90_per_firing"))))
    if any(g["nudges"] for g in report["groups"]):
        lines.append("")
        lines.append("| plugin | nudge | shown | followed | follow-up recorded |")
        lines.append("|---|---|---:|---:|---|")
        for g in report["groups"]:
            for name, n in g["nudges"].items():
                lines.append("| %s | %s | %s | %s | %s |" % (g["plugin_version"], name, fmt(n["shown"]),
                                                          fmt(n["followed"]), n["follow_up"]))
    lines.append("")
    for g in report["groups"]:
        if g["sessions_with_feature_record"]:
            lines.append("Features on at session end, %s (%d sessions with a summary): %s." % (
                g["plugin_version"], g["sessions_with_feature_record"],
                ", ".join("%s %d" % item for item in g["features_enabled_sessions"].items()) or "none"))
    totals = report["totals"]
    lines.append("Ignored: %d malformed or invalid ledger lines, %d duplicate tool-call lines%s." % (
        totals["ledger_lines_ignored"], totals["duplicate_call_rows"],
        ", %d lines outside the range" % totals["ledger_rows_outside_range"] if any(report["filter"].values()) else ""))
    if issues:
        lines.append("Not read: %s." % ", ".join("%s %d" % item for item in sorted(issues.items())))
    lines.append("Not recorded: " + "; ".join(report["not_recorded"]) + ".")
    lines.append(report["note"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="UTC date or time; earlier lines and sessions are left out")
    parser.add_argument("--until", help="UTC date (inclusive) or time; later lines and sessions are left out")
    parser.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    parser.add_argument("--temp-base", action="append", metavar="DIR",
                        help="search this temp directory instead of the system ones (repeatable)")
    args = parser.parse_args(argv)
    try:
        since = parse_time(args.since) if args.since else None
        until = parse_time(args.until, end=True) if args.until else None
    except ValueError:
        parser.error("--since and --until take YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ")
    issues = Issues()
    stores = session.Store.all(os.environ, bases=args.temp_base, every=True)
    sessions = [load_store(store, issues) for store in stores if private_store(store, issues)]
    report = aggregate(sessions, since, until)
    report["not_read"] = dict(sorted(issues.counts.items()))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(markdown(report, report["not_read"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
