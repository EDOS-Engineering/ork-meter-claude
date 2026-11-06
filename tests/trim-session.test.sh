#!/usr/bin/env bash
# Tests for hooks/trim-session.py: status line, state reset, feature file (ork-meter #209), cache nudge (#59).
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

features="$XDG_STATE_HOME/ork-meter/plugin-features.json"
version=$(python3 -c 'import json; print(json.load(open(".claude-plugin/plugin.json"))["version"])')
ledger="$SCRATCH/ork-meter/savings.jsonl"

# The hook still builds its systemMessage (status line, nudge), but hook.run strips every
# systemMessage at the emission boundary (hooks/lib/native_text.py clean_reply keeps only
# hookSpecificOutput/continue/suppressOutput/decision), so a SessionStart is silent on stdout.
# What the status line used to show is read back from the session pin the hook wrote, as the
# hook itself computes it (features.status_line over the pinned policy); what the nudge used
# to say is the cache_nudge ledger row. [VAR=value ...] are the hook process's own settings.
pinned_status() { # [VAR=value ...]: the status line for the pinned session policy
  env "$@" python3 -I -c 'import os, sys
sys.path.insert(0, "hooks/lib")
import features, policy, session
store = session.Store.for_scratchpad(os.environ["SCRATCH"])
gated = features.gate(dict(os.environ))
print(features.status_line(features.carried(policy.for_store(store, gated), gated)))'
}
status_is() { # <name> <python expression over `s`> [VAR=value ...]
  local name=$1 expr=$2 s
  shift 2
  s=$(pinned_status "$@")
  if S="$s" python3 -c 'import os, sys; s = os.environ["S"]; sys.exit(0 if eval(sys.argv[1]) else 1)' "$expr"; then
    echo "  ok   $name"
  else
    echo "  FAIL $name: $expr"; echo "       pinned: $s"; fails=$((fails + 1)); return 1
  fi
}
nudges() { # the number of cache_nudge ledger rows so far
  python3 -c 'import json, os, sys
try: rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
except OSError: rows = []
print(sum(1 for r in rows if r["kind"] == "cache_nudge"))' "$ledger"
}
last_nudge() { # <name> <python expression over `r` (the last cache_nudge row)>
  assert "$1" 'out == "" and (lambda r: '"$2"')([json.loads(l) for l in open("'"$ledger"'") if l.strip() and json.loads(l)["kind"] == "cache_nudge"][-1])'
}

run_hook trim-session.py sessionstart.startup.json
silent "startup: silent (the status line is stripped at the emission boundary)"
status_is "startup: the pin carries the default budgets and no unavailable command" 's.startswith("Ork Meter trim: on (Bash 12K, Read 24K, Grep 8K, Glob 4K, WebFetch 16K") and "/ork-meter:" not in s'
run_hook trim-session.py sessionstart.startup.json ORK_METER_TRIM=off ORK_METER_TRIM_BUDGET_BASH=32K
silent "startup with trim off: silent"
status_is "startup with trim off: the opt-out is in force" 's == "Ork Meter trim: off (ORK_METER_TRIM=off)."' ORK_METER_TRIM=off ORK_METER_TRIM_BUDGET_BASH=32K
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json ORK_METER_TRIM_BUDGET_BASH=32K
silent "startup with a budget override: silent"
status_is "startup with a budget override: the pin carries it" '"Bash 32K" in s'
status_is "startup without a read-guard override: the default threshold" '"; read guard 96K)." in s'
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json ORK_METER_READ_GUARD_BYTES=262144 ORK_METER_TRIM_BUDGET_GREP=16kib ORK_METER_TRIM_BUDGET_GLOB=junk
silent "startup with overrides: silent"
status_is "startup with overrides: the pin carries the thresholds in force" 's.startswith("Ork Meter trim: on (Bash 12K, Read 24K, Grep 16K, Glob 4K, WebFetch 16K; read guard 256K).")'
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json ORK_METER_READ_GUARD_BYTES=100
silent "startup with a read-guard value below the floor: silent"
status_is "startup with a read-guard value below the floor: the floor is pinned" '"; read guard 4K)." in s'
run_hook trim-session.py sessionstart.startup.json ORK_METER_READ_GUARD=off
silent "startup with the read guard off: silent"
status_is "startup with the read guard off: the opt-out is in force" '"; read guard off)." in s' ORK_METER_READ_GUARD=off
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
assert o["features"] == sorted(o["features"]) == sorted(["audit", "cache_nudge", "dedupe", "loop_guard", "read_guard", "test_filter", "trim"] ), o
assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", o["updated_at"])
EOF
then echo "  ok   feature file: exact shape, sorted features, RFC3339 UTC"; else echo "  FAIL feature file shape"; fails=$((fails + 1)); fi
fmode=$(file_mode "$features"); dmode=$(file_mode "$(dirname "$features")")
[ "$fmode" = "600" ] && [ "$dmode" = "700" ] && echo "  ok   feature file 600, directory 700" || { echo "  FAIL feature file modes $fmode/$dmode"; fails=$((fails + 1)); }
run_hook trim-session.py sessionstart.compact.json ORK_METER_TRIM=off ORK_METER_NUDGES=off ORK_METER_LOOP_GUARD=off
if python3 -c 'import json,sys; o=json.load(open(sys.argv[1])); assert o["features"] == sorted(["audit"] ), o' "$features"; then
  echo "  ok   env opt-outs removed (trim family, nudges, loop guard)"; else echo "  FAIL opt-outs"; fails=$((fails + 1)); fi
run_hook trim-session.py sessionstart.compact.json ORK_METER_DEDUPE=off ORK_METER_READ_GUARD=off ORK_METER_TEST_FILTER=off
if python3 -c 'import json,sys; o=json.load(open(sys.argv[1])); assert o["features"] == sorted(["audit", "cache_nudge", "loop_guard", "trim"] ), o' "$features"; then
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

# Cache nudge (#59): only when prompt_cache_likely_expired and context_tokens >= threshold. The nudge
# text is stripped with the systemMessage (native_text.clean_reply); the cache_nudge ledger row is
# what remains of a nudge, so "no nudge" is "no new cache_nudge row".
run_hook trim-session.py sessionstart.resume-expired.json
last_nudge "expired + large: silent; a cache_nudge row with the context size and cost" 'r["context_tokens"] == 182340 and r["est_cache_write_usd"] == 1.1396'
n=$(nudges)
run_hook trim-session.py sessionstart.resume-fresh.json
silent "not expired: silent"
[ "$(nudges)" = "$n" ] && echo "  ok   not expired: no nudge row" || { echo "  FAIL not expired: a nudge row was added"; fails=$((fails + 1)); }
run_hook trim-session.py sessionstart.resume-expired-small.json
silent "expired but 12,000 tokens: silent"
[ "$(nudges)" = "$n" ] && echo "  ok   expired but 12,000 tokens: no nudge row" || { echo "  FAIL expired but small: a nudge row was added"; fails=$((fails + 1)); }
reset_fixture_policy
run_hook trim-session.py sessionstart.resume-expired-small.json ORK_METER_CACHE_NUDGE_TOKENS=10000
last_nudge "threshold lowered to 10,000: a cache_nudge row for 12,000 tokens" 'r["context_tokens"] == 12000 and round(r["est_cache_write_usd"], 2) == 0.07'
n=$(nudges)
run_hook trim-session.py sessionstart.resume-expired.json ORK_METER_NUDGES=off
silent "ORK_METER_NUDGES=off: silent"
[ "$(nudges)" = "$n" ] && echo "  ok   ORK_METER_NUDGES=off: no nudge row" || { echo "  FAIL ORK_METER_NUDGES=off: a nudge row was added"; fails=$((fails + 1)); }
run_hook trim-session.py sessionstart.resume.json
silent "resume without the cache fields: silent"
[ "$(nudges)" = "$n" ] && echo "  ok   resume without the cache fields: no nudge row" || { echo "  FAIL resume without the cache fields: a nudge row was added"; fails=$((fails + 1)); }

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
