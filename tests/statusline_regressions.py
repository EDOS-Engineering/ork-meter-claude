"""scripts/statusline.py: context size, list-price cost per turn and session,
observed cache lifetime, incremental bounded reads, and failing quietly.
All transcripts are synthetic; no Claude Code process is started."""
import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_* when this file runs on its own

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "statusline.py"
sys.path.insert(0, str(ROOT / "scripts"))
import cache_report  # noqa: E402
import statusline  # noqa: E402

CANARY = "PRIVATE_CONTENT_CANARY"


def stamp(seconds_ago):
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return when.isoformat().replace("+00:00", "Z")


def usage(read=0, write=0, fresh=5, out=100, ttl="1h"):
    split = {"ephemeral_1h_input_tokens": write if ttl == "1h" else 0,
             "ephemeral_5m_input_tokens": write if ttl == "5m" else 0}
    return {"input_tokens": fresh, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
            "output_tokens": out, "cache_creation": split}


def assistant(mid, ago, u, model="claude-opus-5-5", side=False):
    return {"type": "assistant", "timestamp": stamp(ago), "isSidechain": side,
            "message": {"id": mid, "model": model, "usage": u,
                        "content": [{"type": "text", "text": CANARY}]}}


def prompt(ago):
    return {"type": "user", "timestamp": stamp(ago), "message": {"role": "user", "content": CANARY}}


def tool_result(ago):
    return {"type": "user", "timestamp": stamp(ago), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": CANARY}]}}


def cost(u, model="claude-opus-5-5"):
    return statusline.request_cost(model, u)[0]


class StatusLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.dir / "state"), HOME=str(self.dir / "home"))
        self.transcript = self.dir / "projects" / "p" / "sess.jsonl"
        self.transcript.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows, path=None, mode="w"):
        path = path or self.transcript
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, mode, encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write((row if isinstance(row, str) else json.dumps(row)) + "\n")

    def run_line(self, payload=None, stdin=None, env=None):
        if stdin is None:
            stdin = json.dumps(payload if payload is not None else
                               {"session_id": "s-1", "transcript_path": str(self.transcript)})
        done = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT)], input=stdin.encode("utf-8"),
                              capture_output=True, env=env or self.env, timeout=30)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stderr, b"")
        self.assertTrue(done.stdout.endswith(b"\n") and not done.stdout.endswith(b"\r\n"), done.stdout)
        return done.stdout.decode("ascii")[:-1]

    def test_turn_session_context_and_cache(self):
        first, second, third = usage(write=20000), usage(read=20000, write=1000), usage(read=21000, write=500)
        self.write([prompt(600), assistant("m1", 590, first), assistant("m1", 589, first),  # streamed copy
                    tool_result(580), assistant("m2", 570, second),
                    prompt(300), assistant("m3", 290, third),
                    {"type": "assistant", "timestamp": stamp(280),
                     "message": {"id": "syn", "model": "<synthetic>", "usage": usage(fresh=999)}}])
        line = self.run_line()
        parts = line.split(" | ")
        self.assertEqual(parts[0], "ctx 21.5k")
        self.assertEqual(parts[1], "turn ~$%.2f" % cost(third))
        self.assertEqual(parts[2], "session ~$%.2f" % (cost(first) + cost(second) + cost(third)))
        self.assertTrue(parts[3].startswith("cache 1h: ") and parts[3].endswith("m left (est.)"), line)
        self.assertNotIn(CANARY, line)
        self.assertNotIn("%", line)

    def test_observed_five_minute_lifetime_expires(self):
        self.write([prompt(700), assistant("m1", 690, usage(write=9000, ttl="5m")),
                    assistant("m2", 400, usage(read=9000, write=0))])   # a read keeps the 5-minute lifetime
        self.assertTrue(self.run_line().endswith("cache 5m: expired (est.)"))

    def test_observed_one_hour_lifetime_still_warm_after_five_minutes(self):
        self.write([prompt(700), assistant("m1", 690, usage(write=9000, ttl="1h"))])
        self.assertIn("cache 1h: 48m left", self.run_line())

    def test_no_lifetime_observed_no_cache_marker(self):
        self.write([prompt(10), assistant("m1", 5, {"input_tokens": 50, "output_tokens": 10})])
        self.assertNotIn("cache", self.run_line())

    def test_payload_ttl_used_only_when_transcript_has_no_split(self):
        self.write([prompt(10), assistant("m1", 5, {"input_tokens": 5, "cache_creation_input_tokens": 9000,
                                                    "output_tokens": 10})])
        line = self.run_line({"session_id": "s", "transcript_path": str(self.transcript),
                              "prompt_cache": {"ttl": "5m"}})
        self.assertIn("cache 5m: 4m left", line)

    def test_subagents_count_toward_turn_and_session(self):
        main, side = usage(write=10000), usage(write=4000)
        self.write([prompt(100), assistant("m1", 90, main)])
        self.write([assistant("a1", 80, side, model="claude-haiku-4-5", side=True)],
                   path=self.transcript.parent / "sess" / "subagents" / "agent-1.jsonl")
        line = self.run_line()
        total = cost(main) + cost(side, "claude-haiku-4-5")
        self.assertIn("turn ~$%.2f | session ~$%.2f" % (total, total), line)
        self.assertIn("ctx 10.0k", line)   # the subagent's context isn't the conversation's

    def test_incremental_runs_and_private_state(self):
        a, b = usage(write=10000), usage(read=10000, write=2000)
        self.write([prompt(100), assistant("m1", 90, a)])
        self.run_line()
        self.write([prompt(50), assistant("m2", 40, b)], mode="a")
        line = self.run_line()
        self.assertIn("turn ~$%.2f | session ~$%.2f" % (cost(b), cost(a) + cost(b)), line)
        files = list((self.dir / "state" / "ork-meter" / "statusline").iterdir())
        self.assertEqual(len(files), 1)
        self.assertNotIn("s-1", files[0].name)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(files[0].parent.stat().st_mode), 0o700)
        self.assertNotIn(CANARY, files[0].read_text(encoding="utf-8"))

    def test_line_being_written_is_read_next_run(self):
        a = usage(write=10000)
        self.write([prompt(100)])
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(assistant("m1", 90, a))[:40])   # no line end yet
        self.assertIn("session ~$0.00", self.run_line())
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(assistant("m1", 90, a))[40:] + "\n")
        self.assertIn("session ~$%.2f" % cost(a), self.run_line())

    def test_long_transcript_tail_first_then_exact(self):
        filler = json.dumps({"type": "attachment", "timestamp": stamp(5000), "pad": "x" * 4000})
        rows, expected = [], 0.0
        for i in range(1200):
            u = usage(read=50000 + i, write=300)
            expected += cost(u)
            rows += [filler, assistant("m%d" % i, 4000 - i, u)]
        rows += [prompt(30), assistant("last", 20, usage(read=60000, write=100))]
        expected += cost(usage(read=60000, write=100))
        self.write(rows)
        self.assertGreater(self.transcript.stat().st_size, statusline.FIRST_TAIL)
        started = time.monotonic()
        first = self.run_line()
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("ctx 60.1k", first)            # live figures come from the tail at once
        self.assertRegex(first, r"session ~\$[0-9.]+\+")   # lower bound while the rest is read
        for _ in range(5):
            line = self.run_line()
        self.assertRegex(line, r"session ~\$%.2f( |$)" % expected)   # caught up: exact, no "+"

    def test_compaction_and_unknown_model(self):
        self.write([prompt(100), assistant("m1", 90, usage(write=30000), model="claude-mystery-9"),
                    {"type": "system", "subtype": "compact_boundary", "timestamp": stamp(80)}])
        line = self.run_line()
        self.assertIn("ctx compacted", line)
        self.assertIn("?", line)
        self.assertNotIn("cache", line)

    def test_haiku_long_prompt_card(self):
        small, large = usage(read=90000, write=0), usage(read=150000, write=0)
        self.assertAlmostEqual(cost(small, "claude-haiku-5-5"), (5 * 0.10 + 90000 * 0.01 + 100 * 0.50) / 1e6)
        self.assertAlmostEqual(cost(large, "claude-haiku-5-5"), (5 * 0.50 + 150000 * 0.05 + 100 * 2.50) / 1e6)

    def test_matches_cache_report_pricing(self):
        u = usage(read=1234567, write=678901, fresh=1111, out=22222, ttl="5m")
        report_path = self.dir / "r.jsonl"
        self.write([assistant("m1", 10, u, model="claude-sonnet-5-5")], path=report_path)
        report = cache_report.analyse([str(report_path)])
        self.assertEqual(round(cost(u, "claude-sonnet-5-5"), 2), report["estimated_cost"])   # report rounds to cents
        self.assertGreater(report["estimated_cost"], 1)

    def test_never_fails_loudly(self):
        for stdin in ("", "not json", "[]", json.dumps({"transcript_path": 5}),
                      json.dumps({"session_id": "x", "transcript_path": str(self.dir / "missing.jsonl")})):
            with self.subTest(stdin=stdin[:30]):
                line = self.run_line(stdin=stdin)
                self.assertTrue(line, "a status line always prints something")
        (self.dir / "blocker").write_text("a file, not a directory", encoding="utf-8")
        self.write([prompt(10), assistant("m1", 5, usage(write=1000))])
        env = dict(self.env, XDG_STATE_HOME=str(self.dir / "blocker"))
        self.assertIn("ctx 1.0k", self.run_line(env=env))   # no state: still renders

    def test_garbage_rows_are_skipped(self):
        self.write(["not json", "[1, 2]", json.dumps({"type": "assistant", "timestamp": "never",
                                                       "message": {"id": "x", "usage": {"input_tokens": 1}}}),
                    prompt(10), assistant("m1", 5, usage(write=1000))])
        self.assertIn("ctx 1.0k", self.run_line())

    def test_snippet(self):
        done = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--snippet"], capture_output=True, timeout=30)
        snippet = json.loads(done.stdout)["statusLine"]
        self.assertEqual(snippet["type"], "command")
        self.assertEqual(snippet["refreshInterval"], 60)
        self.assertTrue(snippet["command"].startswith('python3 -I -B "'))
        self.assertNotIn("\\", snippet["command"])
        self.assertTrue(snippet["command"].endswith('scripts/statusline.py"'))

    def test_snippet_uses_forward_slashes_for_windows_paths(self):
        saved = statusline.HERE
        try:
            statusline.HERE = "C:\\Users\\me\\.claude\\plugins\\cache\\m\\ork-meter\\1\\scripts"
            command = json.loads(statusline.snippet())["statusLine"]["command"]
        finally:
            statusline.HERE = saved
        self.assertEqual(command, 'python3 -I -B "C:/Users/me/.claude/plugins/cache/m/ork-meter/1/scripts/statusline.py"')

    def test_replaced_plugin_version_says_so(self):
        saved = statusline.ROOT
        try:
            statusline.ROOT = str(self.dir)
            (self.dir / ".orphaned_at").write_text("1", encoding="utf-8")
            line = statusline.render(statusline.new_state("t"), time.time())
        finally:
            statusline.ROOT = saved
        self.assertTrue(line.endswith("plugin updated: rerun /ork-meter:statusline"))


if __name__ == "__main__":
    unittest.main()
