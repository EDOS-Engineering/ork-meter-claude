#!/usr/bin/env bash
# Tests for hooks/lib/features.py, the lever registry: the one parser grammar, enabled() for
# every lever and opt-out combination, the status line, plugin_version, the docs table, and
# that no file outside features.py reads an ORK_METER_* variable.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh


export PLUGIN_COPY="$tmp/plugin-copy"; mkdir -p "$PLUGIN_COPY/.claude-plugin"
printf '{"version": "9.8.7-rc.1"}' > "$PLUGIN_COPY/.claude-plugin/plugin.json"

pyexprs \
  "parse_int: plain, k, kib (any case, spaces), empty, invalid, below floor" '
import features
p = features.parse_int
assert p("12288", 1, 1024) == 12288
assert p("12k", 1, 1024) == 12288 and p("12K", 1, 1024) == 12288
assert p("12kib", 1, 1024) == 12288 and p("12KiB", 1, 1024) == 12288 and p(" 12KIB ", 1, 1024) == 12288
assert p("0", 7, 0) == 0
for bad in ("", None, "  ", "abc", "12kb", "12 k", "1.5k", "-5", "+5", "k", "12kk", "1_000", "12b", "0x10"):
    assert p(bad, 999, 1) == 999, bad
assert p("512", 12288, 1024) == 1024, "below floor gives the floor, not the default"
assert p("0", 3, 1) == 1 and p("0k", 3, 1) == 1
assert p("1k", None, 1) == 1024 and p("", None, 1) is None, "a None default stays None when unset"
assert p("1950", 300, 50, 1500) == 1500 and p("9k", 300, 50, 1500) == 1500 and p("10", 300, 50, 1500) == 50, "above the ceiling gives the ceiling"
assert p("1500", 300, 50, 1500) == 1500 and p("junk", 300, 50, 1500) == 300
' \
  "value: every tunable in LEVERS parses from its env name with its default, floor and ceiling" '
import features
for lever, spec in features.LEVERS.items():
    for key, t in spec["tunables"].items():
        env_name, default, floor, unit = t.env, t.default, t.floor, t.unit
        assert isinstance(t, features.Tunable), (lever, key)
        assert env_name.startswith("ORK_METER_") and unit in ("bytes", "tokens", "seconds", "failures", "switch", "lines"), (lever, key)
        assert default is None or default >= floor, (lever, key)
        assert t.ceiling is None or t.ceiling >= (default or floor), (lever, key)
        assert features.value({}, lever, key) == default
        assert features.value({env_name: "junk"}, lever, key) == default
        if unit == "switch":   # the switch spellings, not the integer grammar
            assert default in (0, 1) and not t.off_means_unset and t.ceiling is None, (lever, key)
            assert [features.value({env_name: s}, lever, key) for s in features.ON_VALUES] == [1] * len(features.ON_VALUES)
            assert [features.value({env_name: s}, lever, key) for s in features.OFF_VALUES] == [0] * len(features.OFF_VALUES)
            assert features.value({env_name: " TRUE "}, lever, key) == 1 and features.value({env_name: "7"}, lever, key) == default
            continue
        assert features.value({env_name: "0"}, lever, key) == (default if t.off_means_unset else floor)
        assert features.value({env_name: str(floor + 7)}, lever, key) == floor + 7
        assert features.value({env_name: "9999k"}, lever, key) == min(9999 * 1024, t.ceiling or 9999 * 1024)
        if t.ceiling is not None:
            assert features.value({env_name: str(t.ceiling)}, lever, key) == t.ceiling
            assert features.value({env_name: str(t.ceiling + 1)}, lever, key) == t.ceiling
assert features.value({"ORK_METER_TRIM_BUDGET_BASH": "32K"}, "trim", "budget_bash") == 32 * 1024
assert features.trim_budget({}, "Bash") == 12 * 1024 and features.trim_budget({}, "Read") == 24 * 1024
assert features.trim_budget({}, "Grep") == 8 * 1024 and features.trim_budget({}, "Glob") == 4 * 1024
assert features.trim_budget({}, "WebFetch") == 16 * 1024
assert features.value({}, "read_guard", "bytes") == 96 * 1024
assert features.value({"ORK_METER_READ_GUARD_BYTES": "100"}, "read_guard", "bytes") == 4096
assert features.value({}, "cache_nudge", "tokens") == 40000 and features.value({}, "cache_nudge", "accept_seconds") == 300
assert features.value({}, "loop_guard", "warn") == 3 and features.value({}, "loop_guard", "stop") is None
assert features.value({"ORK_METER_LOOP_STOP": "5"}, "loop_guard", "stop") == 5
assert [k for l, s in features.LEVERS.items() for k, t in s["tunables"].items() if t.off_means_unset] == ["stop"], "only loop_guard.stop"
try:
    features.value({}, "trim", "nope"); raise SystemExit("unknown key must raise")
except KeyError:
    pass
' \
  "ORK_METER_LOOP_STOP: off spellings mean unset (never stop); anything else is the one grammar, floor 1" '
import features
stop = lambda raw: features.value({"ORK_METER_LOOP_STOP": raw}, "loop_guard", "stop")
assert features.value({}, "loop_guard", "stop") is None
for unset in ("0", "off", "", "OFF", " Off ", "false", "False", "no", "NO", "  "):
    assert stop(unset) is None, unset
assert stop("1") == 1 and stop("2") == 2 and stop("2k") == 2048 and stop("2KiB") == 2048
assert stop("junk") is None and stop("-1") is None and stop("1.5") is None, "invalid gives the default (unset)"
assert stop("0k") == 1 and stop("00") == 1, "only the exact off spellings mean unset; other zeros floor to 1"
assert features.value({"ORK_METER_LOOP_WARN": "0"}, "loop_guard", "warn") == 1, "warn has no off_means_unset: 0 floors to 1"
assert features.value({"ORK_METER_LOOP_WARN": "off"}, "loop_guard", "warn") == 3, "and off is just invalid there"
' \
  "registry: every lever with tunables has an on/off switch (policy adaptation turns a missing switch off)" '
import features
for name, lever in features.LEVERS.items():
    if lever["tunables"]:
        assert lever["opt_outs"] or lever.get("opt_in"), (name, "a tunable-only lever would start mid-session in sessions pinned before it (#105, hooks/lib/policy.adapt)")
' \
  "enabled: every lever against every opt-out combination, and every OFF_VALUES spelling" '
import itertools, features
assert tuple(features.OPT_OUTS) == features.ALL_FEATURES == ("trim", "test_filter", "dedupe", "read_guard", "haiku_agents", "cache_nudge", "loop_guard", "audit", "context_hygiene", "stale_guard", "delta")
assert features.OPT_INS == {"context_hygiene": "ORK_METER_CONTEXT_HYGIENE", "stale_guard": "ORK_METER_STALE_GUARD", "delta": "ORK_METER_DELTA"}, features.OPT_INS
assert features.OPT_OUTS == {
    "trim": ("ORK_METER_TRIM",),
    "dedupe": ("ORK_METER_TRIM", "ORK_METER_DEDUPE"),
    "read_guard": ("ORK_METER_TRIM", "ORK_METER_READ_GUARD"),
    "test_filter": ("ORK_METER_TRIM", "ORK_METER_TEST_FILTER"),
    "haiku_agents": (),
    "cache_nudge": ("ORK_METER_NUDGES",),
    "loop_guard": ("ORK_METER_LOOP_GUARD",),
    "audit": (),
    "context_hygiene": (),
    "stale_guard": (),
    "delta": ("ORK_METER_TRIM",),
}, features.OPT_OUTS
switches = sorted(set(n for names in features.OPT_OUTS.values() for n in names))
for n in range(len(switches) + 1):
    for combo in itertools.combinations(switches, n):
        env = dict((s, "off") for s in combo)
        for lever, names in features.OPT_OUTS.items():
            # Opt-in levers are off without their switch, whatever the opt-outs say.
            want = lever not in ("haiku_agents", "context_hygiene", "stale_guard", "delta") and not any(s in combo for s in names)
            assert features.enabled(env, lever) is want, (combo, lever)
        assert features.enabled_features(env) == sorted(l for l, names in features.OPT_OUTS.items() if l not in ("haiku_agents", "context_hygiene", "stale_guard", "delta") and not any(s in combo for s in names))
for off in ("off", "0", "false", "no", "OFF", " No ", "False"):
    assert not features.enabled({"ORK_METER_TRIM": off}, "trim"), off
for on in ("", "on", "1", "true", "yes", "nope", "disabled"):
    assert features.enabled({"ORK_METER_TRIM": on}, "trim"), on
assert features.enabled({"ORK_METER_TRIM": "off"}, "no_such_lever"), "unknown levers have no switch"
# The opt-in lever: only an ON_VALUES spelling turns it on; reduce mode only.
for on in ("on", "1", "true", "yes", "ON", " Yes "):
    assert features.enabled({"ORK_METER_CONTEXT_HYGIENE": on}, "context_hygiene"), on
for off in ("", "off", "0", "nope", "enabled"):
    assert not features.enabled({"ORK_METER_CONTEXT_HYGIENE": off}, "context_hygiene"), off
for mode in ("shadow", "off", "junk"):
    assert not features.enabled({"ORK_METER_CONTEXT_HYGIENE": "on", "ORK_METER_MODE": mode}, "context_hygiene"), mode
assert features.policy_settings({"ORK_METER_CONTEXT_HYGIENE": "on"})["ORK_METER_CONTEXT_HYGIENE"] == "on"
assert features.policy_settings({})["ORK_METER_CONTEXT_HYGIENE"] == "off"
pinned = features.policy_settings({"ORK_METER_CONTEXT_HYGIENE": "on"})
assert features.apply_policy_settings({"ORK_METER_CONTEXT_HYGIENE": "off"}, pinned)["ORK_METER_CONTEXT_HYGIENE"] == "off", "turning it off applies at once"
assert features.apply_policy_settings({}, pinned)["ORK_METER_CONTEXT_HYGIENE"] == "on", "unset keeps the pin, which may hold the per-token default (#110)"
assert features.apply_policy_settings({"ORK_METER_CONTEXT_HYGIENE": "on"}, features.policy_settings({}))["ORK_METER_CONTEXT_HYGIENE"] == "off", "turning it on waits for the next session"
for on in ("on", "1", "true", "yes"):
    assert features.enabled({"ORK_METER_STALE_GUARD": on}, "stale_guard"), on
for off in ("", "off", "0", "nope"):
    assert not features.enabled({"ORK_METER_STALE_GUARD": off}, "stale_guard"), off
assert features.apply_policy_settings({}, features.policy_settings({"ORK_METER_STALE_GUARD": "on"}))["ORK_METER_STALE_GUARD"] == "off", "turning it off applies at once"
' \
  "status_line: defaults, per-tool overrides, read-guard override, read guard off, trim off" '
import features
assert features.status_line({}) == "Ork Meter trim: on (Bash 12K, Read 24K, Grep 8K, Glob 4K, WebFetch 16K; read guard 96K)."
line = features.status_line({"ORK_METER_TRIM_BUDGET_BASH": "32k", "ORK_METER_TRIM_BUDGET_GREP": "16384", "ORK_METER_READ_GUARD_BYTES": "256KiB"})
assert line.startswith("Ork Meter trim: on (Bash 32K, Read 24K, Grep 16K, Glob 4K, WebFetch 16K; read guard 256K)."), line
assert "read guard 4K" in features.status_line({"ORK_METER_READ_GUARD_BYTES": "1"}), "floored value shown"
assert "read guard off" in features.status_line({"ORK_METER_READ_GUARD": "off"})
assert features.status_line({"ORK_METER_TRIM": "off", "ORK_METER_TRIM_BUDGET_BASH": "32K"}) == "Ork Meter trim: off (ORK_METER_TRIM=off)."
' \
  "plugin_version: this plugin, CLAUDE_PLUGIN_ROOT, explicit root, and the 0.0.0 fallback" '
import json, os, re, features
VERSION_RE = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-[A-Za-z0-9.]{1,16})?$")
here = json.load(open(".claude-plugin/plugin.json"))["version"]
assert features.plugin_version({}) == here and VERSION_RE.match(here)
copy = os.environ["PLUGIN_COPY"]
assert features.plugin_version({"CLAUDE_PLUGIN_ROOT": copy}) == "9.8.7-rc.1"
assert features.plugin_version({}, plugin_root=copy) == "9.8.7-rc.1"
assert features.plugin_version({"CLAUDE_PLUGIN_ROOT": copy}, plugin_root=".") == here, "an explicit root wins"
missing = os.path.join(copy, "missing")
assert features.plugin_version({"CLAUDE_PLUGIN_ROOT": missing}) == "0.0.0" == features.FALLBACK_VERSION
open(os.path.join(copy, ".claude-plugin", "plugin.json"), "w").write("{not json")
assert features.plugin_version({"CLAUDE_PLUGIN_ROOT": copy}) == "0.0.0"
open(os.path.join(copy, ".claude-plugin", "plugin.json"), "w").write("[1]")
assert features.plugin_version({"CLAUDE_PLUGIN_ROOT": copy}) == "0.0.0"
open(os.path.join(copy, ".claude-plugin", "plugin.json"), "w").write("{\"version\": \"\"}")
assert features.plugin_version({"CLAUDE_PLUGIN_ROOT": copy}) == "0.0.0"
open(os.path.join(copy, ".claude-plugin", "plugin.json"), "w").write("{\"version\": 3}")
assert features.plugin_version({"CLAUDE_PLUGIN_ROOT": copy}) == "0.0.0"
assert VERSION_RE.match(features.FALLBACK_VERSION), "the fallback must pass the reader"
doc = json.load(open(features.write_features_file({"CLAUDE_PLUGIN_ROOT": missing, "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]})))
assert doc["plugin_version"] == "0.0.0", doc
' \
  "global features file: levers on in every live session, whichever session wrote last (#131)" '
import json, os, tempfile, time
import features
env = {"XDG_STATE_HOME": tempfile.mkdtemp()}
read = lambda: json.load(open(features.features_path(env)))["features"]
features.write_features_file(env, session_id="a")
assert "trim" in read() and "dedupe" in read()
features.write_features_file(dict(env, ORK_METER_TRIM="off"), session_id="b")
assert "trim" not in read() and "loop_guard" in read(), read()
features.write_features_file(env, session_id="a")   # a refreshes last: b still has trim off
assert "trim" not in read() and "dedupe" not in read(), read()
assert "trim" in json.load(open(features.features_path(env, "a")))["features"], "the session file keeps its own levers"
features.write_features_file(dict(env, ORK_METER_TRIM="off"), session_id="b", ended=True)
assert "trim" in read(), "an ended session no longer counts"
features.write_features_file(env, session_id="a", ended=True)
assert "trim" in read(), "no live session: the last writer own levers"
features.write_features_file(dict(env, ORK_METER_TRIM="off"), session_id="b")
assert "trim" not in read(), "a resumed session counts again"
old = time.time() - features.LIVE_SECONDS - 60
os.utime(features.features_path(env, "b"), (old, old))
features.write_features_file(env, session_id="a")
assert "trim" in read(), "an idle session drops out"
gone = time.time() - features.PRUNE_SECONDS - 60
os.utime(features.features_path(env, "b"), (gone, gone))
features.write_features_file(env, session_id="a")
assert not os.path.exists(features.features_path(env, "b")), "files the reader would reject are pruned"
assert sorted(json.load(open(features.features_path(env)))) == ["features", "plugin_version", "updated_at"]
' \
  "docs table matches LEVERS: every env name, default and floor, and nothing extra" '
import re, features
text = open("docs/token-optimization.md").read()
section = text.split("## Defaults and why", 1)[1].split("\n## ", 1)[0]
rows = [l for l in section.splitlines() if l.startswith("| ") and "ORK_METER_" in l]
in_doc = set(re.findall(r"ORK_METER_[A-Z_]+", section))
in_code = set(n for s in features.OPT_OUTS.values() for n in s) | set(features.OPT_INS.values()) | set(t[0] for s in features.LEVERS.values() for t in s["tunables"].values())
assert in_doc == in_code, (in_doc - in_code, in_code - in_doc)
for lever, spec in features.LEVERS.items():
    for key, t in spec["tunables"].items():
        env_name, default, floor = t.env, t.default, t.floor
        row = [r for r in rows if ("`%s`" % env_name) in r]
        assert len(row) == 1, (env_name, row)
        want_default = "unset" if default is None else str(default)
        assert re.search(r"\|\s*%s" % re.escape(want_default), row[0]), (env_name, "default", row[0])
        assert re.search(r"\|\s*%d\b" % floor, row[0]), (env_name, "floor", row[0])
        assert t.ceiling is None or re.search(r"\|\s*%d\b.*ceiling %d\b" % (floor, t.ceiling), row[0]), (env_name, "ceiling", row[0])
assert "ORK_METER_" + "" in text and "status line is static" not in text
'

# No file outside features.py reads an ORK_METER_* variable (messages may still name one).
if python3 - <<'EOF'
import os, re
reads = re.compile(r"(?:env|environ|os\.environ|getenv)\s*(?:\.get\s*\(|\[|\()\s*[\"']ORK_METER_"   # Python reads
                   r"|\$\{?ORK_METER_"                                                            # shell reads
                   r"|ORK_METER_[A-Z_]*\s*%")                                                     # formatted env names
offenders = []
for d in ("hooks", "hooks/lib", "scripts"):
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if not os.path.isfile(p) or f == "features.py" or not (f.endswith(".py") or f.endswith(".sh")):
            continue
        src = open(p, encoding="utf-8").read()
        for m in reads.finditer(src):
            offenders.append((p, src[:m.start()].count("\n") + 1, m.group(0)))
assert not offenders, offenders
EOF
then echo "  ok   no ORK_METER_* read outside hooks/lib/features.py"; else echo "  FAIL ORK_METER_* read outside features.py"; fails=$((fails + 1)); fi

[ "$fails" -eq 0 ]
