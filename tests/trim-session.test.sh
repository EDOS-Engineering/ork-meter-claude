#!/usr/bin/env bash
# Tests for hooks/trim-session.py: status line, state reset, feature file (ork-meter #209), cache nudge (#59).
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

features="$XDG_STATE_HOME/ork-meter/plugin-features.json"
version=$(python3 -c 'import json; print(json.load(open(".claude-plugin/plugin.json"))["version"])')

run_hook trim-session.py sessionstart.startup.json
assert "startup: one systemMessage with budgets and no unavailable command" 'list(o) == ["systemMessage"] and o["systemMessage"].startswith("Ork Meter trim: on (Bash 12K, Read 24K, Grep 8K, Glob 4K, WebFetch 16K") and "/ork-meter:" not in o["systemMessage"]'
run_hook trim-session.py sessionstart.startup.json ORK_METER_TRIM=off ORK_METER_TRIM_BUDGET_BASH=32K
assert "startup with trim off: says off" 'o["systemMessage"] == "Ork Meter trim: off (ORK_METER_TRIM=off)."'
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json ORK_METER_TRIM_BUDGET_BASH=32K
assert "startup with a budget override: shows it" '"Bash 32K" in o["systemMessage"]'
assert "startup without a read-guard override: the default threshold" '"; read guard 96K)." in o["systemMessage"]'
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json ORK_METER_READ_GUARD_BYTES=262144 ORK_METER_TRIM_BUDGET_GREP=16kib ORK_METER_TRIM_BUDGET_GLOB=junk
assert "startup with overrides: the status line shows the thresholds in force" 'o["systemMessage"].startswith("Ork Meter trim: on (Bash 12K, Read 24K, Grep 16K, Glob 4K, WebFetch 16K; read guard 256K).")'
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json ORK_METER_READ_GUARD_BYTES=100
assert "startup with a read-guard value below the floor: the floor is shown" '"; read guard 4K)." in o["systemMessage"]'
run_hook trim-session.py sessionstart.startup.json ORK_METER_READ_GUARD=off
assert "startup with the read guard off: says so" '"; read guard off)." in o["systemMessage"]'
run_hook trim-session.py sessionstart.clear.json
silent "clear: silent"
run_hook trim-session.py sessionstart.compact.json
silent "compact: silent"
run_hook trim-session.py postcompact.json
silent "PostCompact: silent"

# One reset per compaction (#268): SessionStart(compact) fires after manual and
# automatic compaction (tests/native_lifecycle_qualification.py), so trim-session
# runs from that matcher only, never also from PostCompact, whose command text
# Claude Code appends to the /compact result.
if python3 - <<'EOF'
import json
hooks = json.load(open("hooks/hooks.json"))["hooks"]
runs = [(event, group.get("matcher")) for event, groups in hooks.items() for group in groups
        for hook in group["hooks"] if hook["command"].endswith("py.sh\" trim-session.py")]
assert runs == [("SessionStart", "startup|resume|clear|compact|fork")], runs
assert "PostCompact" not in hooks, sorted(hooks)
EOF
then echo "  ok   wired once: SessionStart startup|resume|clear|compact|fork, no PostCompact"; else echo "  FAIL trim-session wiring"; fails=$((fails + 1)); fi
mkdir -p "$SCRATCH/ork-meter"; echo '{}' > "$SCRATCH/ork-meter/reads.json"
run_hook trim-session.py postcompact.json
[ -e "$SCRATCH/ork-meter/reads.json" ] && echo "  ok   PostCompact payload: not handled, no second reset" || { echo "  FAIL PostCompact reset the store"; fails=$((fails + 1)); }

# Feature file: shape, sorted, permissions, opt-outs, malformed file replaced.
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json
if python3 - "$features" "$version" <<'EOF'
import json, sys, re
o = json.load(open(sys.argv[1]))
assert list(sorted(o)) == ["features", "plugin_version", "updated_at"], o
assert o["plugin_version"] == sys.argv[2]
assert o["features"] == sorted(o["features"]) == ["audit", "cache_nudge", "dedupe", "loop_guard", "read_guard", "test_filter", "trim"], o
assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", o["updated_at"])
EOF
then echo "  ok   feature file: exact shape, sorted features, RFC3339 UTC"; else echo "  FAIL feature file shape"; fails=$((fails + 1)); fi
fmode=$(file_mode "$features"); dmode=$(file_mode "$(dirname "$features")")
[ "$fmode" = "600" ] && [ "$dmode" = "700" ] && echo "  ok   feature file 600, directory 700" || { echo "  FAIL feature file modes $fmode/$dmode"; fails=$((fails + 1)); }
run_hook trim-session.py sessionstart.compact.json ORK_METER_TRIM=off ORK_METER_NUDGES=off ORK_METER_LOOP_GUARD=off
if python3 -c 'import json,sys; o=json.load(open(sys.argv[1])); assert o["features"] == ["audit"], o' "$features"; then
  echo "  ok   env opt-outs removed (trim family, nudges, loop guard)"; else echo "  FAIL opt-outs"; fails=$((fails + 1)); fi
run_hook trim-session.py sessionstart.compact.json ORK_METER_DEDUPE=off ORK_METER_READ_GUARD=off ORK_METER_TEST_FILTER=off
if python3 -c 'import json,sys; o=json.load(open(sys.argv[1])); assert o["features"] == ["audit", "cache_nudge", "loop_guard", "trim"], o' "$features"; then
  echo "  ok   granular opt-outs keep trim"; else echo "  FAIL granular opt-outs"; fails=$((fails + 1)); fi
printf 'not json {' > "$features"
run_hook trim-session.py sessionstart.clear.json
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$features" && echo "  ok   malformed feature file replaced" || { echo "  FAIL malformed feature file not replaced"; fails=$((fails + 1)); }
HOME="$tmp/home2" XDG_STATE_HOME= run_hook trim-session.py sessionstart.clear.json
[ -f "$tmp/home2/.local/state/ork-meter/plugin-features.json" ] && echo "  ok   without XDG_STATE_HOME: ~/.local/state/ork-meter" || { echo "  FAIL default state dir"; fails=$((fails + 1)); }

# Reset: a stale reads index and loop counters go away on every start.
mkdir -p "$SCRATCH/ork-meter"; echo '{}' > "$SCRATCH/ork-meter/reads.json"; echo '{}' > "$SCRATCH/ork-meter/failures.json"
run_hook trim-session.py sessionstart.resume.json
[ ! -e "$SCRATCH/ork-meter/reads.json" ] && [ ! -e "$SCRATCH/ork-meter/failures.json" ] && echo "  ok   resume: reads index and loop counters reset" || { echo "  FAIL reset"; fails=$((fails + 1)); }

# Cache nudge (#59): only when prompt_cache_likely_expired and context_tokens >= threshold.
run_hook trim-session.py sessionstart.resume-expired.json
assert "expired + large: nudge with context size, cost and both options" '"182,340 tokens" in o["systemMessage"] and "$1.14" in o["systemMessage"] and "/clear" in o["systemMessage"] and "pay the rewrite" in o["systemMessage"]'
assert "expired + large: status line still first" 'o["systemMessage"].startswith("Ork Meter trim: on")'
run_hook trim-session.py sessionstart.resume-fresh.json
assert "not expired: no nudge" '"cache" not in o["systemMessage"].lower() or "likely expired" not in o["systemMessage"]'
run_hook trim-session.py sessionstart.resume-expired-small.json
assert "expired but 12,000 tokens: no nudge" '"likely expired" not in o["systemMessage"]'
reset_fixture_policy
run_hook trim-session.py sessionstart.resume-expired-small.json ORK_METER_CACHE_NUDGE_TOKENS=10000
assert "threshold lowered to 10,000: nudge" '"12,000 tokens" in o["systemMessage"] and "$0.07" in o["systemMessage"]'
run_hook trim-session.py sessionstart.resume-expired.json ORK_METER_NUDGES=off
assert "ORK_METER_NUDGES=off: no nudge" '"likely expired" not in o["systemMessage"]'
run_hook trim-session.py sessionstart.resume.json
assert "resume without the cache fields: status only" 'o["systemMessage"].count("\n") == 0'

# Accepted: a clear within the window after a nudge logs cache_nudge_accepted; outside it, nothing.
reset_fixture_policy
run_hook trim-session.py sessionstart.resume-expired.json
run_hook trim-session.py sessionstart.clear.json
silent "clear after nudge: silent"
run_hook trim-session.py sessionstart.resume-expired.json
python3 -c '
import json, sys, time
p = sys.argv[1]; o = json.load(open(p)); o["ts"] = time.time() - 3600; json.dump(o, open(p, "w"))' "$XDG_STATE_HOME/ork-meter/cache-nudges/00000000-0000-0000-0000-000000000000.json"
run_hook trim-session.py sessionstart.clear.json
if python3 - "$SCRATCH/ork-meter/savings.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
nudges = [r for r in rows if r["kind"] == "cache_nudge"]
accepted = [r for r in rows if r["kind"] == "cache_nudge_accepted"]
assert len(nudges) >= 3, nudges
assert all(r["context_tokens"] in (182340, 12000) for r in nudges)
assert len(accepted) == 1, accepted
assert accepted[0]["est_cache_write_usd_avoided"] == 1.1396 and accepted[0]["context_tokens"] == 182340
for r in rows:
    assert r["bytes_before"] == 0 and r["est_tokens_avoided"] == 0
EOF
then echo "  ok   ledger: cache_nudge rows; one cache_nudge_accepted (within window only)"; else echo "  FAIL nudge ledger"; fails=$((fails + 1)); fi

run_raw trim-session.py 'nope'
silent "malformed stdin: silent, exit 0"

[ "$fails" -eq 0 ]
