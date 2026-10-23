#!/usr/bin/env bash
# Tests for hooks/dedupe-reads.py (PostToolUse on Read): repeat-read suppression, compaction and head trimming.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

# A 10 KiB Read is never trimmed; the first read is indexed silently.
run_hook dedupe-reads.py posttooluse.read.small.json
silent "first Read under 24 KiB: untouched"
[ -f "$SCRATCH/ork-meter/reads.json" ] && echo "  ok   reads.json created" || { echo "  FAIL reads.json missing"; fails=$((fails + 1)); }

# The same path with the same content: replaced by the first 15 lines plus a note.
run_hook dedupe-reads.py posttooluse.read.small.json
assert "repeat Read: replaced" 'o["hookSpecificOutput"]["updatedToolOutput"]["type"] == "text"'
assert "repeat Read: first 15 lines, numLines updated" 'o["hookSpecificOutput"]["updatedToolOutput"]["file"]["numLines"] == 15 and o["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"].count("\n") == 14'
assert "repeat Read: note names sha256 prefix and line count" '"Unchanged since" in o["hookSpecificOutput"]["additionalContext"].replace("is unchanged since", "Unchanged since") and "(sha256 " in o["hookSpecificOutput"]["additionalContext"] and "800 lines" in o["hookSpecificOutput"]["additionalContext"]'
assert "repeat Read: shape kept (filePath, startLine, totalLines)" 'sorted(o["hookSpecificOutput"]["updatedToolOutput"]["file"]) == ["content", "filePath", "numLines", "startLine", "totalLines"]'

# Changed content: shown in full.
run_hook dedupe-reads.py posttooluse.read.small-changed.json
silent "changed file: shown in full (no output)"
run_hook dedupe-reads.py posttooluse.read.small-changed.json
assert "changed file read again: deduped against the new hash" 'o["hookSpecificOutput"]["updatedToolOutput"]["file"]["numLines"] == 15'

# ORK_METER_DEDUPE=off leaves a repeat alone.
run_hook dedupe-reads.py posttooluse.read.small-changed.json ORK_METER_DEDUPE=off
silent "ORK_METER_DEDUPE=off: repeat untouched"

# Claude Code's own unchanged-read result is not ours to touch or count.
run_hook dedupe-reads.py posttooluse.read.native-unchanged.json
silent "native file_unchanged result: untouched"

# Index reset on compaction: the next identical read is shown in full again.
run_hook trim-session.py sessionstart.compact.json
silent "SessionStart(compact): resets silently"
[ ! -f "$SCRATCH/ork-meter/reads.json" ] && echo "  ok   reads.json removed on compaction" || { echo "  FAIL reads.json survived compaction"; fails=$((fails + 1)); }
run_hook dedupe-reads.py posttooluse.read.small-changed.json
silent "after compaction: same file shown in full"
# SessionStart(compact) is the one reset per compaction (#268); a PostCompact payload is not trim-session's event.
run_hook trim-session.py postcompact.json
silent "PostCompact: ignored silently"
[ -f "$SCRATCH/ork-meter/reads.json" ] && echo "  ok   reads.json kept on PostCompact (no second reset)" || { echo "  FAIL PostCompact reset reads.json again"; fails=$((fails + 1)); }

# Read compaction is off by default (#316): a 44 KiB Read keeps its head (whole lines, under 24 KiB) and
# explains the rest in additionalContext, with error-looking lines from the unshown range and their real
# line numbers.
run_hook dedupe-reads.py posttooluse.read.large.json
assert "large Read: head under budget" 'len(o["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"].encode()) <= 24 * 1024'
assert "large Read: numLines matches kept lines, truncatedByTokenCap set" 'o["hookSpecificOutput"]["updatedToolOutput"]["file"]["numLines"] == o["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"].count("\n") + 1 and o["hookSpecificOutput"]["updatedToolOutput"]["file"]["truncatedByTokenCap"] is True'
assert "large Read: head ends on a whole line" 'o["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"].endswith("to fill bytes")'
assert "large Read: context names the unshown range and offset" '"Lines 339-621 were not shown" in o["hookSpecificOutput"]["additionalContext"] and "offset=339" in o["hookSpecificOutput"]["additionalContext"]'
assert "large Read: error line from the unshown range with real line number" '"line 601: raise RuntimeError" in o["hookSpecificOutput"]["additionalContext"]'
run_hook dedupe-reads.py posttooluse.read.large.json
assert "large Read again: partial earlier result, so trimmed again rather than deduped" '"Ork Meter trimmed this Read" in o["hookSpecificOutput"]["additionalContext"]'

# Compaction opted in (ORK_METER_READ_COMPACT=on, which like every switch turned on takes effect from the
# next session): the same Read, about 11,600 tokens rendered, loses its long comment run instead. Lines
# 1-40 and 601-621 are shown exact at their own numbers; lines 41-600 are one marker and empty slots; one
# note, no trim.
run_hook dedupe-reads.py posttooluse.read.large.json ORK_METER_READ_COMPACT=on
assert "compaction on mid-session: waits for the next session, so the Read budget trims" '"Ork Meter trimmed this Read" in o["hookSpecificOutput"]["additionalContext"]'
reset_fixture_policy
run_hook dedupe-reads.py posttooluse.read.large.json ORK_METER_READ_COMPACT=on
assert "large Read compacted: original lines kept at their numbers, a marker and empty slots between" '
(lambda f, src: f["content"].split("\n")[:40] == src[:40] and f["content"].split("\n")[600:] == src[600:]
 and f["content"].split("\n")[40].startswith("\u2026 lines 41\u2013600 not shown (comment; Read offset=41 limit=560")
 and f["content"].split("\n")[41:600] == [""] * 559 and f["numLines"] == 621 and "truncatedByTokenCap" not in f)(
 o["hookSpecificOutput"]["updatedToolOutput"]["file"],
 json.load(open("tests/fixtures/posttooluse.read.large.json"))["tool_response"]["file"]["content"].split("\n"))'
assert "large Read compacted: one neutral note naming the saved original" '(
o["hookSpecificOutput"]["additionalContext"].startswith("Ork Meter compacted this read of /tmp/project/big.py: it is a PARTIAL view. 560 lines are elided")
and "\n" not in o["hookSpecificOutput"]["additionalContext"] and "trimmed" not in o["hookSpecificOutput"]["additionalContext"]
and "read guard" not in o["hookSpecificOutput"]["additionalContext"])'
assert "large Read compacted: the original is saved for recovery" '(
open(o["hookSpecificOutput"]["additionalContext"].rsplit("Original: ", 1)[1], encoding="utf-8", newline="").read()
== json.load(open("tests/fixtures/posttooluse.read.large.json"))["tool_response"]["file"]["content"])'
run_hook dedupe-reads.py posttooluse.read.large.json
assert "large Read compacted again: partial earlier result, so compacted again (same text) rather than deduped" '
o["hookSpecificOutput"]["additionalContext"].startswith("Ork Meter compacted this read")'
reset_fixture_policy   # a raised threshold, like any tunable, applies from the next session
run_hook dedupe-reads.py posttooluse.read.large.json ORK_METER_READ_COMPACT=on ORK_METER_READ_GUARD_TOKENS=20000
assert "under the token threshold: left to the Read budget, which trims as before" '(
"Ork Meter trimmed this Read" in o["hookSpecificOutput"]["additionalContext"]
and "compacted" not in o["hookSpecificOutput"]["additionalContext"])'
run_hook dedupe-reads.py posttooluse.read.large.json ORK_METER_READ_GUARD=off
assert "ORK_METER_READ_GUARD=off: no compaction, the Read budget trims" '"Ork Meter trimmed this Read" in o["hookSpecificOutput"]["additionalContext"]'

# Switches and robustness.
run_hook dedupe-reads.py posttooluse.read.large.json ORK_METER_TRIM=off
silent "ORK_METER_TRIM=off: no output"
run_raw dedupe-reads.py 'garbage'
silent "malformed stdin: no output, exit 0"

# Ledger: dedupe rows carry the path's sha256, never the path or content.
if python3 - "$SCRATCH/ork-meter/savings.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
kinds = [r["kind"] for r in rows]
assert kinds.count("dedupe") == 2 and kinds.count("trim") == 5 and kinds.count("read_compact") == 2, kinds
for r in rows:
    if r["kind"] in ("dedupe", "read_compact"):
        assert len(r["path_sha256"]) == 64
    if r["kind"] == "read_compact":
        assert r["lines_elided"] == 560 and r["bytes_before"] == 44107 and r["bytes_after"] < r["bytes_before"] // 4, r
        assert r["est_tokens_before"] == (44107 + 3) // 4 and r["est_tokens_after"] == (r["bytes_after"] + 3) // 4, r
    assert "path" not in r and "content" not in r
raw = open(sys.argv[1]).read()
assert "/tmp/project" not in raw and "def fn_" not in raw and "RuntimeError" not in raw
EOF
then echo "  ok   ledger: dedupe, trim and read_compact rows, hashed path only"; else echo "  FAIL ledger"; fails=$((fails + 1)); fi

# Claude Code's own saved output of a too-large tool result: the model Reads it because it needs that
# output, so it is neither compacted nor trimmed, and nothing is recorded (#329). A file of the same
# name under another session's directory is trimmed as before.
saved_read() { # <session directory name> -> the large fixture as a Read of <dir>/tool-results/b1.txt
  TRANSCRIPT="$TRANSCRIPT" python3 -c '
import json, os, sys
d = json.load(open("tests/fixtures/posttooluse.read.large.json"))
path = "/srv/u/.claude/projects/p/%s/tool-results/b1.txt" % sys.argv[1]
d["tool_input"]["file_path"] = path
d["tool_response"]["file"]["filePath"] = path
print(json.dumps(d).replace("__SCRATCH__", os.environ["SCRATCH"]).replace("__TRANSCRIPT__", os.environ["TRANSCRIPT"]))' "$1"
}
reset_fixture_policy
rows_before=$(wc -l < "$SCRATCH/ork-meter/savings.jsonl")
run_raw dedupe-reads.py "$(saved_read 00000000-0000-0000-0000-000000000000)"
silent "Claude Code's saved tool result for this session: untouched (compaction on)"
run_raw dedupe-reads.py "$(saved_read 00000000-0000-0000-0000-000000000000)" ORK_METER_READ_COMPACT=off
silent "Claude Code's saved tool result for this session: untouched (compaction off, Read budget)"
[ "$(wc -l < "$SCRATCH/ork-meter/savings.jsonl")" -eq "$rows_before" ] && echo "  ok   saved tool result: no ledger row" || { echo "  FAIL saved tool result recorded a saving"; fails=$((fails + 1)); }
run_raw dedupe-reads.py "$(saved_read 11111111-1111-1111-1111-111111111111)" ORK_METER_READ_COMPACT=off
assert "a tool-results file of another session: trimmed as before" '"Ork Meter trimmed this Read" in o["hookSpecificOutput"]["additionalContext"]'

# Demand: once the model goes back with a ranged Read past line 1 of a file Ork Meter showed partially (here
# the Read budget's head; compaction and the read guard's page count the same), later whole-file Reads of that
# file are delivered whole, also after a compaction of the conversation (#329). A ranged Read from line 1
# does not count, and other files are shortened as before.
as_read() { # <path> [offset limit]: the large fixture as a whole-file Read of <path>, or as a ranged Read
  TRANSCRIPT="$TRANSCRIPT" python3 -c '
import json, os, sys
d = json.load(open("tests/fixtures/posttooluse.read.large.json"))
path = sys.argv[1]
d["tool_input"] = {"file_path": path}
f = d["tool_response"]["file"]
f["filePath"] = path
if len(sys.argv) > 2:
    offset, limit = int(sys.argv[2]), int(sys.argv[3])
    d["tool_input"].update(offset=offset, limit=limit)
    lines = f["content"].split("\n")[offset - 1:offset - 1 + limit]
    f.update(content="\n".join(lines), startLine=offset, numLines=len(lines))
d["prompt_id"] = os.environ.get("PROMPT_ID") or d["prompt_id"]
print(json.dumps(d).replace("__SCRATCH__", os.environ["SCRATCH"]).replace("__TRANSCRIPT__", os.environ["TRANSCRIPT"]))' "$@"
}
wanted=/tmp/project/wanted.py; shown_only=/tmp/project/shown-only.py
reset_fixture_policy   # the default token threshold again (a test above raised it)
run_raw dedupe-reads.py "$(as_read $wanted)"
assert "demand: first whole Read shown partially" '"/tmp/project/wanted.py" in o["hookSpecificOutput"]["additionalContext"] and o["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"] != ""'
run_raw dedupe-reads.py "$(as_read $wanted 1 20)"
silent "demand: ranged Read from line 1 passes through"
run_raw dedupe-reads.py "$(as_read $wanted)"
assert "demand: from line 1 only, so a whole Read is still shortened" '"/tmp/project/wanted.py" in o["hookSpecificOutput"]["additionalContext"]'
run_raw dedupe-reads.py "$(as_read $wanted 100 50)"
silent "demand: ranged Read past line 1 passes through"
run_raw dedupe-reads.py "$(as_read $wanted)"
silent "demand: after that, a whole Read is delivered whole"
run_hook trim-session.py sessionstart.compact.json
run_raw dedupe-reads.py "$(PROMPT_ID=22222222-2222-2222-2222-222222222222 as_read $wanted)"
silent "demand: still delivered whole after the conversation is compacted, for a later prompt"
run_raw dedupe-reads.py "$(PROMPT_ID=33333333-3333-3333-3333-333333333333 as_read $wanted)" ORK_METER_READ_COMPACT=off
silent "demand: and with compaction switched off as well"
run_raw dedupe-reads.py "$(as_read $shown_only)"
assert "demand: another file is shortened as before" '"/tmp/project/shown-only.py" in o["hookSpecificOutput"]["additionalContext"]'

[ "$fails" -eq 0 ]
