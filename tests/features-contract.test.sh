#!/usr/bin/env bash
# Contract test for the feature file (hooks/lib/features.py): the rules the plugin runtime's
# reader (ork-meter internal/pluginstate, internal/ledger) enforces, listed in
# docs/token-optimization.md under "Active-lever file". A file that breaks one is ignored
# by the reader, silently, so the writer is checked here.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh


export CONTRACT="$tmp/contract.py"
cat > "$CONTRACT" <<'EOF'
import json, os, re, stat, time

FEATURE_RE = re.compile(r"^[a-z_]{2,32}$")
VERSION_RE = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-[A-Za-z0-9.]{1,16})?$")
TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
MAX_BYTES = 4096
MAX_FEATURES = 16

def check(path, now=None):
    """Raise AssertionError naming the first rule the file at ``path`` breaks."""
    st = os.lstat(path)
    assert stat.S_ISREG(st.st_mode), "not a regular file"
    assert st.st_mode & 0o077 == 0, "group/other bits set: %s" % oct(st.st_mode & 0o777)
    assert st.st_size <= MAX_BYTES, "file over %d bytes" % MAX_BYTES
    raw = open(path, "rb").read()
    assert len(raw) <= MAX_BYTES
    doc, end = json.JSONDecoder().raw_decode(raw.decode("utf-8"))
    assert not raw.decode("utf-8")[end:].strip(), "more than one document or trailing content"
    assert isinstance(doc, dict) and sorted(doc) == ["features", "plugin_version", "updated_at"], sorted(doc)
    v = doc["plugin_version"]
    assert isinstance(v, str) and v, "plugin_version not a non-empty string"
    assert VERSION_RE.match(v), "plugin_version %r does not match the reader's version rule" % v
    ts = doc["updated_at"]
    assert isinstance(ts, str) and TS_RE.match(ts), "updated_at %r is not RFC 3339 UTC with Z" % ts
    t = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    now = time.time() if now is None else now
    assert t <= now + 300 and t >= now - 24 * 3600, "updated_at is in the future or older than 24 h"
    f = doc["features"]
    assert isinstance(f, list), "features not a list"
    assert len(f) <= MAX_FEATURES, "more than %d features" % MAX_FEATURES
    assert all(isinstance(x, str) and FEATURE_RE.match(x) for x in f), "feature name outside ^[a-z_]{2,32}$"
    assert f == sorted(set(f)), "features not sorted and deduplicated"
    return doc
EOF

pyexprs \
  "write_features_file: the file passes every reader rule (all levers on)" '
import os, features
exec(open(os.environ["CONTRACT"]).read())
path = features.write_features_file({"XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]})
assert path == os.path.join(os.environ["XDG_STATE_HOME"], "ork-meter", "plugin-features.json"), path
doc = check(path)
assert doc["features"] == ["audit", "cache_nudge", "dedupe", "loop_guard", "read_guard", "test_filter", "trim"], doc
assert oct(os.stat(path).st_mode & 0o777) == "0o600"
assert oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == "0o700"
' \
  "write_features_file: every opt-out combination still passes (including the empty-looking set)" '
import itertools, os, features
exec(open(os.environ["CONTRACT"]).read())
switches = sorted(set(n for names in features.OPT_OUTS.values() for n in names))
for n in range(len(switches) + 1):
    for combo in itertools.combinations(switches, n):
        env = {"XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]}
        env.update((s, "off") for s in combo)
        doc = check(features.write_features_file(env))
        assert set(doc["features"]) <= set(features.ALL_FEATURES)
        assert len(doc["features"]) == sum(1 for f in features.ALL_FEATURES if features.enabled(env, f))
' \
  "ALL_FEATURES itself obeys the name rule and the cap; plugin.json version obeys the version rule" '
import os, re, features
exec(open(os.environ["CONTRACT"]).read())
assert len(features.ALL_FEATURES) <= MAX_FEATURES
assert all(FEATURE_RE.match(f) for f in features.ALL_FEATURES), features.ALL_FEATURES
assert len(set(features.ALL_FEATURES)) == len(features.ALL_FEATURES)
assert VERSION_RE.match(features.plugin_version()), features.plugin_version()
' \
  "a malformed or world-readable existing file is replaced by a conforming one" '
import os, features
exec(open(os.environ["CONTRACT"]).read())
path = features.features_path({"XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]})
open(path, "w").write("{not json, \"extra\": 1")
os.chmod(path, 0o644)
features.write_features_file({"XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]})
check(path)
' \
  "the checker rejects what the reader rejects (self-test of the contract)" '
import json, os, time, features
exec(open(os.environ["CONTRACT"]).read())
path = features.features_path({"XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]})
good = {"plugin_version": "0.2.0", "features": ["dedupe", "trim"], "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
def write(doc, mode=0o600, raw=None):
    open(path, "w").write(json.dumps(doc) + "\n" if raw is None else raw); os.chmod(path, mode)
def rejected(doc=None, **kw):
    write(good if doc is None else doc, **kw)
    try:
        check(path)
    except AssertionError:
        return True
    return False
write(good); check(path)
assert rejected(mode=0o640)
assert rejected(dict(good, extra=1))
assert rejected({k: v for k, v in good.items() if k != "features"})
assert rejected(dict(good, plugin_version=""))
assert rejected(dict(good, plugin_version="unknown")), "the old fallback; features.FALLBACK_VERSION is 0.0.0 now"
write(dict(good, plugin_version=features.FALLBACK_VERSION)); check(path)
assert rejected(dict(good, plugin_version=1))
assert rejected(dict(good, updated_at="2026-10-13T12:00:00+00:00"))
assert rejected(dict(good, updated_at="2026-10-13T12:00:00.000Z"))
assert rejected(dict(good, updated_at="2000-01-01T00:00:00Z"))
assert rejected(dict(good, features=["trim", "dedupe"]))
assert rejected(dict(good, features=["trim", "trim"]))
assert rejected(dict(good, features=["Trim"]))
assert rejected(dict(good, features=["a"]))
assert rejected(dict(good, features=["f%02d" % i for i in range(17)]))
assert rejected(dict(good, features="trim"))
assert rejected(dict(good, features=["trim"] + ["x" * 33]))
assert rejected(raw=json.dumps(good) + "\n" + json.dumps(good) + "\n")
assert rejected(raw=json.dumps(good) + " " * 4096 + "\n")
write(dict(good, plugin_version="0.0.0-" + "a" * 16, features=sorted("%s_%s" % ("a" * 15, chr(97 + i) * 16) for i in range(16)))); check(path)
'

[ "$fails" -eq 0 ]
