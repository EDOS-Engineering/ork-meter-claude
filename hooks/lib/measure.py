"""Measured token counts from a Claude Code session transcript.

The transcript at ``transcript_path`` is JSON Lines. Each assistant turn is
one or more lines of the form::

    {"type": "assistant", "requestId": "req_…", "uuid": "…",
     "message": {"id": "msg_…", "role": "assistant", "usage": {
         "input_tokens": 2, "cache_creation_input_tokens": 19701,
         "cache_read_input_tokens": 23849, "output_tokens": 212, …}, …}}

Claude Code writes one line per content block, so several lines share one
``message.id`` and repeat the same ``usage``. The sums below count each
message id once, reconciling growing or delayed usage snapshots. This shape
was checked against a real transcript written
by Claude Code 2.1.296 on 2026-10-13; if a transcript doesn't match it, the
result says ``unavailable`` rather than guessing.
"""

import datetime
import json
import time
from usage import reconcile

USAGE_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)
NOTE = ("measured from transcript: monotone message.usage snapshots summed over distinct assistant "
        "message ids; prompt tokens = input + cache read + cache creation; supplied transcript only")


def unavailable(reason):
    return {"status": "unavailable", "reason": reason, "source": "transcript"}


def measure_transcript(path, deadline=None):
    """Sum assistant usage in the transcript at ``path``.

    ``deadline`` is a ``time.monotonic()`` value; past it the scan stops and
    the result says ``partial``. Never raises.
    """
    if not isinstance(path, str) or not path:
        return unavailable("no transcript_path")
    totals = dict((k, 0) for k in USAGE_KEYS)
    messages = 0
    assistant_lines = 0
    without_usage = 0
    seen = {}
    unresolved = set()
    partial = False
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if deadline is not None and time.monotonic() > deadline:
                    partial = True
                    break
                if b'"assistant"' not in raw:
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    partial = True
                    continue
                if not isinstance(row, dict) or row.get("type") != "assistant":
                    continue
                assistant_lines += 1
                message = row.get("message")
                mid = (message.get("id") if isinstance(message, dict) else None) or row.get("requestId") or row.get("uuid")
                if not isinstance(mid, str) or not mid.strip():
                    return unavailable("assistant message has no stable identity")
                usage = message.get("usage") if isinstance(message, dict) else None
                if not isinstance(usage, dict):
                    without_usage += 1
                    mid = ((message.get("id") if isinstance(message, dict) else None)
                           or row.get("requestId") or row.get("uuid"))
                    if isinstance(mid, str) and mid.strip():
                        unresolved.add(mid)
                    else:
                        partial = True
                    continue
                if any(type(usage.get(k)) is not int or usage[k] < 0 for k in USAGE_KEYS):
                    return unavailable("message.usage counters missing or invalid; transcript shape not recognised")
                mid = message.get("id") or row.get("requestId") or row.get("uuid")
                if not isinstance(mid, str) or not mid.strip():
                    return unavailable("assistant message identity missing or invalid; transcript shape not recognised")
                previous = seen.get(mid)
                try:
                    current = reconcile(previous, {k: usage[k] for k in USAGE_KEYS})
                except ValueError:
                    return unavailable("conflicting message.usage snapshots")
                seen[mid] = current
                if previous is None:
                    messages += 1
                for k in USAGE_KEYS:
                    totals[k] += current[k] - (previous[k] if previous else 0)
    except OSError as exc:
        return unavailable("transcript not readable: %s" % exc.__class__.__name__)
    if assistant_lines == 0:
        return unavailable("no assistant lines found in transcript")
    if messages == 0:
        return unavailable("assistant lines carry no message.usage object; transcript shape not recognised")
    partial = partial or bool(unresolved - seen.keys())
    result = {"status": "partial" if partial else "ok", "source": "transcript", "note": NOTE,
              "assistant_messages": messages, "assistant_lines_without_usage": without_usage}
    result.update(totals)
    result["prompt_tokens"] = (totals["input_tokens"] + totals["cache_read_input_tokens"]
                               + totals["cache_creation_input_tokens"])
    return result


TAIL_BYTES = 512 * 1024


def tail_lines(path):
    """The lines in the last ``TAIL_BYTES`` of the transcript at ``path``
    (a first line that may be cut is dropped), or None when unreadable."""
    if not isinstance(path, str) or not path:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read(TAIL_BYTES)
    except OSError:
        return None
    lines = tail.split(b"\n")
    if len(tail) == TAIL_BYTES and size > TAIL_BYTES:
        lines = lines[1:]  # the first line may be cut
    return lines


def context_tokens(path, deadline=None, lines=None):
    """The conversation's current context size, from the last assistant
    message's usage in the transcript tail: input + cache read + cache
    creation (the prompt it was given) + output (now part of the context).

    Reads at most the last ``TAIL_BYTES`` of the file, or uses ``lines``
    from ``tail_lines``. Returns None when the
    transcript is unreadable, has no recognisable usage in that tail, or the
    deadline passes. Never raises and never reads message content."""
    lines = tail_lines(path) if lines is None else lines
    if lines is None:
        return None
    for raw in reversed(lines):
        if deadline is not None and time.monotonic() > deadline:
            return None
        if b'"assistant"' not in raw or b'"usage"' not in raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        message = row.get("message") if isinstance(row, dict) and row.get("type") == "assistant" else None
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        if any(type(usage.get(k)) is not int or usage[k] < 0 for k in USAGE_KEYS):
            return None
        return sum(usage[k] for k in USAGE_KEYS)
    return None


def last_turn(path, deadline=None, lines=None):
    """The last main-conversation assistant turn in the transcript tail:
    ``{"tokens", "timestamp", "ttl_seconds"}``.

    ``tokens`` is its context size, as ``context_tokens``. ``timestamp`` is its
    time in epoch seconds. ``ttl_seconds`` is the prompt-cache lifetime the
    conversation was last written with: 3600 when the newest cache write in
    the tail used the 1-hour TTL, 300 when it used the 5-minute one, and 3600
    when the tail has no split (the longer lifetime holds fewer prompts).
    Sidechain (subagent) turns and Claude Code's ``<synthetic>`` rows are
    skipped. A compaction after the last turn gives None: the context it
    described is gone. Reads at most ``TAIL_BYTES`` (or uses ``lines``, as
    ``context_tokens``); returns None when nothing usable is found or the
    deadline passes. Never raises and never reads message content."""
    lines = tail_lines(path) if lines is None else lines
    if lines is None:
        return None
    turn = None
    for raw in reversed(lines):
        if deadline is not None and time.monotonic() > deadline:
            return None
        if b'compact_boundary' in raw or b'isCompactSummary' in raw:
            try:
                row = json.loads(raw)
            except ValueError:
                row = None
            if isinstance(row, dict) and (row.get("subtype") == "compact_boundary" or row.get("isCompactSummary") is True):
                # The conversation was compacted after this point: turns before
                # it no longer describe the context (None), and a split before it
                # no longer describes the cache (keep the default lifetime).
                return turn
        if b'"assistant"' not in raw or b'"usage"' not in raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "assistant" or row.get("isSidechain"):
            continue
        message = row.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict) or message.get("model") == "<synthetic>":
            continue   # Claude Code's own error/notice rows carry zero usage
        if turn is None:
            if any(type(usage.get(k)) is not int or usage[k] < 0 for k in USAGE_KEYS):
                return None
            stamp = row.get("timestamp")
            try:
                when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except (AttributeError, TypeError, ValueError):
                return None
            turn = {"tokens": sum(usage[k] for k in USAGE_KEYS), "timestamp": when, "ttl_seconds": 3600}
        split = usage.get("cache_creation")
        if isinstance(split, dict):
            hour, five = split.get("ephemeral_1h_input_tokens"), split.get("ephemeral_5m_input_tokens")
            if type(hour) is int and hour > 0:
                return dict(turn, ttl_seconds=3600)
            if type(five) is int and five > 0:
                return dict(turn, ttl_seconds=300)
    return turn


def stale_idle(turn, tokens):
    """Seconds since ``turn`` (``last_turn``) when its context is at least
    ``tokens`` and its prompt cache has expired, else None: the condition
    under which the stale-context guard holds a prompt."""
    if turn is None or turn["tokens"] < tokens:
        return None
    idle = time.time() - turn["timestamp"]
    return idle if idle > turn["ttl_seconds"] else None
