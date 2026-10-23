"""PostToolUse hook for Read: repeat-read suppression, then compaction or size trimming.

One process handles both for a Read result because Claude Code runs the
hooks of one event in parallel on the original output and keeps the last
``updatedToolOutput`` it receives; two separate rewriting hooks would race.

Repeat reads: the session store's reads index (``reads.json``,
hooks/lib/session.py) maps the path of each whole-file Read to the
SHA-256 of what Claude saw and the ``prompt_id`` it was read for. When the
same range comes back with the same hash for the same prompt, the result is
replaced by its first 15 lines and a note that it is unchanged since the
earlier read, with the exact Read (offset and limit) that returns the rest
if that earlier result is gone. No hook fires on /rewind, so an earlier
prompt's result is never assumed to be in context (#129). Index updates are
locked against parallel Read hooks (#131). Claude Code
itself already answers an unchanged re-read (same mtime, content still in
context) with a short "file unchanged" result; that case arrives here as
``type: "file_unchanged"`` and is left alone and not counted.

Changed re-reads (opt-in ``delta`` lever, ``ORK_METER_DELTA=on``): when the
same whole-file Read was delivered in full earlier this conversation and the
file has changed, the Read result becomes a unified diff against that
earlier content (hooks/lib/delta.py), if that is smaller. The diff is file
text, so it stays in the tool result; ``additionalContext`` carries only
Ork Meter's own fixed note, never file content (#143).

Read compaction (the ``read_guard`` lever's ``compact`` switch,
``ORK_METER_READ_COMPACT``, opt-in since #316; hooks/lib/read_compact.py): a
whole-file Read result over ``ORK_METER_READ_GUARD_TOKENS`` (6,000 by
default), counted as Claude Code renders it, has whole lines elided, every
shown line exact at its own number. When that fits under the threshold the
result is the compacted text with one note saying it is a partial view
and listing the elided ranges, and the Write guard (hooks/write-guard.py)
gets a record of them; a ranged Read of the file is recorded for it too,
and a whole delivery drops it. When it doesn't fit, the
result is paged as the read guard pages (read-guard.py lets such a Read
through whole, ``deferred``), with the guard's note and the compacted size,
or, for a file the guard would have left whole, trimmed as below. Under
the threshold it is left to the Read budget, as before. The original is
saved for recovery like a trimmed Read's, and the ledger gets a
``read_compact`` (or ``read_guard``) row with a path hash only.

Order and ownership, per Read result: explicit ranges and recovery reads
pass through; an unchanged repeat is stubbed (dedupe); a changed re-read may
be a delta (opt-in); then compaction owns every whole-file result over its
token threshold while it is on; the Read budget below applies only to what
compaction doesn't own (under the threshold, results Claude Code itself cut
short, compaction off) or gives back (no fit and no guard page, or a reply
not admitted). One reply per result, so the levers never act twice on one
Read, and a compacted result is never trimmed after.

Size: an implicit whole-file text result over the Read budget keeps only its head, whole lines,
so the line numbers Claude Code prints stay right; the elided range and any
error-looking lines in it (with real line numbers) go into
``additionalContext``. An immutable original snapshot is kept for recovery,
written only once the reply is admitted (``artifacts.admit``).
Explicit ranges, allow_large, and recovery artifact reads pass through.

Runs through hooks/lib/hook.py: nothing is printed on any error or when
``ORK_METER_TRIM=off``.
"""

import os
import time

import artifacts
import circuit
import delta
import features
import hook
import ledger
import read_compact
import read_tool
import session
import token_gate
import trimmer
import write_guard

EVENTS = ("PostToolUse",)
# Seconds of the hook's budget kept back from compaction for the snapshot
# write and the reply: a search past this ends without a compaction.
COMPACT_RESERVE = 0.3
# A result larger than this is not compacted: Claude Code delivers a
# whole-file Read this large only with its own cap raised, compaction costs
# about 0.7 ms per KiB on a laptop (several times that on a slow Windows
# runner) and so much text can't come down to the threshold. The Read
# budget trims it.
COMPACT_MAX_BYTES = 256 * 1024
# Tokens a compaction must save beyond reaching the threshold: the offline
# token gate admits a replacement only when it saves its 512-token reserve
# plus the note, so a smaller saving would be refused anyway.
COMPACT_MARGIN = token_gate.RESERVE + 300
FEATURE = "trim"
REDUCER = "dedupe-reads"
MIN_DEDUPE_BYTES = 1024    # smaller results aren't worth replacing


def same_prompt(previous, payload):
    """The earlier Read happened while Claude worked on this same prompt.

    No hook fires on /rewind or an edited earlier message, so an earlier
    result from another prompt may no longer be in context (#129). Assumption
    (Claude Code hooks reference): ``prompt_id`` names the user prompt a tool
    call serves and is shared by every tool call made for it; a new or
    rewound prompt gets a new one. Compaction within a prompt resets the
    index (trim-session.py). A missing ``prompt_id`` never matches."""
    current = payload.get("prompt_id")
    return isinstance(current, str) and bool(current) and previous.get("prompt_id") == current


def whole_file(file_info, start_line):
    """Claude Code returned the whole file, not cut at its own line or token cap (#144)."""
    lines = file_info.get("numLines")
    return (start_line == 1 and not file_info.get("truncatedByTokenCap")
            and isinstance(lines, int) and lines == file_info.get("totalLines"))


def read_delta(ctx, store, name, previous, response, file_info, content, digest, file_path, size):
    """A changed whole-file re-read as a unified diff against the full earlier
    Read result still in context, or None.

    Only when that earlier result was delivered whole (not trimmed, cut short
    or itself a delta) and its stored base still has the hash the reads index
    recorded for it."""
    if (not isinstance(previous, dict) or previous.get("partial") or previous.get("sha256") == digest
            or size < delta.MIN_BYTES):
        return None
    base, meta = delta.load(store, name)
    if base is None or meta.get("sha256") != previous.get("sha256"):
        return None
    rendered = delta.render(base, content, "%s (earlier Read, sha256 %s)" % (file_path, meta["sha256"][:8]),
                            "%s (now, sha256 %s)" % (file_path, digest[:8]))
    if rendered is None or not rendered[0]:
        return None
    diff, info = rendered
    updated = dict(response)
    updated["file"] = dict(file_info)
    updated["file"]["content"] = diff
    updated["file"]["numLines"] = diff.count("\n") + 1
    context = (
        "Ork Meter delta: %s changed since its earlier full Read in this conversation (%d lines, %d bytes now). "
        "The Read result above is not the file: it is a unified diff against that earlier Read result "
        "(%d hunks, +%d/-%d lines). Its left-margin numbers are not file line numbers; the @@ headers are. "
        "The complete current file is the earlier Read result with this diff applied. "
        "Read with offset and limit for exact current lines, or set ORK_METER_DELTA=off."
        % (file_path, content.count("\n") + 1, size, info["hunks"], info["added"], info["removed"])
    )
    after = trimmer.nbytes(diff) + trimmer.nbytes(context)
    if after >= size:
        return None
    ctx.record(ledger.delta_delivered("Read", size, after, info["hunks"]))
    return hook.updated_tool_output(ctx.event, updated, context)


def handle(ctx):
    payload, env = ctx.payload, ctx.env
    if payload.get("tool_name") != "Read":
        return None
    response = payload.get("tool_response")
    if not isinstance(response, dict) or response.get("type") != "text":
        return None
    file_info = response.get("file")
    if not isinstance(file_info, dict):
        return None
    content = file_info.get("content")
    file_path = file_info.get("filePath")
    if not isinstance(content, str) or not isinstance(file_path, str) or not content:
        return None
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    # Exact ranges and recovery files are intentional evidence retrieval.
    # Do not shorten them again or claim an earlier partial read was complete.
    if (read_tool.explicit_range(tool_input) or session.is_recovery_path(file_path)
            or session.is_host_tool_result(file_path, payload.get("session_id"))):
        if read_tool.explicit_range(tool_input):
            note_demand(ctx, file_path, tool_input)
        return None   # a ranged Read marks lines read for the Write guard in write-guard.py
    start_line = file_info.get("startLine") if isinstance(file_info.get("startLine"), int) else 1
    size = trimmer.nbytes(content)
    digest = ledger.sha256_hex(content)

    store = ctx.store()
    agent = payload.get("agent_id")
    # Only whole-file Reads get here, so the path is the key (#190).
    k = ledger.sha256_hex(agent) + "\x00" + file_path if isinstance(agent, str) and agent else file_path
    previous = store.reads().get(k)
    complete = whole_file(file_info, start_line)
    # "partial": the model does not hold the whole file from this result, so a
    # repeat must be delivered in full rather than stubbed or diffed.
    entry = {"sha256": digest, "prompt_id": payload.get("prompt_id"), "ts": int(time.time()),
             "lines": content.count("\n") + 1, "bytes": size, "partial": not complete}

    if (features.enabled(env, "dedupe") and isinstance(previous, dict) and previous.get("sha256") == digest
            and not previous.get("partial") and size > MIN_DEDUPE_BYTES and same_prompt(previous, payload)):
        lines = content.split("\n")
        shown = lines[:trimmer.FIRST_LINES]
        head = "\n".join(shown)
        updated = dict(response)
        updated["file"] = dict(file_info)
        updated["file"]["content"] = head
        updated["file"]["numLines"] = len(shown)
        context = (
            "Ork Meter: %s is unchanged since your earlier Read of it while working on this same prompt "
            "(sha256 %s, %d lines, %d bytes). The Read result above shows only lines 1-%d; that earlier Read "
            "result holds the full content. If it is not in your context, Read %s with offset=%d and limit=%d "
            "for the rest (a Read with offset and limit is never shortened). Set ORK_METER_DEDUPE=off to turn "
            "this off."
            % (file_path, digest[:8], entry["lines"], size, len(shown), file_path, len(shown) + 1,
               max(1, entry["lines"] - len(shown)))
        )
        if trimmer.nbytes(head) + trimmer.nbytes(context) >= size:
            return None
        store.update_reads(k, entry)
        ctx.record(ledger.deduped(size, trimmer.nbytes(head) + trimmer.nbytes(context),
                                  ledger.sha256_hex(file_path)))
        return hook.updated_tool_output(ctx.event, updated, context)

    name = delta.key("Read", agent, file_path) if features.enabled(env, "delta") else None
    if name is not None and complete:
        reply = read_delta(ctx, store, name, previous, response, file_info, content, digest, file_path, size)
        if reply is not None:
            # The model now holds a diff, not the file: the next re-read is
            # delivered in full, never stubbed or chained onto this delta (#144).
            delta.forget(store, name)
            entry["partial"] = True
            store.update_reads(k, entry)
            return reply

    if complete and store.wants_whole(session.path_key(file_path)):
        # Earlier this session the model Read back lines a partial view of this
        # file left out. Compacting or trimming it again only makes it do that
        # again, and in a long session the re-reads cost more than the elision
        # saves (#329). Dedupe and delta above still apply.
        return deliver_whole(store, k, entry, name, complete, content, file_path, env, write_guard.view_key(payload))

    note = ""
    if complete and features.read_compaction(env):
        # Read compaction owns a whole-file Read over its token threshold:
        # compacted, or paged when that doesn't fit. Everything else, and what
        # it gives back, is the Read budget's below.
        reply = compaction(ctx, store, k, entry, name, response, file_info, content, file_path, size)
        if isinstance(reply, dict):
            return reply
        note = reply

    budget = features.trim_budget(env, "Read")
    head, info = trimmer.trim_head(content, budget, start_line)
    if info is not None:
        # Named now, written only for an admitted reply (#130, #190).
        pending = artifacts.prepare(store, "Read", response, content)
        updated = dict(response)
        updated["file"] = dict(file_info)
        updated["file"]["content"] = head
        updated["file"]["numLines"] = info["lines_kept"]
        updated["file"]["truncatedByTokenCap"] = True
        context = (
            "Ork Meter trimmed this Read of %s: it shows lines %d-%d (%d bytes of %d). Lines %d-%d were not shown; "
            "Read the file again with offset=%d (and a limit) to see them, or set ORK_METER_TRIM=off."
            % (file_path, start_line, start_line + info["lines_kept"] - 1, info["bytes_after"], info["bytes_before"],
               info["first_elided_line"], info["last_elided_line"], info["first_elided_line"])
        )
        if info["partial_line"]:
            context = (
                "Ork Meter trimmed this Read of %s: line %d alone exceeds the byte budget. "
                "Only its first %d bytes are shown; the rest of that line and later lines were not shown. "
                "Use a targeted Bash command to inspect that line, or set ORK_METER_TRIM=off for a full Read."
                % (file_path, start_line, info["bytes_after"])
            )
        if info["error_lines"]:
            context += "\nError-looking lines in the range not shown:\n" + "\n".join(info["error_lines"])
        if note:
            context += "\n" + note
        context += "\nFull original Read snapshot: %s" % pending.path
        if trimmer.nbytes(head) + trimmer.nbytes(context) < size:
            reply = hook.updated_tool_output(ctx.event, updated, context)
            # State first, admission last: the index update can wait on its
            # lock, and nothing slow may run between writing the snapshot and
            # returning the reply, or a late reply would leave it uncited.
            if name is not None:
                delta.forget(store, name)   # the model saw only the head (#138)
            store.update_reads(k, dict(entry, partial=True))
            mark_partial(store, file_path)
            if artifacts.admit(ctx, reply, pending):
                ctx.record(ledger.trimmed("Read", info["bytes_before"], info["bytes_after"] + trimmer.nbytes(context)))
                return reply
            # Not admitted: the whole file reaches the model, and the
            # delivered-whole bookkeeping below puts the index and base back.

    return deliver_whole(store, k, entry, name, complete, content, file_path, env, write_guard.view_key(payload))


def deliver_whole(store, k, entry, name, complete, content, file_path=None, env=None, view=None):
    """The result reaches the model unchanged: record it, keep it as the
    delta base when it is a whole file, and drop the Write guard's record."""
    if complete and file_path is not None and env is not None and write_guard.exists(env):
        try:
            write_guard.forget(env, file_path, view)
        except (write_guard.Corrupt, write_guard.Busy, OSError, ValueError):
            pass
    if name is not None:
        # Delivered unchanged: this is now the text the model holds, and only a
        # whole file can be a base.
        if complete:
            delta.save(store, name, content)
        else:
            delta.forget(store, name)
    store.update_reads(k, entry)
    return None


def compaction(ctx, store, k, entry, name, response, file_info, content, file_path, size):
    """Read compaction of a whole-file Read result (hooks/lib/read_compact.py).

    Returns a reply (compacted, or paged as the read guard pages when the
    compacted text still doesn't fit), or else a note for the ordinary
    trim's reply, empty unless a compaction was tried: the result is under
    the token threshold or over ``COMPACT_MAX_BYTES``, the search ran out of time,
    the read guard wouldn't page this file, or the reply wasn't admitted."""
    env = ctx.env
    threshold = features.value(env, "read_guard", "tokens")
    if size > COMPACT_MAX_BYTES or not read_compact.compactable(file_path) or not token_gate.bounded(content):
        return ""
    try:
        if circuit.disabled(circuit.state(store), "write-guard:Write"):
            return ""   # no compacted view without its Write guard
    except (OSError, ValueError, TypeError, KeyError):
        return ""
    count, _exact = token_gate.counter()
    lines, info = read_compact.compact(content, file_path, threshold, count, margin=COMPACT_MARGIN,
                                       deadline=ctx.deadline - COMPACT_RESERVE, now=time.monotonic)
    if info["tokens_before"] <= threshold or info["late"]:
        return ""
    if lines is not None:
        compacted = "\n".join(lines)
        updated = dict(response)
        updated["file"] = dict(file_info)
        updated["file"]["content"] = compacted
        updated["file"]["numLines"] = len(lines)
        pending = artifacts.prepare(store, "Read", response, content)
        context = (
            "Ork Meter compacted this read of %s: it is a PARTIAL view. %d lines are elided, about %d tokens down "
            "to %d: %s. Each elided range is one line '\u2026 lines A\u2013B not shown' followed by empty numbered lines "
            "that are NOT empty in the file; every other line is exact at its own number. Before editing an elided "
            "range, Read it with offset and limit (a plain Read of the whole file again returns nothing new). Never "
            "rewrite this file with Write from this view (Ork Meter refuses such a Write); use Edit, and note that "
            "Edit with replace_all also changes elided lines. Original: %s"
            % (file_path, info["lines_elided"], info["tokens_before"], info["tokens_after"],
               read_compact.describe(info["elided"]), pending.path))
        reply = hook.updated_tool_output(ctx.event, updated, context)
        return admitted(ctx, store, k, entry, name, reply, pending,
                        ledger.read_compacted(size, trimmer.nbytes(compacted) + trimmer.nbytes(context),
                                              ledger.sha256_hex(file_path), info["lines_elided"]),
                        file_path, info["elided"])
    over = ("compacted, it is still about %d tokens, over the %d-token threshold"
            % (info["tokens_after"], threshold))
    page = guard_page(ctx, content, file_path)
    if page is None:
        return "Ork Meter compacted this Read first: %s." % over
    page_lines, outline = page
    shown = content.split("\n")[:page_lines]
    head = "\n".join(shown)
    updated = dict(response)
    updated["file"] = dict(file_info)
    updated["file"]["content"] = head
    updated["file"]["numLines"] = page_lines
    updated["file"]["truncatedByTokenCap"] = True
    pending = artifacts.prepare(store, "Read", response, content)
    context = (
        "Ork Meter read guard: %s is %d KiB and %d lines, about %d tokens; %s. This Read shows only its first "
        "%d lines. The rest is available with further Read calls that set offset and limit, for example "
        "offset=%d; Ork Meter refuses a Write of this file from this page until the rest is read. Set "
        "ORK_METER_TRIM=off to turn the guard off.\nFull original Read snapshot: %s"
        % (file_path, (size + 1023) // 1024, content.count("\n") + 1, info["tokens_before"], over, page_lines,
           page_lines + 1, pending.path))
    if outline:
        context += "\n" + outline
    reply = hook.updated_tool_output(ctx.event, updated, context)
    total = content.count("\n") + 1
    return admitted(ctx, store, k, entry, name, reply, pending,
                    ledger.read_guarded(size, trimmer.nbytes(head) + trimmer.nbytes(context),
                                        ledger.sha256_hex(file_path)),
                    file_path, [(page_lines + 1, total, "lines after the page")])


def guard_page(ctx, content, file_path):
    """``(lines, outline)``: the page the read guard (read-guard.py) would
    have given this file before it ran, the Python outline's when it is
    smaller, or None when the guard would have left it whole (under its byte
    threshold and no outline, or no more lines than its page)."""
    env = ctx.env
    limit = features.value(env, "read_guard", "lines")
    lines = content.split("\n")
    if len(lines) <= limit:
        return None
    raw = content.encode("utf-8")
    first_bytes = len("\n".join(lines[:limit]).encode("utf-8")) + 1
    if (os.path.splitext(file_path)[1].lower() == ".py"
            and read_tool.OUTLINE_MIN_FILE < len(raw) <= read_tool.OUTLINE_MAX_FILE):
        candidate = read_tool.outline_page(raw, first_bytes, ctx.remaining())
        if candidate is not None:
            return read_tool.OUTLINE_LIMIT, candidate[1]
    if len(raw) <= features.value(env, "read_guard", "bytes"):
        return None
    return limit, None


def admitted(ctx, store, k, entry, name, reply, pending, row, file_path=None, elided=()):
    """``reply`` once its snapshot is written and it may be emitted, with the
    reads index marking the result partial and the delta base dropped;
    otherwise an empty note, so the ordinary trim may act instead. As for the
    trim: state first, admission last, so nothing slow runs between writing
    the snapshot and returning the reply."""
    if name is not None:
        delta.forget(store, name)
    store.update_reads(k, dict(entry, partial=True))
    if elided:
        # The Write guard's record (hooks/lib/write_guard.py), written before
        # the reply: a view that leaves lines out is never delivered without it.
        try:
            write_guard.remember(ctx.env, file_path, ctx.payload["tool_response"]["file"]["content"], elided,
                                 write_guard.view_key(ctx.payload))
        except (write_guard.Corrupt, write_guard.Busy, OSError, ValueError, TypeError, KeyError):
            return ""
    mark_partial(store, file_path)   # state first: only lets a later ranged Read count as demand
    if artifacts.admit(ctx, reply, pending):
        ctx.record(row)
        return reply
    if elided:
        try:
            write_guard.forget(ctx.env, file_path, write_guard.view_key(ctx.payload))
        except (write_guard.Corrupt, write_guard.Busy, OSError, ValueError):
            pass
    return ""


def mark_partial(store, file_path):
    """Remember that this file was shown partially, for ``note_demand``."""
    try:
        store.mark_partial_view(session.path_key(file_path))
    except (OSError, ValueError, TypeError):
        pass


def note_demand(ctx, file_path, tool_input):
    """A ranged Read past line 1 of a file Ork Meter showed partially earlier
    this session (compacted, trimmed to its head, or paged by the read guard):
    the model went back for lines it was not shown, so later whole-file Reads
    of that file are delivered whole (#329). Only an existing store is read or
    written; any failure leaves things as they were."""
    offset = tool_input.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset <= 1:
        return
    try:
        store = session.Store.existing_for_payload(ctx.payload, ctx.env)
        if store is None:
            return
        key = session.path_key(file_path)
        if store.had_partial_view(key):
            store.mark_wants_whole(key)
    except (OSError, ValueError, TypeError):
        pass
