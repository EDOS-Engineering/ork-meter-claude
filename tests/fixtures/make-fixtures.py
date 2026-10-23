"""Regenerates the synthetic hook payloads in this directory.

Run: python3 tests/fixtures/make-fixtures.py
Every payload is synthetic (no real output, paths or IDs). Placeholders the
tests replace at run time: __SCRATCH__ (scratchpad_dir), __FILE__ (a file
path), __TRANSCRIPT__ (a transcript path).
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
COMMON = {
    "session_id": "00000000-0000-0000-0000-000000000000",
    "prompt_id": "11111111-1111-1111-1111-111111111111",
    "transcript_path": "__TRANSCRIPT__",
    "cwd": "/tmp/project",
    "scratchpad_dir": "__SCRATCH__",
    "permission_mode": "default",
}


def write(name, payload):
    with open(os.path.join(HERE, name), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.write("\n")


def post(tool, tool_input, response, tool_use_id="toolu_fixture_%s"):
    p = dict(COMMON)
    p.update({"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": tool_input,
              "tool_response": response, "tool_use_id": tool_use_id % tool.lower(), "duration_ms": 12})
    return p


def pre(tool, tool_input):
    p = dict(COMMON)
    p.update({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
              "tool_use_id": "toolu_fixture_pre_%s" % tool.lower()})
    return p


def bash(stdout, stderr="", command="make build"):
    return post("Bash", {"command": command, "description": "fixture"},
                {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False})


# Bash, under budget: untouched.
write("posttooluse.bash.small.json", bash("\n".join("line %d ok" % i for i in range(40))))

# Bash, over 12 KiB with everything the trimmer handles: ANSI colour, a run
# of identical lines, a 3,000-char line, and error lines deep in the middle.
lines = []
for i in range(30):
    lines.append("\x1b[32m[build]\x1b[0m compiling module_%03d.go    " % i)
lines.append("x" * 3000)                       # capped at 2,000 chars, kept in the head
lines.extend(["warning: deprecated API"] * 6)  # collapsed to one line + count
for i in range(30, 300):
    lines.append("[build] compiling module_%03d.go" % i)
lines.append("ERROR: module_299 failed: undefined symbol frob")   # deep in the elided middle
lines.append("panic: runtime error: index out of range")
for i in range(300, 560):
    lines.append("[build] packaging module_%03d" % i)
lines.append("Process exited with exit code 2")
write("posttooluse.bash.large.json", bash("\n".join(lines)))

# Bash output Claude Code already persisted, shaped as Claude Code 2.1.308
# sends it (captured 2026-10-23, contents replaced): a `git diff` over the
# host's 30,000-character inline limit. The hook gets the first 30,000
# characters, cut mid-line, plus persistedOutputPath (the saved file under
# <projects>/<project>/<session_id>/tool-results/) and persistedOutputSize
# (the full output's bytes); the model gets only the host's 2 KB
# <persisted-output> preview of that file, never the hook's reply (#100, #350).
diff = ["diff --git a/pkg/stats.py b/pkg/stats.py", "index abf5e09..3dcd0e5 100644",
        "--- a/pkg/stats.py", "+++ b/pkg/stats.py"]
for i in range(700):
    diff.append("@@ -%d,3 +%d,4 @@ def quantile_%03d(data):" % (i * 9 + 1, i * 10 + 1, i) if i % 25 == 0
                else "+    value_%04d = sorted(data)[%d]  # fixture padding" % (i, i))
diff = "\n".join(diff) + "\n"
persisted = bash(diff[:30000], command="git diff")
persisted["tool_input"]["description"] = "Show full uncommitted diff"
persisted["tool_use_id"] = "toolu_fixture_bash_persisted"
persisted["effort"] = "high"
persisted["tool_response"].update(
    noOutputExpected=False,
    persistedOutputPath="/srv/fixture/.claude/projects/-tmp-project/00000000-0000-0000-0000-000000000000/tool-results/b8kq2xw1n.txt",
    persistedOutputSize=len(diff.encode("utf-8")))
write("posttooluse.bash.persisted.json", persisted)

# Bash test runner, go test -v with 200 passing tests and one failure.
go = []
for i in range(200):
    go.append("=== RUN   TestThing%03d" % i)
    go.append("--- PASS: TestThing%03d (0.00s)" % i)
go.append("=== RUN   TestBroken")
go.append("    broken_test.go:12: expected 4, got 5")
go.append("--- FAIL: TestBroken (0.00s)")
go.append("FAIL")
go.append("FAIL\texample.com/pkg\t0.412s")
go.append("ok  \texample.com/other\t0.020s")
write("posttooluse.bash.gotest.json", bash("\n".join(go), command="go test ./... -v"))

# Same output, but the command isn't a test runner: no filtering.
write("posttooluse.bash.gotest-notest.json", bash("\n".join(go), command="cat build.log"))

# Read, 10 KiB: untouched.
small = "\n".join("def fn_%d():\n    return %d" % (i, i) for i in range(400))
write("posttooluse.read.small.json", post("Read", {"file_path": "/tmp/project/small.py"},
      {"type": "text", "file": {"filePath": "/tmp/project/small.py", "content": small,
                                "numLines": small.count("\n") + 1, "startLine": 1,
                                "totalLines": small.count("\n") + 1}}))

# Read, 40 KiB with an error line near the end.
big_lines = ["# line %d of a long generated file with some padding text to fill bytes" % i for i in range(600)]
big_lines.append("raise RuntimeError('config error: missing key')")
big_lines.extend("# trailing line %d" % i for i in range(20))
big = "\n".join(big_lines)
write("posttooluse.read.large.json", post("Read", {"file_path": "/tmp/project/big.py"},
      {"type": "text", "file": {"filePath": "/tmp/project/big.py", "content": big,
                                "numLines": len(big_lines), "startLine": 1, "totalLines": len(big_lines)}}))

# Read that Claude Code itself already answered as unchanged (native dedupe).
write("posttooluse.read.native-unchanged.json", post("Read", {"file_path": "/tmp/project/small.py"},
      {"type": "file_unchanged", "file": {"filePath": "/tmp/project/small.py"}}))

# A second, changed version of small.py for the dedupe test.
changed = small + "\n# changed"
write("posttooluse.read.small-changed.json", post("Read", {"file_path": "/tmp/project/small.py"},
      {"type": "text", "file": {"filePath": "/tmp/project/small.py", "content": changed,
                                "numLines": changed.count("\n") + 1, "startLine": 1,
                                "totalLines": changed.count("\n") + 1}}))

# Grep content mode, 20 KiB.
grep_lines = ["/tmp/project/src/file_%03d.go:%d:    log.Printf(\"handled request %d\")" % (i % 50, i, i)
              for i in range(300)]
write("posttooluse.grep.large.json", post("Grep", {"pattern": "handled", "output_mode": "content"},
      {"mode": "content", "numFiles": 50, "filenames": [], "content": "\n".join(grep_lines),
       "numLines": len(grep_lines)}))

# Glob, 120 long paths (over 4 KiB).
paths = ["/tmp/project/packages/service-%02d/internal/handlers/generated/handler_%03d.go" % (i % 7, i)
         for i in range(120)]
write("posttooluse.glob.large.json", post("Glob", {"pattern": "**/*.go"},
      {"durationMs": 5, "numFiles": len(paths), "filenames": paths, "truncated": False}))

# WebFetch, 20 KiB result.
web = "\n".join("Paragraph %d of the fetched page, summarised by the small model." % i for i in range(300))
write("posttooluse.webfetch.large.json", post("WebFetch", {"url": "https://example.com", "prompt": "summarise"},
      {"bytes": 123456, "code": 200, "codeText": "OK", "result": web, "durationMs": 900,
       "url": "https://example.com"}))

# PreToolUse Read fixtures (the test substitutes __FILE__).
write("pretooluse.read.plain.json", pre("Read", {"file_path": "__FILE__"}))
write("pretooluse.read.offset.json", pre("Read", {"file_path": "__FILE__", "offset": 10, "limit": 50}))
write("pretooluse.read.allow-large.json", pre("Read", {"file_path": "__FILE__", "allow_large": True}))

# Session events.
for source in ("startup", "resume", "clear", "compact"):
    p = dict(COMMON)
    p.update({"hook_event_name": "SessionStart", "source": source})
    write("sessionstart.%s.json" % source, p)
p = dict(COMMON)
p.update({"hook_event_name": "SessionStart", "source": "resume", "model": "claude-opus-5",
          "seconds_since_last_response": 5400, "context_tokens": 182340,
          "prompt_cache_likely_expired": True, "estimated_cache_write_usd": 1.1396})
write("sessionstart.resume-expired.json", p)
p = dict(COMMON)
p.update({"hook_event_name": "SessionStart", "source": "resume", "seconds_since_last_response": 60,
          "context_tokens": 182340, "prompt_cache_likely_expired": False, "estimated_cache_write_usd": 1.1396})
write("sessionstart.resume-fresh.json", p)
p = dict(COMMON)
p.update({"hook_event_name": "SessionStart", "source": "resume", "seconds_since_last_response": 5400,
          "context_tokens": 12000, "prompt_cache_likely_expired": True, "estimated_cache_write_usd": 0.07})
write("sessionstart.resume-expired-small.json", p)
p = dict(COMMON)
p.update({"hook_event_name": "PostCompact", "trigger": "auto", "compact_summary": "synthetic summary"})
write("postcompact.json", p)
p = dict(COMMON)
p.update({"hook_event_name": "SessionEnd", "reason": "other"})
write("sessionend.json", p)

# PostToolUseFailure, identical Bash failure.
p = dict(COMMON)
p.update({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
          "tool_input": {"command": "npm test", "description": "fixture"},
          "tool_use_id": "toolu_fixture_fail", "error": "Exit code 1\nError: Cannot find module 'express'",
          "is_interrupt": False, "duration_ms": 40})
write("posttoolusefailure.bash.json", p)

# SubagentStop for one of our agents (the test substitutes __TRANSCRIPT__).
p = dict(COMMON)
p.update({"hook_event_name": "SubagentStop", "stop_hook_active": False, "agent_id": "agent-fixture",
          "agent_type": "ork-meter:explore", "agent_transcript_path": "__TRANSCRIPT__",
          "last_assistant_message": "Found it at src/x.go:12.", "background_tasks": [], "session_crons": []})
write("subagentstop.explore.json", p)
p = dict(p)
p["agent_type"] = "Explore"
write("subagentstop.other.json", p)

# A synthetic transcript: 3 assistant messages, one split over two lines
# (same message id, same usage), one line without usage, one user line.
usage = lambda i, cr, cc, o: {"input_tokens": i, "cache_read_input_tokens": cr,  # noqa: E731
                              "cache_creation_input_tokens": cc, "output_tokens": o}
transcript = [
    {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hi"}},
    {"type": "assistant", "uuid": "a1", "requestId": "req_1",
     "message": {"id": "msg_1", "role": "assistant", "content": [{"type": "text", "text": "x"}],
                 "usage": usage(10, 1000, 200, 50)}},
    {"type": "assistant", "uuid": "a2", "requestId": "req_1",
     "message": {"id": "msg_1", "role": "assistant", "content": [{"type": "tool_use"}],
                 "usage": usage(10, 1000, 200, 50)}},
    {"type": "assistant", "uuid": "a3", "requestId": "req_2",
     "message": {"id": "msg_2", "role": "assistant", "content": [{"type": "text", "text": "y"}],
                 "usage": usage(5, 2000, 0, 25)}},
    {"type": "assistant", "uuid": "a4", "requestId": "req_3",
     "message": {"id": "msg_3", "role": "assistant", "content": [{"type": "text", "text": "z"}]}},
    {"type": "attachment", "uuid": "t1"},
]
with open(os.path.join(HERE, "transcript.sample.jsonl"), "w", encoding="utf-8") as fh:
    for row in transcript:
        fh.write(json.dumps(row, sort_keys=True) + "\n")

print("fixtures written to", HERE)
