#!/usr/bin/env bash
# Tests for the signed Plus policy (ork-meter docs/plus-policy.md): the vendored
# verifier is the contract's byte for byte and accepts the contract fixtures;
# hooks/lib/entitlement.py binds an envelope to connection.json and falls back
# to the legacy entitlement.json only before LEGACY_UNTIL; features.gate applies
# the policy's levers and tunable defaults with env settings only turning things
# off on top; and the hooks observe all of it end to end.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

# The digest of ork-meter docs/plus-policy-fixtures/plus_policy.py (contract v1, 2026-10-21; cell keys added).
export VERIFIER_SHA256=313b6126d9a1b982494d76e6491a4067bb0105240a8b764851a812905b7326f4
export FIXTURES=tests/fixtures/plus-policy

pyexprs \
  "vendored verifier: byte-identical to the contract (sha256), the test key, and the contract fixtures verify" '
import calendar, hashlib, json, os
import features, plus_policy
digest = hashlib.sha256(open("hooks/lib/plus_policy.py", "rb").read()).hexdigest()
assert digest == os.environ["VERIFIER_SHA256"], digest
F = os.environ["FIXTURES"]
key = open(os.path.join(F, "test-public-key.hex")).read().strip()
assert plus_policy.PUBLIC_KEYS["orkmeter-test-2026"] == (key, "https://plus-policy-test.invalid")
assert [k for k, (_, origin) in plus_policy.PUBLIC_KEYS.items() if origin.endswith(".invalid")] == ["orkmeter-test-2026"], "only the test key signs for the test origin"
REF, ORIGIN = "fff8bc443e61f3d6b4e78c55eff5ba01", "https://plus-policy-test.invalid"
NOW = calendar.timegm((2026, 10, 21, 20, 30, 0))
fx = lambda name: open(os.path.join(F, name + ".json"), "rb").read()
v = plus_policy.verify(fx("claude-code-entitled"), "claude-code", REF, ORIGIN, NOW)
assert v["policy"]["id"] == "claude-code-defaults-2026-10-22" and plus_policy.entitled(v, NOW)
assert v["entitlement"]["state"] == "promo" and v["audience"] == REF and v["issuer"] == ORIGIN
assert set(v["policy"]["levers"]) == set(features.LEVERS), "the contract policy names every lever in LEVERS"
assert all(flag is True for flag in v["policy"]["levers"].values()), "and leaves none off (the opt-ins still need their switch)"
known = {"%s.%s" % (lever, key) for lever, spec in features.LEVERS.items() for key in spec["tunables"]}
assert set(v["policy"]["tunables"]) <= known, set(v["policy"]["tunables"]) - known
for name, raw in v["policy"]["tunables"].items():
    lever, key = name.split(".")
    assert features.policy_default(v["policy"], lever, key, features.LEVERS[lever]["tunables"][key]) == raw, name
c = plus_policy.verify(fx("codex-entitled"), "codex", REF, ORIGIN, NOW)
assert c["plugin"] == "codex" and plus_policy.entitled(c, NOW)
free = plus_policy.verify(fx("codex-free"), "codex", REF, ORIGIN, NOW)
assert free["entitlement"] == {"optimize": False, "state": "free", "until": None, "checked_at": calendar.timegm((2026, 10, 21, 20, 0, 0))}
assert not plus_policy.entitled(free, NOW), "a free envelope verifies but does not entitle"
def refused(raw, plugin="claude-code", reference=REF, origin=ORIGIN, now=NOW):
    try:
        plus_policy.verify(raw, plugin, reference, origin, now)
    except ValueError:
        return True
    return False
assert refused(fx("codex-entitled")), "another plugin envelope"
assert refused(fx("codex-other-audience"), "codex"), "another audience"
assert refused(fx("claude-code-entitled"), reference="0" * 32), "wrong reference"
assert refused(fx("claude-code-entitled"), origin="https://orkmeter.com"), "wrong origin"
assert refused(fx("claude-code-entitled"), now=NOW - 3600), "issued more than 5 minutes in the future"
assert refused(fx("claude-code-entitled"), now=calendar.timegm((2026, 10, 22, 20, 0, 1))), "past expires_at"
env = json.loads(fx("claude-code-entitled"))
flip = lambda text, i: text[:i] + ("A" if text[i] != "A" else "B") + text[i + 1:]
assert refused(json.dumps(dict(env, payload=flip(env["payload"], 40))).encode()), "tampered payload"
assert refused(json.dumps(dict(env, signature=flip(env["signature"], 10))).encode()), "tampered signature"
assert refused(json.dumps(dict(env, key_id="orkmeter-2026-11")).encode()), "unknown key"
assert refused(fx("claude-code-entitled") + b" " * 8192), "oversized envelope"
' \
  "signer (tests/plus_policy_sign.py): the test seed derives the fixture key and reproduces every fixture signature" '
import base64, calendar, json, os, sys
sys.path.insert(0, "tests")
import plus_policy, plus_policy_sign as signer
F = os.environ["FIXTURES"]
assert signer.public_key() == open(os.path.join(F, "test-public-key.hex")).read().strip()
for name in ("claude-code-entitled", "codex-entitled", "codex-free", "codex-other-audience"):
    env = json.loads(open(os.path.join(F, name + ".json"), "rb").read())
    raw = base64.urlsafe_b64decode(env["payload"] + "==")
    assert signer.b64(signer.sign(raw)) == env["signature"], name   # Ed25519 is deterministic
NOW = calendar.timegm((2026, 10, 21, 20, 30, 0))
v = plus_policy.verify(signer.envelope(now=NOW), "claude-code", signer.REFERENCE, signer.ORIGIN, NOW)
assert v["policy"] == signer.DEFAULT_POLICY and plus_policy.entitled(v, NOW)
issued = calendar.timegm((2026, 10, 21, 20, 0, 0))
ours = signer.payload(state="promo", until="2026-10-31T23:59:59Z", checked_at="2026-10-21T20:00:00Z", issued_at=issued)
fixture = json.loads(open(os.path.join(F, "claude-code-entitled.json"), "rb").read())
assert json.dumps(ours, sort_keys=True, separators=(",", ":")) == base64.urlsafe_b64decode(fixture["payload"] + "==").decode(), "the same document"
' \
  "entitlement: a valid envelope with its connection.json entitles; audience, origin, tampering, plugin, freshness and a missing connection.json do not" '
import calendar, json, os, shutil, sys, tempfile
sys.path.insert(0, "tests")
import entitlement as e, plus_policy_sign as signer
NOW = calendar.timegm((2026, 10, 21, 20, 30, 0))
F = os.environ["FIXTURES"]
d = tempfile.mkdtemp(); env = {"CLAUDE_PLUGIN_DATA": d}
P, C, L = (os.path.join(d, n) for n in ("plus-policy.json", "connection.json", "entitlement.json"))
def put(path, raw, mode=0o600):
    with open(path, "wb") as f:
        f.write(raw if isinstance(raw, bytes) else raw.encode())
    os.chmod(path, mode)
def connection(reference=signer.REFERENCE, origin=signer.ORIGIN, **extra):
    put(C, json.dumps(dict({"reference": reference, "origin": origin}, **extra)))
def clear():
    for p in (P, C, L):
        if os.path.lexists(p):
            (shutil.rmtree if os.path.isdir(p) else os.unlink)(p)
assert e.resolve(env, NOW) == (e.NOT_CONNECTED, None), "nothing at all"
connection(); assert e.resolve(env, NOW) == (e.NOT_CONNECTED, None), "paired, no answer cached yet"
shutil.copy(os.path.join(F, "claude-code-entitled.json"), P); os.chmod(P, 0o600)
state, policy = e.resolve(env, NOW)
assert state == e.ENTITLED and policy["id"] == "claude-code-defaults-2026-10-22" and policy["levers"]["trim"] is True
assert e.status(env, NOW) == e.ENTITLED and e.policy(env, NOW) == policy
assert e.status(env, calendar.timegm((2026, 11, 1, 23, 59, 59))) == e.NOT_ENTITLED, "the envelope itself expires before until does"
assert e.status(env, NOW - 3600) == e.NOT_ENTITLED, "issued in the future"
put(L, json.dumps({"optimize": False, "state": "free", "until": None, "checked_at": "2026-10-21T20:00:00Z"}))
assert e.status(env, NOW) == e.ENTITLED, "a legacy file beside a valid envelope is not consulted"
os.unlink(L)
connection(reference="0" * 32); assert e.resolve(env, NOW) == (e.NOT_ENTITLED, None), "wrong audience"
connection(origin="https://orkmeter.com"); assert e.resolve(env, NOW) == (e.NOT_ENTITLED, None), "wrong origin"
connection(origin="http://plus-policy-test.invalid"); assert e.status(env, NOW) == e.NOT_ENTITLED, "not https"
connection(origin="https://plus-policy-test.invalid/"); assert e.status(env, NOW) == e.NOT_ENTITLED, "an origin has no path"
connection(token="x"); assert e.status(env, NOW) == e.NOT_ENTITLED, "exactly two keys"
connection(reference=""); assert e.status(env, NOW) == e.NOT_ENTITLED
put(C, json.dumps({"reference": signer.REFERENCE})); assert e.status(env, NOW) == e.NOT_ENTITLED
put(C, "{\"reference\": \"%s\", \"reference\": \"x\", \"origin\": \"%s\"}" % (signer.REFERENCE, signer.ORIGIN)); assert e.status(env, NOW) == e.NOT_ENTITLED, "duplicate key"
put(C, " " * 2000); assert e.status(env, NOW) == e.NOT_ENTITLED, "oversized connection"
connection(); os.chmod(C, 0o644); assert e.status(env, NOW) == e.NOT_ENTITLED, "connection readable by others"
connection(); assert e.status(env, NOW) == e.ENTITLED
os.unlink(C); assert e.resolve(env, NOW) == (e.NOT_ENTITLED, None), "envelope without connection.json"
connection()
raw = open(os.path.join(F, "claude-code-entitled.json"), "rb").read()
put(P, raw.replace(b"orkmeter-test-2026", b"orkmeter-2026-11")); assert e.status(env, NOW) == e.NOT_ENTITLED, "unknown key"
body = json.loads(raw)
put(P, json.dumps(dict(body, payload=body["payload"][:50] + ("A" if body["payload"][50] != "A" else "B") + body["payload"][51:]))); assert e.status(env, NOW) == e.NOT_ENTITLED, "tampered payload"
put(P, json.dumps(dict(body, signature=body["signature"][:-2] + "AA"))); assert e.status(env, NOW) == e.NOT_ENTITLED, "tampered signature"
put(P, json.dumps({k: v for k, v in body.items() if k != "key_id"})); assert e.status(env, NOW) == e.NOT_ENTITLED, "envelope fields"
put(P, "not json"); assert e.status(env, NOW) == e.NOT_ENTITLED
put(P, raw, 0o640); assert e.status(env, NOW) == e.NOT_ENTITLED, "envelope readable by the group"
put(P, raw + b" " * 8192); assert e.status(env, NOW) == e.NOT_ENTITLED, "oversized envelope"
os.unlink(P); os.mkdir(P); assert e.status(env, NOW) == e.NOT_ENTITLED, "a directory is not an envelope"
clear(); connection()
shutil.copy(os.path.join(F, "codex-entitled.json"), P); os.chmod(P, 0o600)
assert e.resolve(env, NOW) == (e.NOT_ENTITLED, None), "a codex envelope is refused by the Claude plugin"
shutil.copy(os.path.join(F, "codex-other-audience.json"), P); os.chmod(P, 0o600)
assert e.status(env, NOW) == e.NOT_ENTITLED
put(P, signer.envelope(now=NOW, optimize=False, state="free", until=None))
state, policy = e.resolve(env, NOW)
assert state == e.NOT_ENTITLED and policy == signer.DEFAULT_POLICY, "a free envelope verifies (its policy is returned) but does not entitle"
put(P, signer.envelope(now=NOW, until="2026-10-21T00:00:00Z"))
assert e.status(env, NOW) == e.ENTITLED, "within 24 h past until"
put(P, signer.envelope(now=NOW, until="2026-10-20T20:00:00Z", checked_at="2026-10-20T20:00:00Z"))
assert e.status(env, NOW + 3600) == e.NOT_ENTITLED, "more than 24 h past until: fail closed"
put(P, signer.envelope(now=NOW, state="past_due", until="2026-10-21T20:00:00Z"))
assert e.status(env, NOW) == e.ENTITLED
put(P, signer.envelope(now=NOW))
assert e.status(env, NOW + 24 * 3600 + 15 * 60) == e.NOT_ENTITLED, "the envelope expires 24 h after issue"
assert e.status(env, NOW + 23 * 3600) == e.ENTITLED
put(P, signer.envelope(now=NOW, plugin="codex")); assert e.status(env, NOW) == e.NOT_ENTITLED
put(P, signer.envelope(now=NOW, audience="0" * 32)); assert e.status(env, NOW) == e.NOT_ENTITLED
put(P, signer.envelope(now=NOW, issuer="https://orkmeter.com")); assert e.status(env, NOW) == e.NOT_ENTITLED, "the test key signs only for its origin"
put(P, signer.envelope(now=NOW, policy={"levers": {}})); assert e.status(env, NOW) == e.NOT_ENTITLED, "a policy needs an id"
put(P, signer.envelope(now=NOW, policy={"id": "p"})); assert e.resolve(env, NOW) == (e.ENTITLED, {"id": "p"}), "a bare policy verifies"
' \
  "legacy: entitlement.json alone entitles only before LEGACY_UNTIL; an envelope takes over whenever it exists" '
import calendar, json, os, sys, tempfile
sys.path.insert(0, "tests")
import entitlement as e, plus_policy_sign as signer
d = tempfile.mkdtemp(); env = {"CLAUDE_PLUGIN_DATA": d}
L = os.path.join(d, "entitlement.json")
with open(L, "w") as f:
    json.dump({"optimize": True, "state": "active", "until": "2099-01-01T00:00:00Z", "checked_at": "2026-10-21T20:00:00Z"}, f)
os.chmod(L, 0o600)
assert e.LEGACY_UNTIL == calendar.timegm((2026, 11, 30, 0, 0, 0))
assert e.resolve(env, e.LEGACY_UNTIL - 1) == (e.ENTITLED, None), "an older runtime may still be installed"
assert e.resolve(env, e.LEGACY_UNTIL) == (e.NOT_ENTITLED, None), "from the cutoff the unsigned answer counts for nothing"
assert e.resolve(env, e.LEGACY_UNTIL + 365 * 86400) == (e.NOT_ENTITLED, None)
with open(os.path.join(d, "connection.json"), "w") as f:
    json.dump({"reference": signer.REFERENCE, "origin": signer.ORIGIN}, f)
os.chmod(os.path.join(d, "connection.json"), 0o600)
assert e.status(env, e.LEGACY_UNTIL - 1) == e.ENTITLED, "a new runtime that has not fetched a policy yet: legacy rules"
later = e.LEGACY_UNTIL + 86400
signer.install(d, now=later)
assert e.status(env, later) == e.ENTITLED, "a signed envelope entitles after the cutoff"
signer.install(d, now=later, policy={"id": "trim-off", "levers": {"loop_guard": True}})
assert e.policy(env, later) == {"id": "trim-off", "levers": {"loop_guard": True}}
os.unlink(os.path.join(d, "plus-policy.json"))
assert e.status(env, later) == e.NOT_ENTITLED, "the envelope removed (401/403): the legacy file no longer counts"
' \
  "gate/enabled/value: the policy gates every lever and supplies tunable defaults; env settings only turn things off on top" '
import features
from features import RuntimeEnvironment as RE
P = {"id": "test-policy-1", "levers": {"trim": False, "test_filter": True, "dedupe": True, "read_guard": True, "loop_guard": True,
                                      "stale_guard": True, "delta": True, "no_such_lever": True},
     "tunables": {"trim.budget_bash": 32768, "trim.budget_read": 10, "read_guard.lines": 5000, "read_guard.compact": 0,
                  "cache_nudge.tokens": "50000", "loop_guard.warn": True, "loop_guard.stop": 4, "trim.budget_grep": -1,
                  "trim.nope": 1, "nope.budget_bash": 1, "context_hygiene.tokens": 1.5}}
plain, under = {}, RE({}, policy=P)
assert under.policy is P and RE(under).policy is P and RE(under, ("dedupe",)).policy is P, "wrapping keeps the policy"
assert features.carried(dict(under), under).policy is P and features.carried(under, under) is under and features.carried({}, {}) == {}
assert features.enabled(plain, "trim") and not features.enabled(under, "trim"), "trim false: off with no env opt-out"
assert not features.enabled(RE({"ORK_METER_TRIM": "on"}, policy=P), "trim"), "an explicit on (a project, say) cannot turn a policy-disabled lever on"
assert features.enabled(under, "test_filter") and features.enabled(under, "dedupe") and features.enabled(under, "read_guard")
assert not features.enabled(RE({"ORK_METER_DEDUPE": "off"}, policy=P), "dedupe"), "env opt-out on top"
assert not features.enabled(under, "cache_nudge") and not features.enabled(under, "audit"), "missing from the map: off"
assert "no_such_lever" not in features.enabled_features(under), "unknown policy levers are ignored"
assert not features.enabled(under, "stale_guard") and not features.enabled(under, "delta"), "opt-in: the policy alone is not enough"
assert features.enabled(RE({"ORK_METER_STALE_GUARD": "on", "ORK_METER_DELTA": "on"}, policy=P), "stale_guard")
assert features.enabled(RE({"ORK_METER_STALE_GUARD": "on", "ORK_METER_DELTA": "on"}, policy=P), "delta")
assert not features.enabled(RE({"ORK_METER_DELTA": "on"}, policy=dict(P, levers=dict(P["levers"], delta=False))), "delta"), "opt-in with the policy false"
assert not features.enabled(RE({"ORK_METER_CONTEXT_HYGIENE": "on"}, policy=P), "context_hygiene"), "an opt-in missing from the policy"
assert features.enabled_features(under) == ["dedupe", "loop_guard", "read_guard", "test_filter"], features.enabled_features(under)
assert features.enabled_features(RE({}, policy={"id": "x"})) == [] and features.enabled_features(RE({}, policy={"id": "x", "levers": []})) == [], "no levers map: all off"
for mode in ("shadow", "off"):
    assert not features.enabled(RE({"ORK_METER_MODE": mode}, policy=P), "dedupe"), "mode still applies"
# Tunables: the policy sets the default; floors, ceilings and the env still apply; junk is ignored.
assert features.value(plain, "trim", "budget_bash") == 12288 and features.value(under, "trim", "budget_bash") == 32768
assert features.trim_budget(under, "Bash") == 32768 and features.trim_budget(under, "WebFetch") == 16384, "an unlisted tunable keeps its built-in default"
assert features.value(RE({"ORK_METER_TRIM_BUDGET_BASH": "16k"}, policy=P), "trim", "budget_bash") == 16384, "an explicit env value wins"
assert features.value(RE({"ORK_METER_TRIM_BUDGET_BASH": "junk"}, policy=P), "trim", "budget_bash") == 32768, "an invalid env value gives the policy default"
assert features.value(RE({"ORK_METER_TRIM_BUDGET_BASH": "1"}, policy=P), "trim", "budget_bash") == 1024
assert features.value(under, "trim", "budget_read") == 1024, "below the floor: the floor"
assert features.value(under, "read_guard", "lines") == 1500, "above the ceiling: the ceiling"
assert features.value(under, "read_guard", "compact") == 0 and features.value(RE({"ORK_METER_READ_COMPACT": "on"}, policy=P), "read_guard", "compact") == 1
assert features.value(RE({}, policy=dict(P, tunables={"read_guard.compact": 2})), "read_guard", "compact") == 0, "a switch takes 0 or 1 only: anything else is the built-in default (off)"
assert features.value(under, "cache_nudge", "tokens") == 40000, "a string is not a number"
assert features.value(under, "loop_guard", "warn") == 3, "a bool is not a number"
assert features.value(under, "loop_guard", "stop") == 4 and features.value(RE({"ORK_METER_LOOP_STOP": "off"}, policy=P), "loop_guard", "stop") == 4, "off means unset: the policy default"
assert features.value(under, "trim", "budget_grep") == 8192, "negative: ignored"
assert features.value(under, "context_hygiene", "tokens") == 120000, "a float: ignored"
assert features.value(RE({}, policy={"id": "x", "tunables": "junk"}), "trim", "budget_bash") == 12288
assert features.policy_settings(under)["ORK_METER_TRIM_BUDGET_BASH"] == "32768", "a pin taken from a gated environment records the policy default"
assert features.policy_settings(plain)["ORK_METER_TRIM_BUDGET_BASH"] == "12288"
# Status line: the policy id, and the reason trim is off.
on = RE({}, policy=dict(P, levers=dict(P["levers"], trim=True)))
assert features.status_line(on) == "Ork Meter trim: on (Bash 32K, Read 1K, Grep 8K, Glob 4K, WebFetch 16K; read guard 96K). Policy test-policy-1.", features.status_line(on)
assert features.status_line(under) == "Ork Meter trim: off (policy test-policy-1)."
assert features.status_line(RE({"ORK_METER_TRIM": "off"}, policy=dict(P, levers=dict(P["levers"], trim=True)))) == "Ork Meter trim: off (ORK_METER_TRIM=off)."
assert features.status_line({}) == "Ork Meter trim: on (Bash 12K, Read 24K, Grep 8K, Glob 4K, WebFetch 16K; read guard 96K)."
assert features.policy_id(RE({}, policy={"id": "a b" + chr(10) + "c" * 80})) == "ab" + "c" * 62 and features.policy_id({}) is None
' \
  "gate: reads the files once and threads the policy; legacy entitlement applies the built-in defaults" '
import json, os, sys, tempfile
sys.path.insert(0, "tests")
import entitlement, features, plus_policy_sign as signer
d = tempfile.mkdtemp(); env = {"CLAUDE_PLUGIN_DATA": d}
signer.install(d, policy={"id": "gate-test", "levers": {"dedupe": True, "loop_guard": True}, "tunables": {"loop_guard.warn": 7}})
live = features.gate(env)
assert live.entitlement == entitlement.ENTITLED and live.policy["id"] == "gate-test"
assert features.enabled_features(live) == ["dedupe", "loop_guard"] and features.value(live, "loop_guard", "warn") == 7
assert features.enabled_features(features.gate(dict(env, ORK_METER_DEDUPE="off"))) == ["loop_guard"]
assert features.enabled_features(features.gate(dict(env, ORK_METER_TRIM="on", ORK_METER_NUDGES="on"))) == ["dedupe", "loop_guard"], "env cannot add levers"
assert features.enabled_features(features.RuntimeEnvironment(live, ("dedupe",))) == ["loop_guard"], "the circuit keeps the policy"
assert features.status_line(live) == "Ork Meter trim: off (policy gate-test)."
signer.install(d, optimize=False, state="free", until=None, policy={"id": "gate-test", "levers": {"dedupe": True}})
free = features.gate(env)
assert free.entitlement == entitlement.NOT_ENTITLED and free.policy is None and features.enabled_features(free) == []
assert features.gate(free).policy is None, "regating a not-entitled environment never resurrects a policy"
signer.install(d)   # an entitled answer, then an older runtime: only its entitlement.json
for name in ("plus-policy.json", "connection.json"):
    os.unlink(os.path.join(d, name))
legacy = features.gate(env, entitlement.LEGACY_UNTIL - 1)
assert legacy.entitlement == entitlement.ENTITLED and legacy.policy is None, "the legacy answer: no policy"
assert features.value(legacy, "loop_guard", "warn") == 3 and "trim" in features.enabled_features(legacy), "built-in defaults apply as today"
assert features.gate(env, entitlement.LEGACY_UNTIL).entitlement == entitlement.NOT_ENTITLED
'

features_file="$XDG_STATE_HOME/ork-meter/plugin-features.json"
# End to end. The lib.sh default is the contract's claude-code policy: every lever named, the opt-ins still off without their switch.
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json
assert "contract policy: the status line names it" 'o["systemMessage"] == "Ork Meter trim: on (Bash 12K, Read 24K, Grep 8K, Glob 4K, WebFetch 16K; read guard 96K). Policy claude-code-defaults-2026-10-22."'
assert "contract policy: plugin-features.json lists every lever but the opt-ins" "json.load(open('$features_file'))['features'] == ['audit', 'cache_nudge', 'dedupe', 'loop_guard', 'read_guard', 'test_filter', 'trim']"
run_hook trim-tool-output.py posttooluse.bash.large.json
assert "contract policy: a large Bash result is trimmed" '"updatedToolOutput" in o["hookSpecificOutput"]'

# A policy with trim false: the trim hooks pass through even with no env opt-out,
# and an environment (or project) value of on changes nothing.
policy='{"id":"no-trim-2026","levers":{"loop_guard":true,"dedupe":true},"tunables":{}}'
write_entitlement true active '"2099-01-01T00:00:00Z"' "$policy"
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json
assert "trim off by policy: the status line says which policy" 'o["systemMessage"] == "Ork Meter trim: off (policy no-trim-2026)."'
run_hook trim-tool-output.py posttooluse.bash.large.json
silent "trim off by policy: a large Bash result passes through untouched"
run_hook trim-tool-output.py posttooluse.bash.large.json ORK_METER_TRIM=on ORK_METER_MODE=reduce
silent "trim off by policy: ORK_METER_TRIM=on cannot turn it on"
run_hook read-guard.py pretooluse.read.plain.json
silent "read guard missing from the policy: off"
run_hook loop-guard.py posttoolusefailure.bash.json
silent "loop guard on by policy: the first failure is below the warn threshold"
assert "plugin-features.json lists the policy's levers only" "json.load(open('$features_file'))['features'] == ['dedupe', 'loop_guard']"

# A policy tunable: the pin records it at session start and the hooks use it.
policy='{"id":"wide-bash-2026","levers":{"trim":true,"read_guard":true},"tunables":{"trim.budget_bash":65536,"read_guard.bytes":200000}}'
write_entitlement true active '"2099-01-01T00:00:00Z"' "$policy"
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json
assert "policy tunables: the status line shows them (clamped to the lever's grammar)" 'o["systemMessage"] == "Ork Meter trim: on (Bash 64K, Read 24K, Grep 8K, Glob 4K, WebFetch 16K; read guard 195K). Policy wide-bash-2026."'
run_hook trim-tool-output.py posttooluse.bash.large.json
silent "policy tunables: a 20 KiB Bash result is under the policy's 64 KiB budget"
run_hook trim-tool-output.py posttooluse.grep.large.json
assert "policy tunables: an unlisted budget keeps its built-in default" '"updatedToolOutput" in o["hookSpecificOutput"]'
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json ORK_METER_TRIM_BUDGET_BASH=8k
assert "an explicit env value still wins over the policy default" 'o["systemMessage"].startswith("Ork Meter trim: on (Bash 8K, ")'
run_hook trim-tool-output.py posttooluse.bash.large.json ORK_METER_TRIM_BUDGET_BASH=8k
assert "and the pinned env value trims" '"updatedToolOutput" in o["hookSpecificOutput"]'

# A project's settings still can only turn levers off: a project ORK_METER_TRIM=on is ignored under a policy with trim false.
project="$tmp/project"; mkdir -p "$project/.claude"
printf '{"env": {"ORK_METER_TRIM": "on", "ORK_METER_TRIM_BUDGET_BASH": "1k"}}' > "$project/.claude/settings.json"
write_entitlement true active '"2099-01-01T00:00:00Z"' '{"id":"no-trim-2026","levers":{"loop_guard":true}}'
reset_fixture_policy
run_hook trim-tool-output.py posttooluse.bash.large.json CLAUDE_PROJECT_DIR="$project" ORK_METER_TRIM=on
silent "a project cannot turn a policy-disabled lever on"

# Disconnect: the runtime removes the envelope and the connection; nothing runs, nothing is said.
remove_entitlement
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json
silent "disconnected: no status line"
run_hook trim-tool-output.py posttooluse.bash.large.json
silent "disconnected: passes through"

# No file outside features.py, entitlement.py's own files aside, reads the policy files or ORK_METER_* (tests/features.test.sh checks the latter).
if python3 - <<'EOF'
import os, re
offenders = []
for d in ("hooks", "hooks/lib", "scripts"):
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if not os.path.isfile(p) or f in ("entitlement.py",) or not (f.endswith(".py") or f.endswith(".sh")):
            continue
        src = open(p, encoding="utf-8").read()
        for m in re.finditer(r"plus-policy\.json|connection\.json|plus_policy\.verify", src):
            offenders.append((p, src[:m.start()].count("\n") + 1, m.group(0)))
assert not offenders, offenders
EOF
then echo "  ok   only hooks/lib/entitlement.py reads the policy files"; else echo "  FAIL the policy files are read outside entitlement.py"; fails=$((fails + 1)); fi

[ "$fails" -eq 0 ]
