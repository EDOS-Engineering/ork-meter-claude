#!/usr/bin/env bash
# Tests for hooks/lib/ledger.py: one constructor per kind and its exact row, the KINDS table
# (complete against the hooks, matched by docs/token-optimization.md), and the one summary that
# session-summary.py and scripts/savings.py (text and --json) all render.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

PYEXPR_LINES=40   # a row mismatch prints both rows in full

pyexprs \
  "constructors: the exact row of every kind (keys, order, values)" '
import json, ledger
def same(row, want):
    assert json.dumps(row) == json.dumps(want), (json.dumps(row), json.dumps(want))
common = lambda tool, kind, b, a, tb, ta: {"tool": tool, "kind": kind, "reducer_version": "2", "policy_version": "recoverable-v1", "bytes_before": b, "bytes_after": a,
                                           "est_tokens_before": tb, "est_tokens_after": ta, "est_tokens_avoided": max(0, tb - ta)}
same(ledger.trimmed("Bash", 21725, 12005), common("Bash", "trim", 21725, 12005, 5432, 3002))
same(ledger.trimmed("Read", 5, 0), common("Read", "trim", 5, 0, 2, 0))
same(ledger.test_filtered("Bash", 4000, 401), common("Bash", "test_filter", 4000, 401, 1000, 101))
same(ledger.deduped(10240, 900, "ab" * 32), dict(common("Read", "dedupe", 10240, 900, 2560, 225), path_sha256="ab" * 32))
same(ledger.read_guarded(200000, 19000, "cd" * 32), dict(common("Read", "read_guard", 200000, 19000, 50000, 4750), path_sha256="cd" * 32))
same(ledger.recovered("Bash", 600), common("Bash", "recovery", 0, 600, 0, 150))
same(ledger.loop("Bash", 3, stopped=False), dict(common("Bash", "loop_warning", 0, 0, 0, 0), failures=3))
same(ledger.loop("Grep", 5, stopped=True), dict(common("Grep", "loop_stop", 0, 0, 0, 0), failures=5))
same(ledger.agent("ork-meter:explore", None), dict(common("Agent", "haiku_agent", 0, 0, 0, 0), agent_type="ork-meter:explore", measured_status="unavailable"))
same(ledger.agent("ork-meter:logs", {"status": "unavailable", "reason": "x"}),
     dict(common("Agent", "haiku_agent", 0, 0, 0, 0), agent_type="ork-meter:logs", measured_status="unavailable"))
ok = {"status": "ok", "input_tokens": 15, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 200, "output_tokens": 75,
      "assistant_messages": 2, "prompt_tokens": 3215, "note": "n", "source": "transcript"}
same(ledger.agent("ork-meter:explore", ok), dict(common("Agent", "haiku_agent", 0, 0, 0, 0), agent_type="ork-meter:explore",
     measured_status="ok", measured_input_tokens=15, measured_cache_read_input_tokens=3000,
     measured_cache_creation_input_tokens=200, measured_output_tokens=75, measured_assistant_messages=2))
same(ledger.agent("ork-meter:test-runner", {"status": "partial", "input_tokens": 1}), dict(common("Agent", "haiku_agent", 0, 0, 0, 0),
     agent_type="ork-meter:test-runner", measured_status="partial", measured_input_tokens=1, measured_cache_read_input_tokens=0,
     measured_cache_creation_input_tokens=0, measured_output_tokens=0, measured_assistant_messages=0))
same(ledger.cache_nudge(182340, 1.1396), dict(common("SessionStart", "cache_nudge", 0, 0, 0, 0), context_tokens=182340, est_cache_write_usd=1.1396))
same(ledger.cache_nudge(40000.0, None), dict(common("SessionStart", "cache_nudge", 0, 0, 0, 0), context_tokens=40000))
same(ledger.context_nudge(130000.0), dict(common("UserPromptSubmit", "context_nudge", 0, 0, 0, 0), context_tokens=130000))
same(ledger.policy_fallback("ValueError"), dict(common("Policy", "policy_fallback", 0, 0, 0, 0), reason="ValueError"))
same(ledger.stale_guard(262000.0, 730.9), dict(common("UserPromptSubmit", "stale_guard", 0, 0, 0, 0), context_tokens=262000, idle_minutes=730))
same(ledger.stale_guard_continued(262000, 731), dict(common("UserPromptSubmit", "stale_guard_continued", 0, 0, 0, 0), context_tokens=262000, idle_minutes=731))
same(ledger.context_nudge_accepted(130000, "s-1", "compact"), dict(common("SessionStart", "context_nudge_accepted", 0, 0, 0, 0),
     context_tokens=130000, nudged_session_id="s-1", action="compact"))
same(ledger.cache_nudge(40000, 2), dict(common("SessionStart", "cache_nudge", 0, 0, 0, 0), context_tokens=40000, est_cache_write_usd=2.0))
same(ledger.cache_nudge_accepted(182340, "s-1", 1.1396), dict(common("SessionStart", "cache_nudge_accepted", 0, 0, 0, 0),
     context_tokens=182340, nudged_session_id="s-1", est_cache_write_usd_avoided=1.1396))
same(ledger.cache_nudge_accepted(0, None, None), dict(common("SessionStart", "cache_nudge_accepted", 0, 0, 0, 0),
     context_tokens=0, nudged_session_id=None))
' \
  "stamp: ts and session_id first, then the row unchanged; append writes it sorted, 0600" '
import json, os, re, ledger, session
row = ledger.trimmed("Bash", 1000, 400)
stamped = ledger.stamp(row, "sid-1")
assert list(stamped) == list(ledger.COMMON_FIELDS), list(stamped)
assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", stamped["ts"]) and stamped["session_id"] == "sid-1"
assert {k: v for k, v in stamped.items() if k not in ("ts", "session_id")} == row and "ts" not in row
store = session.Store.for_payload({"scratchpad_dir": os.path.join(os.environ["SCRATCH"], "stamp")})
ledger.append(store, stamped)
assert open(store.ledger_path).read() == json.dumps(stamped, sort_keys=True) + "\n"
assert oct(os.stat(store.ledger_path).st_mode & 0o777) == "0o600"
assert ledger.load(store) == [stamped]
' \
  "KINDS: well formed; every constructor builds exactly its declared kinds and fields" '
import ast, inspect, ledger
assert ledger.ORDER == tuple(ledger.KINDS)
samples = {"trimmed": [("Bash", 9, 1)], "test_filtered": [("Bash", 9, 1)], "deduped": [(9, 1, "h")],
           "recovered": [("Bash", 200)],
           "read_guarded": [(9, 1, "h")], "read_compacted": [(9, 1, "h", 5)], "delta_delivered": [("Bash", 9, 1, 2)], "loop": [("Bash", 3, False), ("Bash", 3, True)],
           "agent": [("ork-meter:explore", {"status": "ok"}), ("ork-meter:explore", None)],
           "cache_nudge": [(40000, 1.0), (40000, None)], "cache_nudge_accepted": [(40000, "s", 1.0), (40000, "s", None)],
           "context_nudge": [(130000,)], "context_nudge_accepted": [(130000, "s", "clear"), (190000, "s", "compact")],
           "policy_fallback": [("ValueError",)],
           "stale_guard": [(262000, 730)], "stale_guard_continued": [(262000, 731.9)]}
assert set(samples) == set(k.constructor for k in ledger.KINDS.values()), "a constructor without a sample here"
built = {}
for name, calls in samples.items():
    fn = getattr(ledger, name)
    for args in calls:
        row = fn(*args)
        k = ledger.KINDS[row["kind"]]
        assert k.constructor == name, (name, row["kind"])
        common_count = len(ledger.COMMON_FIELDS) - 2
        assert list(row)[:common_count] == list(ledger.COMMON_FIELDS[2:]), list(row)
        extra = list(row)[common_count:]
        required = [f for f in k.fields if f not in k.optional]
        assert extra[:len(required)] == required and set(extra) <= set(k.fields), (name, extra, k.fields)
        built.setdefault(row["kind"], set()).update(extra)
        if k.counts == "count":
            assert row["bytes_before"] == row["bytes_after"] == row["est_tokens_avoided"] == 0, row
for kind, k in ledger.KINDS.items():
    assert k.counts in ("bytes", "count"), kind
    assert isinstance(k.label, str) and k.label and k.writers and set(k.optional) <= set(k.fields), kind
    assert built.get(kind) == set(k.fields), (kind, built.get(kind), k.fields)
# Nothing else in ledger.py builds a row: every function that calls _row is a declared constructor,
# and every kind it names is in KINDS.
tree = ast.parse(inspect.getsource(ledger))
for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_row"]
    if not calls:
        continue
    assert fn.name in samples, "undeclared constructor %s" % fn.name
    for c in calls:
        kinds = [n.value for n in ast.walk(c.args[1]) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert kinds and all(k in ledger.KINDS for k in kinds), (fn.name, kinds)
assert not hasattr(ledger, "record"), "ledger.record is gone: rows come from the constructors"
' \
  "hooks: every row comes from a constructor, every kind is written, and KINDS names the right writers" '
import ast, os, re, ledger
constructors = {}
for kind, k in ledger.KINDS.items():
    constructors.setdefault(k.constructor, set()).add(kind)
written = {}
for f in sorted(os.listdir("hooks")) + ["scripts/artifact.py", "scripts/command.py", "hooks/lib/policy.py"]:
    if not f.endswith(".py"):
        continue
    src = open(f if "/" in f else os.path.join("hooks", f)).read()
    # Reading an existing row is valid; constructing or mutating its kind is not.
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            assert not any(isinstance(k, ast.Constant) and k.value == "kind" for k in node.keys), f
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            assert not (isinstance(node.slice, ast.Constant) and node.slice.value == "kind"), f
    for arg in re.findall(r"ctx\.record\(\s*([\w.]+)(\(?)", src):
        name, call = arg
        if call:   # ctx.record(ledger.<constructor>(...))
            assert name.startswith("ledger.") and name[len("ledger."):] in constructors, (f, name)
        else:      # ctx.record(row): a row a constructor built earlier in the same hook
            assert re.search(r"\bledger\.(%s)\(" % "|".join(constructors), src), (f, name)
    for name in re.findall(r"\bledger\.(\w+)\(", src):
        if name in constructors:
            for kind in constructors[name]:
                written.setdefault(kind, set()).add(f)
assert set(written) == set(ledger.KINDS), ("kinds no hook writes", set(ledger.KINDS) - set(written), "undeclared", set(written) - set(ledger.KINDS))
for kind, k in ledger.KINDS.items():
    assert written[kind] == set(k.writers), (kind, written[kind], k.writers)
offenders = []
for d in ("hooks", "hooks/lib", "scripts", "tests"):
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if os.path.isfile(p) and f != "ledger.test.sh" and ("ledger." + "record(") in open(p, errors="replace").read():
            offenders.append(p)
assert not offenders, offenders
' \
  "summarise: by_kind in ORDER then unknown kinds; totals count byte interventions only, events apart" '
import ledger
rows = [ledger.stamp(r, "s") for r in (
    ledger.loop("Bash", 3, stopped=False), ledger.trimmed("Bash", 21725, 12005), ledger.agent("ork-meter:explore", None),
    ledger.read_guarded(200000, 19000, "h"), ledger.trimmed("Grep", 9000, 8000), ledger.cache_nudge(50000, 1.0),
    ledger.deduped(10240, 900, "h"), ledger.test_filtered("Bash", 4000, 400), ledger.loop("Bash", 6, stopped=True),
    ledger.cache_nudge_accepted(50000, "s0", 1.0))]
rows += [{"kind": "zz_future", "bytes_before": 100, "bytes_after": 20, "est_tokens_avoided": 20},
         {"kind": "aa_marker", "bytes_before": 0, "bytes_after": 0},
         {"kind": 7, "bytes_before": "9", "bytes_after": True}]
s = ledger.summarise(rows)
assert list(s) == ["by_kind", "totals", "note"]
assert list(s["by_kind"]) == ["trim", "test_filter", "dedupe", "read_guard", "haiku_agent", "cache_nudge",
                              "cache_nudge_accepted", "loop_warning", "loop_stop", "aa_marker", "unknown", "zz_future"], list(s["by_kind"])
b = s["by_kind"]
assert b["trim"] == {"count": 2, "bytes_before": 30725, "bytes_after": 20005, "bytes_avoided": 10720,
                     "est_tokens_avoided": (5432 - 3002) + (2250 - 2000), "counts_as": "intervention"}, b["trim"]
assert b["haiku_agent"] == {"count": 1, "bytes_before": 0, "bytes_after": 0, "bytes_avoided": 0, "est_tokens_avoided": 0, "counts_as": "event"}
assert b["zz_future"]["counts_as"] == "intervention" and b["aa_marker"]["counts_as"] == "event" and b["unknown"]["counts_as"] == "event"
interventions = [k for k, v in b.items() if v["counts_as"] == "intervention"]
assert interventions == ["trim", "test_filter", "dedupe", "read_guard", "zz_future"], interventions
t = s["totals"]
assert t["interventions"] == 2 + 1 + 1 + 1 + 1 == 6, t
assert t["events"] == 1 + 1 + 1 + 1 + 1 + 1 + 1 == 7, t
for key in ("bytes_before", "bytes_after", "bytes_avoided", "est_tokens_avoided"):
    assert t[key] == sum(b[k][key] for k in interventions), key
assert t["interventions"] + t["events"] == len(rows) == sum(v["count"] for v in b.values())
assert "estimated" in s["note"] and "not a billing figure" in s["note"]
assert ledger.count(s, "loop_stop") == 1 and ledger.count(s, "nope") == 0
assert ledger.label("trim") == "trim" and ledger.label("zz_future") == "zz_future"
empty = ledger.summarise([])
assert empty["by_kind"] == {} and set(empty["totals"].values()) == {0} and empty["note"] == ledger.NOTE
' \
  "stamp: tool_call_sha256 is the SHA-256 of tool_use_id, only for a non-empty string id (#98)" '
import hashlib, ledger
row = ledger.trimmed("Bash", 1000, 400)
s = ledger.stamp(row, "sid", "toolu_01")
assert s["tool_call_sha256"] == hashlib.sha256(b"toolu_01").hexdigest() and "toolu_01" not in s.values()
assert list(s)[:len(ledger.COMMON_FIELDS)] == list(ledger.COMMON_FIELDS)
for missing in (None, "", 7, ["toolu_01"]):
    assert "tool_call_sha256" not in ledger.stamp(row, "sid", missing), missing
assert "tool_call_sha256" not in ledger.stamp(row, "sid")
' \
  "summarise: a byte row repeating a tool call and kind is a duplicate, left out of every total (#98)" '
import ledger
a, b = (ledger.stamp(ledger.trimmed("Bash", 29999, 23754), "s", i) for i in ("toolu_a", "toolu_b"))
plain = ledger.stamp(ledger.trimmed("Bash", 29999, 23754), "s")
filt = ledger.stamp(ledger.test_filtered("Bash", 4000, 400), "s", "toolu_a")
loops = [ledger.stamp(ledger.loop("Bash", 3, stopped=False), "s", "toolu_a")] * 2
s = ledger.summarise([a, b, dict(a), plain, dict(plain), filt, dict(filt)] + loops)
t = s["totals"]
assert s["by_kind"]["trim"]["count"] == 4, s["by_kind"]["trim"]
assert s["by_kind"]["test_filter"]["count"] == 1 and s["by_kind"]["loop_warning"]["count"] == 2
assert t["duplicate_call_rows"] == 2 and t["duplicate_call_bytes"] == (29999 - 23754) + (4000 - 400), t
assert t["bytes_avoided"] == 4 * (29999 - 23754) + 3600 and t["interventions"] == 5, t
same = ledger.summarise([a, b, plain, dict(plain), filt] + loops)
assert {k: v for k, v in same["totals"].items() if not k.startswith("duplicate")} == \
       {k: v for k, v in t.items() if not k.startswith("duplicate")}
'

# Text and --json agree on a mixed ledger (the bug this fixes: the JSON total counted events as interventions).
mixed="$tmp/mixed/scratchpad"
python3 -I - "$mixed" <<'EOF'
import os, sys
sys.path.insert(0, "hooks/lib")
import json, ledger, session
store = session.Store.for_payload({"scratchpad_dir": sys.argv[1]})
session.write_json(store.session_path, {"session_id": "sid-mixed", "transcript_path": None})
ok = {"status": "ok", "input_tokens": 15, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 200, "output_tokens": 75, "assistant_messages": 2}
for row in (ledger.trimmed("Bash", 21725, 12005), ledger.trimmed("Read", 45000, 24000), ledger.test_filtered("Bash", 30000, 900),
            ledger.deduped(10240, 1100, "h"), ledger.read_guarded(200000, 19000, "h"), ledger.agent("ork-meter:explore", ok),
            ledger.agent("ork-meter:logs", None), ledger.cache_nudge(182340, 1.1396), ledger.cache_nudge_accepted(182340, "s0", 1.1396),
            ledger.loop("Bash", 3, stopped=False), ledger.loop("Bash", 6, stopped=False), ledger.loop("Bash", 6, stopped=True)):
    ledger.append(store, ledger.stamp(row, "sid-mixed"))
with open(store.ledger_path, "a") as fh:
    fh.write(json.dumps({"kind": "zz_future", "bytes_before": 100, "bytes_after": 20, "est_tokens_avoided": 20}) + "\n")
EOF
export AGREE="$tmp/agree.py"
cat > "$AGREE" <<'EOF'
import json, os, re
def table(text):
    """{label: [count, before, after, avoided, est]} for the table lines, and the total line."""
    lines = text.splitlines()
    start = lines.index(next(l for l in lines if l.startswith("kind "))) + 2
    out = {}
    for line in lines[start:]:
        m = re.match(r"^(\S+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)$", line)
        if not m:
            break
        out[m.group(1)] = [int(g.replace(",", "")) for g in m.groups()[1:]]
    return out
def agree(text, o):
    rows = table(text)
    t = o["estimated"]["totals"]
    assert rows.pop("total") == [t["interventions"], t["bytes_before"], t["bytes_after"], t["bytes_avoided"], t["est_tokens_avoided"]], (text, t)
    b = o["estimated"]["by_kind"]
    want = {k: [v["count"], v["bytes_before"], v["bytes_after"], v["bytes_avoided"], v["est_tokens_avoided"]]
            for k, v in b.items() if v["counts_as"] == "intervention"}
    assert rows == want, (rows, want)
    assert t["interventions"] == sum(v[0] for v in want.values())
    assert t["events"] == sum(v["count"] for v in b.values() if v["counts_as"] == "event")
    return t
EOF
text=$(python3 scripts/savings.py --scratchpad "$mixed" --no-measure); json=$(python3 scripts/savings.py --scratchpad "$mixed" --no-measure --json)
out=$(TEXT="$text" JSON="$json" python3 -c '
import json, os
exec(open(os.environ["AGREE"]).read())
text, o = os.environ["TEXT"], json.loads(os.environ["JSON"])
t = agree(text, o)
assert (t["interventions"], t["events"]) == (6, 7), t
assert "Haiku agents: %d runs" % o["haiku_agent_runs"] in text and o["haiku_agent_runs"] == 2
assert "Cache nudges: %d shown, %d followed" % (o["cache_nudges"], o["cache_nudges_accepted"]) in text
assert "Loop guard: 2 warnings, 1 stops." in text
assert text.rstrip().endswith(o["heuristic"]) and "estimated" in o["estimated"]["note"]
print("agree")'); status=$?
assert "one session: the text table and total equal --json by_kind and totals (6 interventions, 7 events)" 'out == "agree"'

allroot="$tmp/allroot"; mkdir -p "$allroot/-p/a" "$allroot/-p/b"
cp -R "$mixed" "$allroot/-p/a/scratchpad"; cp -R "$mixed" "$allroot/-p/b/scratchpad"
text=$(python3 scripts/savings.py --all --scratchpad "$allroot/-p/a/scratchpad" --no-measure)
json=$(python3 scripts/savings.py --all --scratchpad "$allroot/-p/a/scratchpad" --no-measure --json)
out=$(TEXT="$text" JSON="$json" python3 -c '
import json, os
exec(open(os.environ["AGREE"]).read())
t = agree(os.environ["TEXT"], json.loads(os.environ["JSON"]))
assert (t["interventions"], t["events"]) == (12, 14), t
print("agree")'); status=$?
assert "--all: the text table and total equal --json by_kind and totals (12 interventions, 14 events)" 'out == "agree"'

# session-summary.py writes the same summary savings.py renders.
sed -e "s#__SCRATCH__#$mixed#g" -e "s#__TRANSCRIPT__#$TRANSCRIPT#g" tests/fixtures/sessionend.json | bash hooks/py.sh session-summary.py >/dev/null
out=$(python3 -c '
import json, sys
summary = json.load(open(sys.argv[1]))["estimated_tokens_avoided"]
print(summary == json.loads(sys.argv[2])["estimated"])' "$mixed/ork-meter/session-summary.json" "$(python3 scripts/savings.py --scratchpad "$mixed" --no-measure --json)"); status=$?
assert "session-summary.json estimated_tokens_avoided is the same summary as savings --json estimated" 'out == "True"'

pyexpr "docs: the kind table in docs/token-optimization.md matches KINDS" '
import ledger
def table():
    lines = ["| Kind | Counts as | Label | Written by | Fields beyond the common ones |", "|---|---|---|---|---|"]
    for name, k in ledger.KINDS.items():
        required = ", ".join("`%s`" % f for f in k.fields if f not in k.optional)
        optional = ", ".join("`%s`" % f for f in k.optional)
        cell = "; ".join(p for p in (required, optional and "when known: " + optional) if p) or "none"
        lines.append("| `%s` | %s | %s | `ledger.%s` in %s | %s |" % (
            name, {"bytes": "byte intervention", "count": "count-only event"}[k.counts], k.label,
            k.constructor, ", ".join("`%s`" % (w if "/" in w else "hooks/" + w) for w in k.writers), cell))
    return lines
doc = open("docs/token-optimization.md").read().splitlines()
start = [i for i, l in enumerate(doc) if l.startswith("| Kind | Counts as |")]
assert len(start) == 1, "the kind table header must appear once"
got = []
for line in doc[start[0]:]:
    if not line.startswith("|"):
        break
    got.append(line)
want = table()
if got != want:
    raise SystemExit("docs/token-optimization.md kind table is out of date; expected:\n" + "\n".join(want))
' "$PYEXPR_LINES"

[ "$fails" -eq 0 ]
