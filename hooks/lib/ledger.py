"""Local savings ledger shared by the Ork Meter token-trim hooks.

The ledger is the session store's ``savings.jsonl`` (hooks/lib/session.py
says where that is). It never records tool content: a ledger line carries
byte counts and token *estimates*, and a dedupe, read-guard or
read-compact line carries the SHA-256 of a file path rather than the path.

``KINDS`` is the ledger's vocabulary: every kind of line, whether it is a
byte intervention (it removed bytes from what the model sees, so it has
bytes and estimated tokens) or a count-only event (zero bytes), its label
and its place in the savings table, which hook writes it and the fields it
carries beyond the common ones. A line is built only by the constructor
named for its kind below (``trimmed``, ``test_filtered``, ``deduped``, ``delta_delivered``,
``recovered``, ``read_guarded``, ``read_compacted``, ``loop``, ``agent``, ``cache_nudge``,
``cache_nudge_accepted``, ``context_nudge``, ``context_nudge_accepted``,
``policy_fallback``, ``stale_guard``, ``stale_guard_continued``); a hook
hands the row to ``ctx.record`` (hooks/lib/hook.py), which ``stamp``s it
with the time, the session id and, for a tool call, ``tool_call_sha256``
(the SHA-256 of the payload's ``tool_use_id``, never the id itself) and
appends it. ``summarise`` is the only reader that totals the ledger; it
counts a byte intervention once per tool call and kind.
tests/ledger.test.sh checks all of this, and that the kind table in
docs/token-optimization.md matches ``KINDS``.

Token estimates use a 4-bytes-per-token heuristic. They are not produced by
the model's tokenizer and are not billing figures. Field names say ``est``
so that nothing downstream can mistake them for measurements.
"""

import collections
import hashlib
import json
import os
import time

import measure
import session

BYTES_PER_TOKEN = 4

# counts: "bytes" (a byte intervention) or "count" (a count-only event, zero bytes);
# label: what the savings table prints for a byte kind, the event's name otherwise;
# constructor: the function below that builds it; writers: the hooks that write it;
# fields: what it carries beyond COMMON_FIELDS, of which ``optional`` are left out when unknown.
Kind = collections.namedtuple("Kind", "counts label constructor writers fields optional")
MEASURED_FIELDS = tuple("measured_" + k for k in measure.USAGE_KEYS) + ("measured_assistant_messages",)

KINDS = {
    "trim": Kind("bytes", "trim", "trimmed", ("trim-tool-output.py", "dedupe-reads.py", "scripts/command.py", "compact-records.py"), (), ()),
    "test_filter": Kind("bytes", "test_filter", "test_filtered", ("trim-tool-output.py",), (), ()),
    "dedupe": Kind("bytes", "dedupe", "deduped", ("dedupe-reads.py",), ("path_sha256",), ()),
    "read_guard": Kind("bytes", "read_guard", "read_guarded", ("read-guard.py", "dedupe-reads.py"), ("path_sha256",), ()),
    "read_compact": Kind("bytes", "read_compact", "read_compacted", ("dedupe-reads.py",),
                         ("path_sha256", "lines_elided"), ()),
    "delta": Kind("bytes", "delta", "delta_delivered", ("trim-tool-output.py", "dedupe-reads.py"), ("hunks",), ()),
    "recovery": Kind("bytes", "recovery", "recovered", ("scripts/artifact.py",), (), ()),
    "haiku_agent": Kind("count", "Haiku agent run", "agent", ("agent-stop.py",),
                        ("agent_type", "measured_status") + MEASURED_FIELDS, MEASURED_FIELDS),
    "cache_nudge": Kind("count", "cache nudge shown", "cache_nudge", ("trim-session.py",),
                        ("context_tokens", "est_cache_write_usd"), ("est_cache_write_usd",)),
    "cache_nudge_accepted": Kind("count", "cache nudge followed by /clear", "cache_nudge_accepted",
                                 ("trim-session.py",),
                                 ("context_tokens", "nudged_session_id", "est_cache_write_usd_avoided"),
                                 ("est_cache_write_usd_avoided",)),
    "context_nudge": Kind("count", "context nudge shown", "context_nudge", ("context-nudge.py",), ("context_tokens",), ()),
    "context_nudge_accepted": Kind("count", "context nudge followed by /clear or /compact", "context_nudge_accepted",
                                   ("trim-session.py",), ("context_tokens", "nudged_session_id", "action"), ()),
    "policy_fallback": Kind("count", "session settings unusable: savings off", "policy_fallback", ("hooks/lib/policy.py",),
                            ("reason",), ()),
    "stale_guard": Kind("count", "stale-context prompt held", "stale_guard", ("stale-context.py",),
                        ("context_tokens", "idle_minutes"), ()),
    "stale_guard_continued": Kind("count", "stale-context prompt resubmitted as is", "stale_guard_continued",
                                  ("stale-context.py",), ("context_tokens", "idle_minutes"), ()),
    "loop_warning": Kind("count", "loop guard warning", "loop", ("loop-guard.py",), ("failures",), ()),
    "loop_stop": Kind("count", "loop guard stop", "loop", ("loop-guard.py",), ("failures",), ()),
}
ORDER = tuple(KINDS)
COMMON_FIELDS = ("ts", "session_id", "tool", "kind", "reducer_version", "policy_version", "bytes_before", "bytes_after",
                 "est_tokens_before", "est_tokens_after", "est_tokens_avoided")
NOTE = ("estimated: ceil(bytes/4) over avoided tool-output bytes; "
        "not the model's tokenizer, not a billing figure; "
        "avoided fields are gross; net includes recorded byte differences and recovery")


def est_tokens(nbytes):
    """ceil(bytes / 4): the plugin's only token estimate."""
    return -(-int(nbytes) // BYTES_PER_TOKEN)


def sha256_hex(text):
    if isinstance(text, str):
        text = text.encode("utf-8", "replace")
    return hashlib.sha256(text).hexdigest()


# --- constructors: the only way a ledger row is built ---------------------

def _row(tool, kind, bytes_before, bytes_after, fields=None):
    before = int(bytes_before)
    after = int(bytes_after)
    row = {
        "tool": tool,
        "kind": kind,
        "reducer_version": "2",
        "policy_version": "recoverable-v1",
        "bytes_before": before,
        "bytes_after": after,
        "est_tokens_before": est_tokens(before),
        "est_tokens_after": est_tokens(after),
        "est_tokens_avoided": max(0, est_tokens(before) - est_tokens(after)),
    }
    if fields:
        row.update(fields)
    return row


def trimmed(tool, before, after):
    """A result over its budget cut to its head and tail (``trim``); ``after``
    includes any note the hook added."""
    return _row(tool, "trim", before, after)


def test_filtered(tool, before, after):
    """Passing-test lines dropped from test-runner output (``test_filter``)."""
    return _row(tool, "test_filter", before, after)


def recovered(tool, returned_bytes):
    """Requested original/selected evidence is additional work, never savings."""
    return _row(tool, "recovery", 0, returned_bytes)


def deduped(before, after, path_sha256):
    """A repeated identical Read shortened to its first lines (``dedupe``)."""
    return _row("Read", "dedupe", before, after, {"path_sha256": path_sha256})


def delta_delivered(tool, before, after, hunks):
    """A re-Read or re-run delivered as a diff against the earlier result
    still in context (``delta``); ``after`` includes the note."""
    return _row(tool, "delta", before, after, {"hunks": int(hunks)})


def read_guarded(size, first_bytes, path_sha256):
    """A whole-file Read paged to its first lines (``read_guard``): before it
    ran, or after it when a compaction of it didn't fit."""
    return _row("Read", "read_guard", size, first_bytes, {"path_sha256": path_sha256})


def read_compacted(before, after, path_sha256, lines_elided):
    """A whole-file Read result with whole lines elided, every shown line
    exact at its own number (``read_compact``); ``after`` includes the note."""
    return _row("Read", "read_compact", before, after, {"path_sha256": path_sha256, "lines_elided": int(lines_elided)})


def loop(tool, failures, stopped):
    """The same failing call warned about (``loop_warning``) or stopped
    (``loop_stop``) after ``failures`` failures."""
    return _row(tool, "loop_stop" if stopped else "loop_warning", 0, 0, {"failures": failures})


def agent(agent_type, measured):
    """One run of a plugin Haiku agent (``haiku_agent``). ``measured`` is
    ``measure.measure_transcript``'s result for the agent's own transcript,
    or None when the payload named none; usage fields are kept only when the
    scan was ``ok`` or ``partial``."""
    fields = {"agent_type": agent_type}
    if measured is None:
        fields["measured_status"] = "unavailable"
    else:
        fields["measured_status"] = measured.get("status")
        if measured.get("status") in ("ok", "partial"):
            for k in measure.USAGE_KEYS + ("assistant_messages",):
                fields["measured_" + k] = measured.get(k, 0)
    return _row("Agent", "haiku_agent", 0, 0, fields)


def cache_nudge(context_tokens, est_cache_write_usd):
    """A cache-miss nudge shown (``cache_nudge``); ``est_cache_write_usd`` is
    Claude Code's estimate, or None when it gave none."""
    fields = {"context_tokens": int(context_tokens)}
    if est_cache_write_usd is not None:
        fields["est_cache_write_usd"] = float(est_cache_write_usd)
    return _row("SessionStart", "cache_nudge", 0, 0, fields)


def cache_nudge_accepted(context_tokens, nudged_session_id, est_cache_write_usd_avoided):
    """A /clear soon after a nudge (``cache_nudge_accepted``); the dollar
    figure is the nudge's estimate, or None when it had none."""
    fields = {"context_tokens": int(context_tokens), "nudged_session_id": nudged_session_id}
    if est_cache_write_usd_avoided is not None:
        fields["est_cache_write_usd_avoided"] = float(est_cache_write_usd_avoided)
    return _row("SessionStart", "cache_nudge_accepted", 0, 0, fields)


def context_nudge(context_tokens):
    """A context-hygiene nudge shown (``context_nudge``)."""
    return _row("UserPromptSubmit", "context_nudge", 0, 0, {"context_tokens": int(context_tokens)})


def policy_fallback(reason):
    """A session's pinned settings could not be used, so its hooks pass
    output through unchanged (``policy_fallback``)."""
    return _row("Policy", "policy_fallback", 0, 0, {"reason": str(reason)[:64]})


def stale_guard(context_tokens, idle_minutes):
    """A prompt held because it would re-send an expired cache (``stale_guard``)."""
    return _row("UserPromptSubmit", "stale_guard", 0, 0,
                {"context_tokens": int(context_tokens), "idle_minutes": int(idle_minutes)})


def stale_guard_continued(context_tokens, idle_minutes):
    """The held prompt resubmitted as is (``stale_guard_continued``)."""
    return _row("UserPromptSubmit", "stale_guard_continued", 0, 0,
                {"context_tokens": int(context_tokens), "idle_minutes": int(idle_minutes)})


def context_nudge_accepted(context_tokens, nudged_session_id, action):
    """A /clear or /compact (``action``) soon after a context nudge
    (``context_nudge_accepted``). "Accepted" is inferred from timing."""
    return _row("SessionStart", "context_nudge_accepted", 0, 0,
                {"context_tokens": int(context_tokens), "nudged_session_id": nudged_session_id, "action": action})


def stamp(row, session_id, tool_use_id=None):
    """``row`` with the current UTC time and the session id in front: the
    line ``append`` writes. Given the hook payload's ``tool_use_id`` (a
    non-empty string), the line also carries ``tool_call_sha256``, its
    SHA-256, so two lines for one tool call can be told apart from two
    identical interventions. Lines written outside a tool-call
    hook (scripts, session events, older versions) have no such field."""
    out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session_id": session_id}
    out.update(row)
    if isinstance(tool_use_id, str) and tool_use_id:
        out["tool_call_sha256"] = sha256_hex(tool_use_id)
    return out


# --- the file -------------------------------------------------------------

def append(store, row):
    """Append one JSON line to the store's ledger (file mode 0600)."""
    fd = os.open(session.fs_path(store.ledger_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def valid_row(row):
    """A ledger line a report may count: an object with a string ``kind`` and
    non-negative integer ``bytes_before``, ``bytes_after`` and
    ``est_tokens_avoided``."""
    return (isinstance(row, dict) and isinstance(row.get("kind"), str)
            and all(type(row.get(k)) is int and row[k] >= 0
                    for k in ("bytes_before", "bytes_after", "est_tokens_avoided")))


def scan(store):
    """Read coverage is part of accounting: damaged/missing data is unknown."""
    rows = []
    issues = []
    try:
        with open(session.fs_path(store.ledger_path), "r", encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    issues.append({"line": number, "reason": "malformed JSON"})
                    continue
                if not valid_row(row):
                    issues.append({"line": number, "reason": "invalid ledger row"})
                    continue
                rows.append(row)
    except (OSError, UnicodeError) as exc:
        issues.append({"reason": exc.__class__.__name__})
        return {"rows": rows, "status": "partial" if rows else "unavailable", "issues": issues}
    return {"rows": rows, "status": "partial" if issues else "ok", "issues": issues}


def load(store):
    """Legacy row-only reader; reports should use scan() for coverage."""
    return scan(store)["rows"]


# --- the one summary ------------------------------------------------------

def label(kind):
    """The savings-table name of ``kind``; an unknown kind is its own label."""
    return KINDS[kind].label if kind in KINDS else kind


def summarise(rows):
    """The ledger's one summary: pure arithmetic on the rows.

    ``by_kind`` maps each kind seen, in ``ORDER`` and then any unknown kind
    in name order, to its ``count``, ``bytes_before``, ``bytes_after``,
    ``bytes_avoided``, ``est_tokens_avoided`` and ``counts_as``:
    ``intervention`` for a byte kind, ``event`` for a count-only kind. A kind
    not in ``KINDS`` (another version's, or a hand-edited line) counts as an
    intervention when its lines carry bytes. ``totals`` has
    ``interventions`` (lines of byte kinds only), ``events`` (lines of
    count-only kinds) and the byte and token sums over the interventions.
    ``note`` says the token figures are estimates.

    A byte-intervention line whose ``tool_call_sha256`` and kind repeat an
    earlier line's is a duplicate: one tool call's output is replaced once,
    so only the first such line is counted. Duplicates are left out of
    ``by_kind`` and of every total, and reported as
    ``totals["duplicate_call_rows"]`` with the ``bytes_before - bytes_after``
    they would have added as ``totals["duplicate_call_bytes"]``. Lines
    without a call id (scripts, older versions) are never duplicates.
    """
    seen = {}
    rows, repeated = split_duplicates(rows)
    duplicates = {"rows": len(repeated),
                  "bytes": sum(_int(row.get("bytes_before")) - _int(row.get("bytes_after")) for row in repeated)}
    for row in rows:
        kind = _kind(row)
        bucket = seen.setdefault(kind, {"count": 0, "bytes_before": 0, "bytes_after": 0,
                                        "bytes_avoided": 0, "est_tokens_avoided": 0})
        before = _int(row.get("bytes_before"))
        after = _int(row.get("bytes_after"))
        bucket["count"] += 1
        bucket["bytes_before"] += before
        bucket["bytes_after"] += after
        bucket["bytes_avoided"] += max(0, before - after)
        bucket["est_tokens_avoided"] += _int(row.get("est_tokens_avoided"))
    by_kind = {}
    totals = {"interventions": 0, "events": 0, "bytes_before": 0, "bytes_after": 0,
              "bytes_avoided": 0, "est_tokens_avoided": 0}
    for kind in [k for k in ORDER if k in seen] + sorted(k for k in seen if k not in KINDS):
        bucket = seen[kind]
        byte_kind = KINDS[kind].counts == "bytes" if kind in KINDS else bucket["bytes_before"] > 0
        bucket["counts_as"] = "intervention" if byte_kind else "event"
        by_kind[kind] = bucket
        if byte_kind:
            totals["interventions"] += bucket["count"]
            for key in ("bytes_before", "bytes_after", "bytes_avoided", "est_tokens_avoided"):
                totals[key] += bucket[key]
        else:
            totals["events"] += bucket["count"]
    totals["recovery_bytes"] = by_kind.get("recovery", {}).get("bytes_after", 0)
    totals["net_bytes_avoided"] = totals["bytes_before"] - totals["bytes_after"]
    totals["est_net_tokens_avoided"] = totals["net_bytes_avoided"] / BYTES_PER_TOKEN
    totals["duplicate_call_rows"] = duplicates["rows"]
    totals["duplicate_call_bytes"] = duplicates["bytes"]
    return {"by_kind": by_kind, "totals": totals, "note": NOTE}


def _kind(row):
    return row.get("kind") if isinstance(row.get("kind"), str) else "unknown"


def split_duplicates(rows):
    """``(counted, duplicates)``: a byte-intervention line whose
    ``tool_call_sha256`` and kind repeat an earlier line's is a duplicate
    (``summarise``); every other line is counted, in order."""
    counted, duplicates, calls = [], [], set()
    for row in rows:
        kind = _kind(row)
        call = row.get("tool_call_sha256")
        if isinstance(call, str) and call and _byte_row(kind, row):
            if (call, kind) in calls:
                duplicates.append(row)
                continue
            calls.add((call, kind))
        counted.append(row)
    return counted, duplicates


def _byte_row(kind, row):
    """A byte intervention: a known byte kind, or an unknown kind with bytes."""
    if kind in KINDS:
        return KINDS[kind].counts == "bytes"
    return _int(row.get("bytes_before")) > 0


def count(summary, kind):
    """How many lines of ``kind`` the summary saw (0 when none)."""
    bucket = summary["by_kind"].get(kind)
    return bucket["count"] if bucket else 0


def _int(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0
