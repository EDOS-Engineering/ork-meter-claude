"""scripts/activation_report.py (#300, Phase 0): aggregates over synthetic
session stores in both layouts, several plugin versions, malformed lines
ignored, and nothing in the output that names a session, path or hash."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import support  # noqa: E402,F401  (puts hooks/lib on sys.path)
import ledger  # noqa: E402
import session  # noqa: E402

SCRIPT = ROOT / "scripts" / "activation_report.py"
POSIX = hasattr(os, "getuid")


def row(kind, before, after, ts, call=None, **extra):
    out = ledger._row("Bash", kind, before, after, extra)
    out["ts"] = ts
    if call:
        out["tool_call_sha256"] = hashlib.sha256(call.encode()).hexdigest()
    return out


class ActivationReport(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="activation-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.secrets = []

    def store(self, layout, sid, version, rows=(), extra_lines=(), summary_ts=None, enabled=None):
        """A session store in ``layout`` (``scratchpad`` or ``fallback``)."""
        uid = os.getuid() if POSIX else 0
        if layout == "scratchpad":
            scratch = os.path.join(self.base, "claude-%d" % uid, "-Users-me-secret-project", sid, "scratchpad")
            path = os.path.join(scratch, session.ORK_DIR)
        else:
            path = os.path.join(self.base, session.FALLBACK_PREFIX + str(uid), sid, session.ORK_DIR)
        for directory in (os.path.dirname(os.path.dirname(path)), os.path.dirname(path), path):
            session.private_dir(directory)
        store = session.Store(path)
        self.secrets += [sid, path, hashlib.sha256(path.encode()).hexdigest(), "/secret/transcript.jsonl",
                         "-Users-me-secret-project"]
        if version:
            session.write_json(store.policy_path, {"schema_version": 1, "session_id": sid, "plugin_version": version,
                                                   "settings": {"ORK_METER_MODE": "auto"}, "policy_sha256": "a" * 64})
        if rows or extra_lines:
            lines = [json.dumps(dict(r, session_id=sid, path_sha256="b" * 64)) for r in rows] + list(extra_lines)
            session.write_private(store.ledger_path, "\n".join(lines) + "\n")
        if summary_ts:
            session.write_json(store.summary_path, {"ts": summary_ts, "session_id": sid,
                                                    "transcript_path": "/secret/transcript.jsonl",
                                                    "features": enabled or ["trim", "not-a-feature"]})
        return store

    def run_report(self, *args):
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--temp-base", self.base] + list(args),
                                capture_output=True, text=True, env=support.clean_environ())
        self.assertEqual(result.returncode, 0, result.stderr)
        for secret in self.secrets:
            self.assertNotIn(secret, result.stdout)
        self.assertNotIn(self.base, result.stdout)
        self.assertIsNone(re.search(r"[0-9a-f]{32,}", result.stdout), "no hash in the output")
        self.assertNotIn("%", result.stdout, "absolute counts only")
        return result.stdout

    def fixture(self):
        self.store("fallback", "s-old-1", "0.2.0", [
            row("trim", 4000, 400, "2026-10-14T10:00:00Z"),
            row("trim", 8000, 400, "2026-10-14T10:01:00Z", call="c1"),
            row("trim", 8000, 400, "2026-10-14T10:01:00Z", call="c1"),   # a duplicate of one tool call
            row("cache_nudge", 0, 0, "2026-10-14T10:02:00Z"),
            row("cache_nudge_accepted", 0, 0, "2026-10-14T10:03:00Z"),
        ], extra_lines=["{not json", json.dumps({"kind": "trim", "bytes_before": -1}), "[1]", ""],
            summary_ts="2026-10-14T11:00:00Z")
        self.store("fallback", "s-old-2", "0.2.0", summary_ts="2026-10-15T09:00:00Z")   # no firing
        self.store("scratchpad", "s-new-1", "0.5.0", [
            row("read_guard", 40000, 2000, "2026-10-19T08:00:00Z"),
            row("trim", 2000, 1000, "2026-10-19T08:05:00Z"),
            row("context_nudge", 0, 0, "2026-10-19T08:06:00Z"),
            row("haiku_agent", 0, 0, "2026-10-19T08:07:00Z", agent_type="ork-meter:explore"),
            row("haiku_agent", 0, 0, "2026-10-19T08:08:00Z", agent_type="/secret/agent"),
            row("future_kind", 100, 0, "2026-10-19T08:09:00Z"),
        ], summary_ts="2026-10-19T09:00:00Z", enabled=["read_guard", "trim"])
        self.store("scratchpad", "s-new-2", "0.5.0", [row("trim", 10000, 1000, "2026-10-20T08:00:00Z")])
        self.store("fallback", "s-no-pin", None, [row("dedupe", 800, 100, "2026-10-16T08:00:00Z")])
        self.store("fallback", "s-bad-version", "/secret/path", [row("trim", 900, 100, "2026-10-16T09:00:00Z")])

    def test_aggregates_by_version_and_kind(self):
        self.fixture()
        report = json.loads(self.run_report("--json"))
        self.assertEqual(report["totals"]["sessions_seen"], 6)
        self.assertEqual(report["totals"]["ledger_lines_ignored"], 3)
        self.assertEqual(report["totals"]["duplicate_call_rows"], 1)
        self.assertEqual(report["time_range"], {"first": "2026-10-14T10:00:00Z", "last": "2026-10-20T08:00:00Z"})
        groups = {g["plugin_version"]: g for g in report["groups"]}
        self.assertEqual(list(groups), ["0.2.0", "0.5.0", "unknown"])
        old = groups["0.2.0"]
        self.assertEqual((old["sessions_seen"], old["sessions_with_ledger"], old["sessions_with_any_firing"]), (2, 1, 1))
        self.assertEqual(old["ledger_versions"], {"2/recoverable-v1": 4})
        self.assertEqual(old["kinds"]["trim"]["events"], 2)
        self.assertEqual(old["kinds"]["trim"]["est_tokens_avoided"],
                         {"sum": 900 + 1900, "median_per_firing": 1400, "p90_per_firing": 1900})
        self.assertEqual(old["nudges"]["cache_nudge"], {"shown": 1, "followed": 1,
                                                        "follow_up": "/clear within the window"})
        self.assertNotIn("est_tokens_avoided", old["kinds"]["cache_nudge"])
        self.assertEqual(old["features_enabled_sessions"], {"trim": 2})
        new = groups["0.5.0"]
        self.assertEqual((new["sessions_seen"], new["sessions_with_any_firing"]), (2, 2))
        self.assertEqual(new["kinds"]["trim"]["sessions_with_firing"], 2)
        self.assertEqual(new["kinds"]["trim"]["est_tokens_avoided"]["sum"], 250 + 2250)
        self.assertEqual(new["kinds"]["read_guard"]["est_tokens_avoided"]["sum"], 9500)
        self.assertEqual(new["kinds"]["future_kind"]["counts_as"], "intervention")
        self.assertEqual(new["haiku_agent_runs"], {"ork-meter:explore": 1})
        self.assertEqual(new["nudges"]["context_nudge"]["shown"], 1)
        self.assertEqual(groups["unknown"]["sessions_seen"], 2, "no pin or a pin with an invalid version")
        self.assertTrue(any("skill invocations" in gap for gap in report["not_recorded"]))

    def test_since_and_until(self):
        self.fixture()
        report = json.loads(self.run_report("--json", "--since", "2026-10-19", "--until", "2026-10-19"))
        self.assertEqual(report["totals"]["sessions_seen"], 1)
        group, = report["groups"]
        self.assertEqual(group["plugin_version"], "0.5.0")
        self.assertEqual(group["kinds"]["trim"]["events"], 1)
        self.assertEqual(report["time_range"], {"first": "2026-10-19T08:00:00Z", "last": "2026-10-19T09:00:00Z"})
        report = json.loads(self.run_report("--json", "--since", "2026-10-14T10:00:30Z", "--until", "2026-10-14"))
        group, = report["groups"]
        self.assertEqual(group["kinds"]["trim"]["events"], 1)
        self.assertEqual(report["totals"]["ledger_rows_outside_range"], 1)

    def test_markdown(self):
        self.fixture()
        text = self.run_report()
        self.assertIn("| 0.2.0 | trim | 2 | 1 | 2,800 | 1,400 | 1,900 |", text)
        self.assertIn("| 0.5.0 | 2/recoverable-v1 | 2 | 2 | 2 | 2026-10-19 | 2026-10-20 |", text)
        self.assertIn("| 0.2.0 | cache_nudge | 1 | 1 | /clear within the window |", text)
        self.assertIn("Not recorded: skill invocations", text)

    def test_empty(self):
        text = self.run_report()
        self.assertIn("0 sessions seen", text)

    @unittest.skipUnless(POSIX, "owner-only modes are POSIX")
    def test_group_readable_files_and_links_are_not_read(self):
        store = self.store("fallback", "s-open", "0.5.0", [row("trim", 4000, 400, "2026-10-14T10:00:00Z")])
        os.chmod(store.ledger_path, 0o644)
        linked = self.store("fallback", "s-link", "0.5.0")
        target = os.path.join(self.base, "elsewhere.jsonl")
        session.write_private(target, json.dumps(row("trim", 4000, 400, "2026-10-14T10:00:00Z")) + "\n")
        os.symlink(target, linked.ledger_path)
        loose = self.store("fallback", "s-loose", "0.5.0", [row("trim", 4000, 400, "2026-10-14T10:00:00Z")])
        os.chmod(loose.path, 0o755)
        report = json.loads(self.run_report("--json"))
        self.assertEqual(report["totals"]["sessions_seen"], 2)
        self.assertEqual(report["groups"][0]["sessions_with_any_firing"], 0)
        self.assertEqual(report["not_read"], {"ledger_not_private": 2, "store_not_private": 1})

    def test_bad_dates(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--temp-base", self.base,
                                 "--since", "yesterday"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
