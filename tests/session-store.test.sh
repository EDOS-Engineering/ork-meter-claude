#!/usr/bin/env bash
# Tests for hooks/lib/session.py: the one place that knows the per-session layout.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh


export FBTMP="$tmp/fbtmp"; mkdir -p "$FBTMP"

TMPDIR="$FBTMP" pyexprs \
  "for_payload: scratchpad layout, created 0700, nothing remembered" '
import os, session
scratch = os.environ["SCRATCH"]
st = session.Store.for_payload({"scratchpad_dir": scratch, "session_id": "sid-a", "transcript_path": "/t/a"})
assert st.path == os.path.join(scratch, "ork-meter"), st
assert oct(os.stat(st.path).st_mode & 0o777) == "0o700"
assert not os.path.exists(st.session_path), "for_payload must not remember"
assert st.ledger_path == os.path.join(st.path, "savings.jsonl")
assert st.session_path == os.path.join(st.path, "session.json")
assert st.reads_path == os.path.join(st.path, "reads.json")
assert st.failures_path == os.path.join(st.path, "failures.json")
assert st.summary_path == os.path.join(st.path, "session-summary.json")
assert st.tool_output_path("x") == os.path.join(st.path, "tool-output", "x.txt")
assert oct(os.stat(os.path.join(st.path, "tool-output")).st_mode & 0o777) == "0o700"
' \
  "for_payload: tempdir fallback without scratchpad_dir; odd ids hashed" '
import os, session
fb = os.environ["FBTMP"]
uid = os.getuid()
st = session.Store.for_payload({"session_id": "sid-b"})
assert st.path == os.path.join(fb, "claude-ork-meter-%d" % uid, "sid-b", "ork-meter"), st
assert os.path.isdir(st.path) and oct(os.stat(st.path).st_mode & 0o777) == "0o700"
odd = session.Store.for_payload({"session_id": "../evil/../x y"})
assert os.path.dirname(os.path.dirname(odd.path)) == os.path.join(fb, "claude-ork-meter-%d" % uid)
assert "/" not in os.path.basename(os.path.dirname(odd.path)) and len(os.path.basename(os.path.dirname(odd.path))) == 32
none = session.Store.for_payload({})
assert none.path.endswith(os.path.join("unknown-session", "ork-meter")), none
assert session.Store.for_payload("not a dict").path == none.path
' \
  "find: scratchpad layout (CLAUDE_CODE_TMPDIR) and fallback layout; None otherwise" '
import os, session
fb = os.environ["FBTMP"]; root = os.path.join(os.environ["SCRATCH"], "..", "claude-root"); root = os.path.abspath(root)
sp = os.path.join(root, "claude-%d" % os.getuid(), "-proj", "sess-s", "scratchpad")
os.makedirs(sp)
env = {"CLAUDE_CODE_TMPDIR": root}
st = session.Store.find("sess-s", env)
assert st is not None and st.path == os.path.join(sp, "ork-meter"), st
assert st == session.Store.for_scratchpad(sp)
fbst = session.Store.for_payload({"session_id": "sess-f"})
found = session.Store.find("sess-f", env)
assert found == fbst, (found, fbst)
assert session.Store.find("nope", env) is None
assert session.Store.find("", env) is None and session.Store.find(None, env) is None
assert session.Store.find("*", env) is None, "glob characters in an id are literal"
' \
  "all: only stores with a ledger, both layouts, sorted by path; roots= limits to those roots" '
import os, session
session.SYSTEM_TEMP_BASES = ()  # hermetic: live Claude Code sessions keep real stores under /tmp
fb = os.environ["FBTMP"]; root = os.path.abspath(os.path.join(os.environ["SCRATCH"], "..", "claude-root"))
env = {"CLAUDE_CODE_TMPDIR": root}
base = os.path.join(root, "claude-%d" % os.getuid(), "-proj")
with_ledger = []
for name in ("z-sess", "a-sess", "m-sess"):
    st = session.Store.for_payload({"scratchpad_dir": os.path.join(base, name, "scratchpad")})
    if name != "m-sess":
        open(st.ledger_path, "w").write("{}\n"); with_ledger.append(st.path)
fbst = session.Store.for_payload({"session_id": "sess-f"})
open(fbst.ledger_path, "w").write("{}\n"); with_ledger.append(fbst.path)
got = [s.path for s in session.Store.all(env)]
# Existing native sessions under the system temp root are not this fixture.
owned = [p for p in got if p.startswith(root + os.sep) or p.startswith(fb + os.sep)]
assert owned == sorted(with_ledger), (owned, sorted(with_ledger))
only = [s.path for s in session.Store.all(env, roots=[os.path.dirname(base)])]
assert only == sorted(p for p in with_ledger if p.startswith(base)), only
assert session.Store.all({"CLAUDE_CODE_TMPDIR": os.path.join(root, "empty")}, roots=[os.path.join(root, "empty")]) == []
' \
  "all: stores under the system temp bases are found, and only there" '
import os, session
system = os.path.join(os.environ["FBTMP"], "system-tmp")
sp = os.path.join(system, "claude-%d" % os.getuid(), "-proj", "live", "scratchpad")
st = session.Store.for_payload({"scratchpad_dir": sp})
open(st.ledger_path, "w").write("{}\n")
session.SYSTEM_TEMP_BASES = (system,)
assert st in session.Store.all({}), "a store under a system temp base is part of all"
session.SYSTEM_TEMP_BASES = ()
assert st not in session.Store.all({}), "replacing the bases keeps a test away from the real /tmp"
own = os.path.realpath(os.environ["FBTMP"]) + os.sep
roots = session.scratchpad_roots({}) + session.fallback_roots({})
assert roots and all(os.path.realpath(r).startswith(own) for r in roots), roots
' \
  "roots: two spellings of one directory (a symlink; /tmp and /private/tmp on macOS) are searched once" '
import os, session
fb = os.environ["FBTMP"]
real = os.path.join(fb, "real"); link = os.path.join(fb, "link"); os.makedirs(real); os.symlink(real, link)
for env in ({"CLAUDE_CODE_TMPDIR": link}, {"CLAUDE_CODE_TMPDIR": real}, {}):
    for roots in (session.scratchpad_roots(env), session.fallback_roots(env)):
        assert len(set(os.path.realpath(r) for r in roots)) == len(roots), (env, roots)
roots = session.scratchpad_roots({"CLAUDE_CODE_TMPDIR": link})
assert roots[0] == os.path.join(link, "claude-%d" % os.getuid()), "the first spelling is kept"
assert os.path.join(real, "claude-%d" % os.getuid()) not in roots, roots
# A session under the real directory, with TMPDIR naming the link: counted once by Store.all.
base = os.path.join(real, "claude-%d" % os.getuid(), "-proj", "once", "scratchpad")
st = session.Store.for_payload({"scratchpad_dir": base})
open(st.ledger_path, "w").write("{}\n")
env = {"CLAUDE_CODE_TMPDIR": real, "TMPDIR": link}
import tempfile; tempfile.tempdir = link
found = [s.path for s in session.Store.all(env)]
assert sum(1 for p in found if p.endswith(os.path.join("once", "scratchpad", "ork-meter"))) == 1, found
'

pyexprs \
  "write_private: mode 0600, atomic (no temp file left, replaced in one step), read_json/write_json round trip" '
import json, os, threading, session
d = os.path.join(os.environ["SCRATCH"], "wp"); os.makedirs(d)
p = os.path.join(d, "f.json")
session.write_private(p, "first\n")
assert oct(os.stat(p).st_mode & 0o777) == "0o600"
assert open(p).read() == "first\n"
session.write_private(p, "second\n")
assert open(p).read() == "second\n" and os.listdir(d) == ["f.json"], os.listdir(d)
# Atomicity: readers never see a partial file while a writer rewrites it.
big = "x" * 200000 + "\n"
session.write_private(p, big)
seen = set(); stop = []
def reader():
    while not stop:
        seen.add(len(open(p).read()))
t = threading.Thread(target=reader); t.start()
for i in range(50):
    session.write_private(p, big if i % 2 else "y" * 100 + "\n")
stop.append(1); t.join()
assert seen <= {len(big), 101}, seen
session.write_json(p, {"b": 1, "a": [1, 2]})
assert open(p).read() == "{\"a\": [1, 2], \"b\": 1}\n"
assert session.read_json(p) == {"a": [1, 2], "b": 1}
assert session.read_json(os.path.join(d, "missing"), "dflt") == "dflt"
open(p, "w").write("{not json")
assert session.read_json(p) is None and session.read_json(p, {}) == {}
' \
  "remember: idempotent (first payload wins); session_info; reads()/save_reads(); reset() removes reads and failures only" '
import json, os, session
st = session.Store.for_payload({"scratchpad_dir": os.path.join(os.environ["SCRATCH"], "r")})
st.remember({"session_id": "s1", "transcript_path": "/t/1"})
st.remember({"session_id": "s2", "transcript_path": "/t/2"})
assert json.load(open(st.session_path)) == {"session_id": "s1", "transcript_path": "/t/1"}
assert oct(os.stat(st.session_path).st_mode & 0o777) == "0o600"
assert st.session_info() == {"session_id": "s1", "transcript_path": "/t/1"}
st.remember("junk")
assert st.reads() == {}
st.save_reads({"k": {"sha256": "ab", "partial": False}})
assert st.reads() == {"k": {"sha256": "ab", "partial": False}}
assert oct(os.stat(st.reads_path).st_mode & 0o777) == "0o600"
open(st.reads_path, "w").write("[1,2]")
assert st.reads() == {}, "a non-object index reads as empty"
session.write_json(st.failures_path, {"h": {"count": 1}})
session.write_private(st.summary_path, "{}\n")
open(st.ledger_path, "w").write("{}\n")
assert st.has_ledger()
st.reset(); st.reset()
assert not os.path.exists(st.reads_path) and not os.path.exists(st.failures_path)
assert os.path.exists(st.summary_path) and os.path.exists(st.ledger_path) and os.path.exists(st.session_path)
assert st.session_info() == {"session_id": "s1", "transcript_path": "/t/1"}
fresh = session.Store(os.path.join(os.environ["SCRATCH"], "never"))
assert fresh.session_info() == {} and fresh.reads() == {} and not fresh.has_ledger()
' \
  "no file name is spelled outside session.py" '
import os, re
names = ("savings.jsonl", "session.json", "session-policy.json", "reads.json", "failures.json", "session-summary.json", "tool-output", "reducer-health", "disabled.json", "claude-ork-meter-")
offenders = []
for d in ("hooks", "hooks/lib", "scripts"):
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if not f.endswith(".py") or f == "session.py" or not os.path.isfile(p):
            continue
        src = open(p).read()
        for n in names:
            if re.search(r"[\"\x27]" + re.escape(n), src):
                offenders.append((p, n))
assert not offenders, offenders
'

[ "$fails" -eq 0 ]
