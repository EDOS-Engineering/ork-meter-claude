#!/usr/bin/env bash
# Tests for the Ork Meter Plus gate (hooks/lib/entitlement.py, features.gate,
# ork-meter #264): the legacy cache rules, hooks passing results through
# untouched without Plus, env opt-outs on top of the gate, the one session-start
# line and plugin-features.json reporting gated levers as off. The signed
# policy envelope itself is covered by tests/plus-policy.test.sh.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

features="$XDG_STATE_HOME/ork-meter/plugin-features.json"
paused='Ork Meter savings are off: they are part of Ork Meter Plus. Upgrade at https://orkmeter.com/billing'


pyexprs \
  "status: the legacy cache rules (Plus states, offline grace, strict file), before LEGACY_UNTIL" '
import json, os, tempfile, calendar, time
import entitlement as e
d = tempfile.mkdtemp()
env = {"CLAUDE_PLUGIN_DATA": d}
p = os.path.join(d, "entitlement.json")
def put(body, mode=0o600):
    with open(p, "w") as f:
        f.write(body if isinstance(body, str) else json.dumps(body))
    os.chmod(p, mode)
T = lambda s: calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
now = T("2026-10-14T12:00:00Z")
assert now < e.LEGACY_UNTIL == T(e.LEGACY_UNTIL_TEXT) == T("2026-11-30T00:00:00Z")
assert e.status({}, now) == e.NOT_CONNECTED, "no data dir: not connected"
assert e.status({"CLAUDE_PLUGIN_DATA": "relative"}, now) == e.NOT_CONNECTED
assert e.status(env, now) == e.NOT_CONNECTED, "no file: not connected"
base = {"optimize": True, "state": "trial", "until": "2026-10-21T12:00:00Z", "checked_at": "2026-10-14T12:00:00Z"}
for state in ("promo", "trial", "active", "past_due"):
    put(dict(base, state=state)); assert e.status(env, now) == e.ENTITLED, state
assert e.policy(env, now) is None, "no envelope: no policy"
put(dict(base, optimize=False, state="free")); assert e.status(env, now) == e.NOT_ENTITLED, "free is visibility only"
put(dict(base, optimize=False, state="active")); assert e.status(env, now) == e.NOT_ENTITLED, "optimize false wins"
put(base)
assert e.status(env, T("2026-10-22T12:00:00Z")) == e.ENTITLED, "within 24 h past until"
assert e.status(env, T("2026-10-22T12:00:01Z")) == e.NOT_ENTITLED, "more than 24 h past until: fail closed"
put(dict(base, until=None))
assert e.status(env, T("2026-10-15T11:59:59Z")) == e.ENTITLED and e.status(env, T("2026-10-15T12:00:01Z")) == e.NOT_ENTITLED, "null until: bounded by checked_at"
put(dict(base, until="2026-10-21T14:00:00+02:00")); assert e.status(env, T("2026-10-22T12:00:00Z")) == e.ENTITLED, "offsets"
bad = [dict(base, state="free"), dict(base, state="expired"), dict(base, optimize="true"), dict(base, until="soon"),
       dict(base, checked_at=None), dict(base, extra=1), {k: v for k, v in base.items() if k != "until"}, [], "not json",
       json.dumps(base)[:-1] + ", \"state\": \"active\"}", " " * 2000]
for body in bad:
    put(body); assert e.status(env, now) == e.NOT_ENTITLED, body
put(base, 0o644); assert e.status(env, now) == e.NOT_ENTITLED, "group/world-readable refused"
os.unlink(p); os.mkdir(p); assert e.status(env, now) == e.NOT_ENTITLED, "a directory is not a cache"
' \
  "gate: every lever off without Plus; opt-outs still apply on top while entitled; pure functions unchanged" '
import json, os, sys, tempfile
sys.path.insert(0, "tests")
import entitlement, features, plus_policy_sign
d = tempfile.mkdtemp()
assert features.enabled({}, "trim"), "pure functions never read the files"
assert features.enabled_features(features.gate({"CLAUDE_PLUGIN_DATA": d})) == [], "not connected: all off"
plus_policy_sign.install(d)
live = features.gate({"CLAUDE_PLUGIN_DATA": d})
assert live.entitlement == "entitled" and features.enabled(live, "trim") and features.enabled(live, "dedupe")
off = features.gate({"CLAUDE_PLUGIN_DATA": d, "ORK_METER_DEDUPE": "off"})
assert features.enabled(off, "trim") and not features.enabled(off, "dedupe"), "opt-out on top of the gate"
plus_policy_sign.install(d, optimize=False, state="free", until=None)
gated = features.gate({"CLAUDE_PLUGIN_DATA": d})
assert features.enabled_features(gated) == [] and gated.entitlement == "not_entitled"
wrapped = features.RuntimeEnvironment(gated, ())
assert features.enabled_features(wrapped) == [] and wrapped.entitlement == "not_entitled", "the reducer circuit cannot re-enable gated levers"
assert features.status_line(gated) == entitlement.MESSAGE
assert features.status_line(features.gate({"CLAUDE_PLUGIN_DATA": tempfile.mkdtemp()})) is None, "not connected: the runtime asks to connect"
'

# Entitled (the lib.sh default): levers run.
run_hook trim-tool-output.py posttooluse.bash.large.json
assert "Plus: a large Bash result is trimmed" '"updatedToolOutput" in o["hookSpecificOutput"]'

# Free: hooks pass Claude's results through untouched, and say so once.
write_entitlement false free null
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json
assert "free: startup says savings are off, once, and nothing else" "o == {'systemMessage': '$paused'}"
assert "free: plugin-features.json reports every lever off" "json.load(open('$features'))['features'] == []"
run_hook trim-session.py sessionstart.resume.json
assert "free: resume says it again for that session" "o == {'systemMessage': '$paused'}"
run_hook trim-session.py sessionstart.clear.json
silent "free: clear stays silent"
for case in "trim-tool-output.py posttooluse.bash.large.json" "trim-tool-output.py posttooluse.grep.large.json" \
            "trim-tool-output.py posttooluse.webfetch.large.json" "read-guard.py pretooluse.read.plain.json" \
            "dedupe-reads.py posttooluse.read.large.json" "loop-guard.py posttoolusefailure.bash.json"; do
  set -- $case
  run_hook "$1" "$2"
  silent "free: $1 ($2) passes through untouched"
done
run_hook trim-tool-output.py posttooluse.bash.large.json ORK_METER_MODE=reduce ORK_METER_TRIM=on
silent "free: env switches cannot turn a gated lever on"

# Offline past the grace period: fail closed.
write_entitlement true active '"2026-01-01T00:00:00Z"'
run_hook trim-tool-output.py posttooluse.bash.large.json
silent "stale answer (more than 24 h past until): passes through"

# Not connected: no file, no lever, no extra line (the runtime asks to connect).
remove_entitlement
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json
silent "not connected: no status line from the trim hook"
run_hook trim-tool-output.py posttooluse.bash.large.json
silent "not connected: passes through"

# Back on Plus mid-session: levers resume, and opt-outs still work on top.
write_entitlement true promo
run_hook trim-tool-output.py posttooluse.bash.large.json
assert "promo: trimming resumes" '"updatedToolOutput" in o["hookSpecificOutput"]'

[ "$fails" -eq 0 ]
