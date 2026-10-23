"""Installed-CLI session lifecycle qualification (#205): compaction, resume,
fork, continue, background Bash results and interrupted tool calls.

Each case drives the real ``claude`` CLI against the localhost fixture in
tests/native_host.py and asserts on three things: what the model receives
(the fixture keeps every request body), the plugin's ledger (rows are tied to
tool calls by ``tool_call_sha256``) and the session store (repeat-read index,
delta bases and the pinned session policy). Concurrent calls are several
tool blocks in one fixture answer.
"""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import host_retry  # noqa: E402
from native_cache_qualification import history  # noqa: E402
from native_host import (Host, assert_hooks_fail_open, call_sha, compact_boundaries, hook_names,  # noqa: E402
                         result_text, stream_session_ids, text_after, tool_id, tool_results, wait_for)
sys.path.insert(0, str(ROOT / "hooks/lib"))
import ledger  # noqa: E402
import session  # noqa: E402

DEDUPE_NOTE = "is unchanged since your earlier Read"
DELTA_NOTE = "Ork Meter delta:"
TRIM_NOTE = "Ork Meter trimmed this Read"


def lines(tag, count=120):
    return "".join("%s_line_%04d stable context for lifecycle qualification\n" % (tag, i) for i in range(count))


def change(path, tag):
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(r"(_line_0060)( [A-Z0-9-]+)? ", r"\1 %s " % tag, text, count=1), encoding="utf-8")


def touch(path):
    """Same bytes, later mtime: Claude Code reads the file again instead of
    answering with its own "file unchanged" stub, so the plugin sees it."""
    stat = path.stat()
    os.utime(path, (stat.st_atime + 5, stat.st_mtime + 5))


def snapshot(store):
    if store is None:
        return {"reads": None, "bases": [], "policy": None}
    reads = session.read_json(store.reads_path, None)
    bases = sorted(path.name for path in Path(store.path, session.DELTA_DIR).glob("*")) if Path(store.path).is_dir() else []
    policy = Path(store.policy_path).read_bytes() if os.path.isfile(store.policy_path) else None
    return {"reads": reads, "bases": bases, "policy": policy}


def rows_by_call(store):
    return [(row["kind"], row["tool"], row.get("tool_call_sha256")) for row in ledger.load(store)]


def log_trim_session_runs(host):
    """Make the host's copy of hooks/trim-session.py log each time its
    handler runs (event and source), whatever hooks.json event started it.
    Returns a function that reads the log."""
    log = host.base / "trim-session-runs.jsonl"
    with open(host.plugin / "hooks/trim-session.py", "a", encoding="utf-8") as script:
        script.write("\n\n_qualified_handle = handle\n\n\ndef handle(ctx):\n"
                     "    with open(%r, 'a', encoding='utf-8') as log:\n"
                     "        log.write(json.dumps([ctx.event, ctx.payload.get('source')]) + '\\n')\n"
                     "    return _qualified_handle(ctx)\n\n\nimport json  # noqa: E402\n" % str(log))
    return lambda: [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.is_file() else []


def compact_results(stream):
    """The text of the CLI's ``/compact`` command result, as replayed into the
    stream (a ``<local-command-stdout>`` user message)."""
    found = []
    for line in stream.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        content = item.get("message", {}).get("content") if isinstance(item, dict) and item.get("type") == "user" else None
        match = re.fullmatch(r"<local-command-stdout>(Compacted.*)</local-command-stdout>", content, re.S) \
            if isinstance(content, str) else None
        if match:
            found.append(match.group(1))
    return found


def stream_turns(test, host, prompts, env, **command):
    """Run one stream-json session, one user message per prompt, each sent
    once the previous turn's result arrived. Returns the stream."""
    process = subprocess.Popen(host.command(stream_input=True, **command), cwd=host.work, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8")
    out = []
    reader = threading.Thread(target=lambda: out.extend(process.stdout), daemon=True)
    reader.start()
    try:
        for count, prompt in enumerate(prompts, 1):
            process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            process.stdin.flush()
            test.assertTrue(wait_for(lambda: sum('"type":"result"' in line.replace(" ", "") for line in out) >= count,
                                     timeout=90), prompt)
        process.stdin.close()
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        reader.join(timeout=10)
        errors = process.stderr.read()
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
    stream = "".join(out)
    test.assertEqual(process.returncode, 0, stream[-2000:] + errors[-500:])
    return stream


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
class NativeLifecycle(unittest.TestCase):
    def assert_hooks_fail_open(self, stream):
        assert_hooks_fail_open(self, stream)

    def assert_history_stable(self, requests):
        """Every tool result keeps exactly the content first delivered for
        it, in every later request where it still appears."""
        first = {}
        for number, body in enumerate(requests, 1):
            for identifier, block in tool_results(body).items():
                content = json.dumps(block.get("content"), sort_keys=True)
                self.assertEqual(first.setdefault(identifier, content), content,
                                 "tool result %s changed by request %d" % (identifier, number))

    def assert_pinned(self, store, rows):
        pin = json.loads(Path(store.policy_path).read_text(encoding="utf-8"))
        self.assertTrue(rows)
        self.assertTrue(all(row["policy_sha256"] == pin["policy_sha256"] for row in rows))
        return pin

    # --- compaction ---------------------------------------------------------

    def test_auto_compaction_mid_prompt_resets_reads_index_and_delta_bases(self):
        with Host(self, "ork-native-compact-", "Read") as host:
            a, b, c = (host.work / name for name in ("a.txt", "b.txt", "c.txt"))
            a.write_text(lines("a"), encoding="utf-8")
            b.write_text(lines("b"), encoding="utf-8")
            c.write_text("".join("c_line_%04d large trimmed evidence padding padding\n" % i for i in range(900)),
                         encoding="utf-8")
            states, compactions, pending = {}, [], set()

            def reads(number, *paths):
                pending.clear()
                pending.update(tool_id(number, index) for index in range(len(paths)))
                return [("tool", "Read", {"file_path": str(path)}) for path in paths]

            def script(body, number):
                # Structural, not a keyword: every ordinary request carries the
                # results of the previous answer's tool calls. The compaction
                # request is the one that does not (the CLI keeps that last
                # exchange as the preserved segment); compact_boundary in the
                # stream confirms it afterwards.
                if pending and not pending & set(tool_results(body)):
                    states["before"] = snapshot(next(iter(host.stores().values()), None))
                    compactions.append(number)
                    return [("text", "<summary>Fixture summary of the earlier reads.</summary>")]
                if number == 1:
                    return reads(number, a, b, c)
                if number == 2:
                    touch(a)
                    change(b, "ONE")
                    return reads(number, a, b)
                if compactions and "after" not in states:
                    states["after"] = snapshot(next(iter(host.stores().values()), None))
                    states["after_number"] = number
                    touch(a)
                    change(b, "TWO")
                    return reads(number, a, b)
                pending.clear()
                return [("text", "QUALIFIED")]
            host.script = script
            runs = log_trim_session_runs(host)
            # A context near the model window after the second answer makes
            # Claude Code compact automatically, inside the same prompt.
            host.usage = lambda number: 990000 if number == 2 else 17
            completed = host.run("Read the fixture files.", env=host.env(ORK_METER_DELTA="on"))
            host.assert_no_external_traffic()
            self.assert_hooks_fail_open(completed.stdout)
            self.assertIn("SessionStart:compact", hook_names(completed.stdout))
            self.assertEqual([boundary.get("trigger") for boundary in compact_boundaries(completed.stdout)], ["auto"])
            # Automatic compaction fires SessionStart:compact too, so the
            # state resets exactly once for it (#268).
            self.assertEqual(runs(), [["SessionStart", "startup"], ["SessionStart", "compact"]])
            self.assertEqual(len(compactions), 1, "exactly one automatic compaction")
            self.assertGreater(compactions[0], 2, "the compaction follows the second answer")
            stores = host.stores()
            self.assertEqual(len(stores), 1)
            store = next(iter(stores.values()))

            # Before compaction: the plugin's own controls fired in this prompt.
            before, after = states["before"], states["after"]
            self.assertEqual(sorted(before["reads"]), sorted(str(path) for path in (a, b, c)))
            self.assertTrue(before["bases"], "a delivered whole file leaves a delta base")
            # The compaction preserved the second answer's exchange: its
            # results first reach the model in the first request after it.
            second = host.requests[states["after_number"] - 1]
            self.assertIn(DEDUPE_NOTE, text_after(second, tool_id(2, 0)))
            self.assertIn(DELTA_NOTE, text_after(second, tool_id(2, 1)))
            # After compaction, before any later tool call: both are gone and the pin is the same file.
            self.assertIsNone(after["reads"], "compaction must reset the repeat-read index")
            self.assertEqual(after["bases"], [], "compaction must drop delta bases")
            self.assertEqual(after["policy"], before["policy"])
            number = states["after_number"]
            final = tool_results(host.requests[number])
            for identifier, tag, needle in ((tool_id(number, 0), "a", "a_line_0119"), (tool_id(number, 1), "b", "TWO")):
                visible = result_text(final[identifier])
                self.assertIn(needle, visible, tag)
                self.assertIn(tag + "_line_0119", visible, tag + " must be delivered whole")
                after_text = text_after(host.requests[number], identifier)
                self.assertNotIn(DEDUPE_NOTE, after_text)
                self.assertNotIn(DELTA_NOTE, after_text)
            # The compaction request carried every earlier result as delivered.
            self.assertTrue({tool_id(1, 0), tool_id(1, 1), tool_id(1, 2)} <= set(tool_results(host.requests[compactions[0] - 1])))
            self.assert_history_stable(host.requests)
            rows = ledger.load(store)
            self.assertEqual(sorted(rows_by_call(store)), sorted([
                ("trim", "Read", call_sha(tool_id(1, 2))),
                ("dedupe", "Read", call_sha(tool_id(2, 0))),
                ("delta", "Read", call_sha(tool_id(2, 1)))]))
            self.assert_pinned(store, rows)

    def test_manual_compact_turn_resets_state_and_restores_no_trimmed_content(self):
        with Host(self, "ork-native-manual-compact-", "Read") as host:
            a, b, c = (host.work / name for name in ("a.txt", "b.txt", "c.txt"))
            a.write_text(lines("a"), encoding="utf-8")
            b.write_text(lines("b"), encoding="utf-8")
            c.write_text("".join("c_line_%04d large trimmed evidence padding padding\n" % i for i in range(900)),
                         encoding="utf-8")
            marker = host.base / "post-compact.txt"
            settings = json.loads(host.settings.read_text())
            settings["hooks"] = {"PostCompact": [{"hooks": [{"type": "command", "command": "echo fired >> '%s'"
                                                             % str(marker).replace("\\", "/")}]}]}
            host.settings.write_text(json.dumps(settings))
            states = {"phase": "first"}

            def script(body, number):
                phase = states["phase"]
                if phase == "compacting":
                    states.setdefault("compaction_requests", []).append(number)
                    return [("text", "<summary>Fixture summary of the earlier reads.</summary>")]
                if phase == "first" and number == 1:
                    return [("tool", "Read", {"file_path": str(path)}) for path in (a, b, c)]
                if phase == "third" and "after_number" not in states:
                    states["after"] = snapshot(next(iter(host.stores().values()), None))
                    states["after_number"] = number
                    touch(a)
                    change(b, "TWO")
                    return [("tool", "Read", {"file_path": str(a)}), ("tool", "Read", {"file_path": str(b)})]
                return [("text", "QUALIFIED")]
            host.script = script
            process = subprocess.Popen(host.command(stream_input=True), cwd=host.work, env=host.env(ORK_METER_DELTA="on"),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8")
            out = []
            reader = threading.Thread(target=lambda: out.extend(process.stdout), daemon=True)
            reader.start()

            def turn(text, count):
                process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
                process.stdin.flush()
                self.assertTrue(wait_for(lambda: sum('"type":"result"' in line.replace(" ", "") for line in out) >= count,
                                         timeout=90), text)
            try:
                turn("Read the fixture files.", 1)
                states["before"] = snapshot(next(iter(host.stores().values()), None))
                states["phase"] = "compacting"
                turn("/compact", 2)
                states["phase"] = "third"
                turn("Read them again.", 3)
                process.stdin.close()
                process.wait(timeout=60)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                reader.join(timeout=10)
                errors = process.stderr.read()
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()
            stream = "".join(out)
            self.assertEqual(process.returncode, 0, stream[-2000:] + errors[-500:])
            host.assert_no_external_traffic()
            self.assert_hooks_fail_open(stream)
            self.assertIn("SessionStart:compact", hook_names(stream))
            self.assertEqual([boundary.get("trigger") for boundary in compact_boundaries(stream)], ["manual"])
            self.assertTrue(marker.is_file(), "PostCompact must fire for a /compact turn")
            self.assertEqual(len(states.get("compaction_requests", [])), 1)
            before, after = states["before"], states["after"]
            self.assertEqual(sorted(before["reads"]), sorted(str(path) for path in (a, b, c)))
            self.assertTrue(before["bases"])
            self.assertIsNone(after["reads"], "/compact must reset the repeat-read index")
            self.assertEqual(after["bases"], [], "/compact must drop delta bases")
            self.assertEqual(after["policy"], before["policy"])
            number = states["after_number"]
            # The CLI re-attaches recently read files after /compact; the
            # lines the plugin trimmed from the large Read do not come back.
            self.assertNotIn("c_line_0899", json.dumps(host.requests[number - 1]))
            final = tool_results(host.requests[number])
            for index, tag in ((0, "a"), (1, "b")):
                identifier = tool_id(number, index)
                self.assertIn(tag + "_line_0119", result_text(final[identifier]))
                self.assertNotIn(DELTA_NOTE, text_after(host.requests[number], identifier))
                self.assertNotIn(DEDUPE_NOTE, text_after(host.requests[number], identifier))
            store = next(iter(host.stores().values()))
            rows = ledger.load(store)
            self.assertEqual(rows_by_call(store), [("trim", "Read", call_sha(tool_id(1, 2)))])
            self.assert_pinned(store, rows)
            self.assert_history_stable(host.requests)

    def test_manual_compact_runs_trim_session_once_and_prints_the_stock_result(self):
        """#268: trim-session.py was on SessionStart (compact) and on
        PostCompact, so a /compact reset the store twice, and the CLI appended
        the PostCompact hook's command text to the /compact result. Now it
        runs once per compaction and the result reads as stock's."""
        results = {}
        for plugin in (False, True):
            with Host(self, "ork-native-compact-once-", "Read") as host:
                # Request 2 is the /compact turn's summarization request.
                host.script = lambda body, number: [("text", "<summary>Fixture summary.</summary>" if number == 2
                                                     else "QUALIFIED")]
                runs = log_trim_session_runs(host)
                env = host.env()
                stream = stream_turns(self, host, ["Say hello.", "/compact", "Say hello again."], env, plugin=plugin)
                host.assert_no_external_traffic()
                self.assert_hooks_fail_open(stream)
                self.assertEqual(len(host.requests), 3)
                self.assertEqual([boundary.get("trigger") for boundary in compact_boundaries(stream)], ["manual"])
                results[plugin] = compact_results(stream)
                if plugin:
                    self.assertIn("SessionStart:compact", hook_names(stream))
                    self.assertEqual(runs(), [["SessionStart", "startup"], ["SessionStart", "compact"]])
                    for number, body in enumerate(host.requests, 1):
                        self.assertNotIn("completed successfully", json.dumps(body), "request %d" % number)
        self.assertEqual(results[False], ["Compacted "], "stock /compact result")
        self.assertEqual(results[True], results[False], "the plugin adds nothing to the /compact result")

    # --- resume, fork and continue -------------------------------------------

    def test_resume_fork_and_continue_reset_state_keep_pins_and_history(self):
        with Host(self, "ork-native-resume-", "Read") as host:
            a, b = host.work / "a.txt", host.work / "b.txt"
            a.write_text(lines("a"), encoding="utf-8")
            b.write_text(lines("b"), encoding="utf-8")
            plan, seen = {}, {}

            def script(body, number):
                step = plan.pop(number, None)
                return step(number) if callable(step) else step or [("text", "QUALIFIED")]
            host.script = script
            original = str(uuid.uuid4())
            env = host.env(ORK_METER_DELTA="on")

            # Turn 1: concurrent reads, then a delta for the changed file (control).
            def reread(_number):
                change(b, "ONE")
                return [("tool", "Read", {"file_path": str(b)})]
            plan.update({1: [("tool", "Read", {"file_path": str(a)}), ("tool", "Read", {"file_path": str(b)})], 2: reread})
            first = host.run("Turn one.", "--session-id", original, env=env, persist=True)
            self.assertEqual(stream_session_ids(first.stdout), [original])
            store = host.stores()[original]
            pin = Path(store.policy_path).read_bytes()
            self.assertEqual(sorted(session.read_json(store.reads_path, {})), sorted([str(a), str(b)]))
            self.assertIn(DELTA_NOTE, text_after(host.requests[2], tool_id(2, 0)))
            self.assertEqual(rows_by_call(store), [("delta", "Read", call_sha(tool_id(2, 0)))])
            turn_one = len(host.requests)

            # Turn 2: resume with a smaller Read budget in the environment.
            # The pin keeps 24 KiB, so neither 6 KiB file is trimmed; the
            # index and bases were reset, so the changed file is not a delta.
            changed = {"ORK_METER_TRIM_BUDGET_READ": "1024"}
            def resumed(number):
                seen["resume"] = snapshot(host.stores().get(original))
                touch(a)
                change(b, "TWO")
                return [("tool", "Read", {"file_path": str(a)}), ("tool", "Read", {"file_path": str(b)})]
            plan[turn_one + 1] = resumed
            second = host.run("Turn two.", "--resume", original, env=dict(env, **changed), persist=True)
            self.assertEqual(stream_session_ids(second.stdout), [original])
            self.assertIn("SessionStart:resume", hook_names(second.stdout))
            self.assertIsNone(seen["resume"]["reads"], "resume must reset the repeat-read index")
            self.assertEqual(seen["resume"]["bases"], [])
            self.assertEqual(seen["resume"]["policy"], pin)
            self.assertEqual(Path(store.policy_path).read_bytes(), pin, "resume keeps the session's pin")
            results = tool_results(host.requests[turn_one + 1])
            for identifier, tag in ((tool_id(turn_one + 1, 0), "a"), (tool_id(turn_one + 1, 1), "b")):
                visible = result_text(results[identifier])
                self.assertIn(tag + "_line_0119", visible)
                self.assertNotIn("Ork Meter", text_after(host.requests[turn_one + 1], identifier))
            self.assertEqual(rows_by_call(store), [("delta", "Read", call_sha(tool_id(2, 0)))])
            self.assertEqual(history(host.requests[turn_one]["messages"][:len(host.requests[turn_one - 1]["messages"])]),
                             history(host.requests[turn_one - 1]["messages"]),
                             "resume must replay earlier content unchanged")
            turn_two = len(host.requests)

            # Turn 3: fork. A new session identity gets its own store and a
            # fresh pin from the current environment (1 KiB Read budget); the
            # original session's pin, index and ledger are untouched.
            def forked(number):
                seen["fork"] = {sid: snapshot(found) for sid, found in host.stores().items()}
                return [("tool", "Read", {"file_path": str(a)}), ("tool", "Read", {"file_path": str(b)})]
            plan[turn_two + 1] = forked
            third = host.run("Turn three.", "--resume", original, "--fork-session", env=dict(env, **changed), persist=True)
            fork_ids = stream_session_ids(third.stdout)
            self.assertEqual(len(fork_ids), 1)
            fork = fork_ids[0]
            self.assertNotEqual(fork, original)
            # 2.1.302 reports source "fork"; the plugin's matcher also covers "resume".
            self.assertTrue({"SessionStart:fork", "SessionStart:resume"} & set(hook_names(third.stdout)))
            self.assertIn(fork, seen["fork"], "the fork's SessionStart creates its own store")
            self.assertIsNone(seen["fork"][fork]["reads"])
            self.assertEqual(seen["fork"][fork]["bases"], [])
            self.assertEqual(seen["fork"][original]["policy"], pin)
            self.assertEqual(Path(store.policy_path).read_bytes(), pin)
            fork_store = host.stores()[fork]
            fork_pin = json.loads(Path(fork_store.policy_path).read_text(encoding="utf-8"))
            self.assertEqual(fork_pin["session_id"], fork)
            self.assertEqual(fork_pin["settings"]["ORK_METER_TRIM_BUDGET_READ"], "1024")
            for index in (0, 1):
                self.assertIn(TRIM_NOTE, text_after(host.requests[turn_two + 1], tool_id(turn_two + 1, index)))
            self.assertEqual(sorted(rows_by_call(fork_store)), sorted(
                [("trim", "Read", call_sha(tool_id(turn_two + 1, index))) for index in (0, 1)]))
            self.assert_pinned(fork_store, ledger.load(fork_store))
            self.assertEqual(rows_by_call(store), [("delta", "Read", call_sha(tool_id(2, 0)))])
            self.assertEqual(history(host.requests[turn_two]["messages"][:len(host.requests[turn_two - 1]["messages"])]),
                             history(host.requests[turn_two - 1]["messages"]),
                             "fork must replay earlier content unchanged")
            turn_three = len(host.requests)

            # Turn 4: --continue picks the most recent conversation here (the fork).
            def continued(number):
                seen["continue"] = {sid: snapshot(found) for sid, found in host.stores().items()}
                return [("tool", "Read", {"file_path": str(a)})]
            plan[turn_three + 1] = continued
            fourth = host.run("Turn four.", "--continue", env=dict(env, **changed), persist=True)
            continued_ids = stream_session_ids(fourth.stdout)
            self.assertEqual(len(continued_ids), 1)
            self.assertIn("SessionStart:resume", hook_names(fourth.stdout))
            self.assertEqual(history(host.requests[turn_three]["messages"][:len(host.requests[turn_three - 1]["messages"])]),
                             history(host.requests[turn_three - 1]["messages"]),
                             "continue must replay the fork's content unchanged")
            current = continued_ids[0]
            self.assertIsNone(seen["continue"][current]["reads"], "continue must reset the repeat-read index")
            self.assertEqual(Path(store.policy_path).read_bytes(), pin)
            for sid, found in host.stores().items():
                self.assertEqual(json.loads(Path(found.policy_path).read_text(encoding="utf-8"))["session_id"], sid)
            print("Lifecycle session ids: original=%s fork=%s continue=%s (%s)" % (
                original, fork, current, "fork" if current == fork else "original" if current == original else "new"))
            for completed in (first, second, third, fourth):
                self.assert_hooks_fail_open(completed.stdout)
            host.assert_no_external_traffic()
            self.assert_history_stable(host.requests)

    # --- background and interrupted results ---------------------------------

    def test_background_bash_launch_passes_through_and_output_read_is_credited_to_read(self):
        with Host(self, "ork-native-background-", "Bash,Read") as host:
            program = host.work / "background.py"
            program.write_text("import sys, time\n"
                               "for i in range(900):\n"
                               "    print('bg_line_%04d background evidence padding padding' % i)\n"
                               "    if i == 450: print('ERROR: background-middle diagnostic')\n"
                               "sys.stdout.flush()\ntime.sleep(1)\nprint('bg_done')\n", encoding="utf-8")
            command = shlex.quote(sys.executable.replace("\\", "/")) + " background.py"
            outputs = []

            def script(body, number):
                if number == 1:
                    return [("tool", "Bash", {"command": command, "run_in_background": True,
                                              "description": "Run the background fixture"})]
                text = json.dumps(body["messages"][-1])
                found = re.search(r"<output-file>(.*?)</output-file>", text)
                if found and not outputs:
                    outputs.extend([json.loads('"%s"' % found.group(1)), number])
                    return [("tool", "Read", {"file_path": outputs[0]})]
                return [("text", "QUALIFIED")]
            host.script = script
            completed = host.run("Run the fixture in the background.", timeout=120)
            host.assert_no_external_traffic()
            self.assert_hooks_fail_open(completed.stdout)
            self.assertIn("PostToolUse:Bash", hook_names(completed.stdout))
            launch = result_text(tool_results(host.requests[1])[tool_id(1, 0)])
            self.assertIn("background", launch.lower())
            self.assertNotIn("Ork Meter", text_after(host.requests[1], tool_id(1, 0)))
            self.assertTrue(outputs, "the completion notification must name the output file")
            identifier = tool_id(outputs[1], 0)
            delivered = text_after(host.requests[outputs[1]], identifier)
            self.assertIn(TRIM_NOTE, delivered)
            self.assertIn("ERROR: background-middle diagnostic", delivered)
            self.assertIn("bg_done", Path(outputs[0]).read_text(encoding="utf-8"))
            stores = list(host.stores().values())
            self.assertEqual(len(stores), 1)
            self.assertEqual(rows_by_call(stores[0]), [("trim", "Read", call_sha(identifier))],
                             "only the Read of the output file may be credited")
            self.assert_history_stable(host.requests)

    def test_interrupted_tool_call_fails_open_and_is_not_credited(self):
        with Host(self, "ork-native-interrupt-", "Bash,Read") as host:
            big = host.work / "big.txt"
            big.write_text("".join("big_line_%04d read evidence padding padding\n" % i for i in range(900)), encoding="utf-8")
            (host.work / "slow.py").write_text(
                "import sys, time\nfrom pathlib import Path\n"
                "for i in range(600): print('slow_line_%04d partial evidence' % i)\n"
                "sys.stdout.flush()\nPath('started').touch()\ntime.sleep(60)\nPath('finished').touch()\n", encoding="utf-8")
            command = shlex.quote(sys.executable.replace("\\", "/")) + " slow.py"

            def script(body, number):
                if number == 1:
                    return [("tool", "Read", {"file_path": str(big)}),
                            ("tool", "Bash", {"command": command, "description": "Run the slow fixture"})]
                return [("text", "QUALIFIED")]
            host.script = script
            process = subprocess.Popen(host.command(stream_input=True), cwd=host.work, env=host.env(),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8")
            out = []
            reader = threading.Thread(target=lambda: out.extend(process.stdout), daemon=True)
            reader.start()

            def send(message):
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()

            def results():
                return sum('"type":"result"' in line.replace(" ", "") for line in out)
            try:
                send({"type": "user", "message": {"role": "user", "content": "Run the slow fixture."}})
                # Interrupt only once the command has observably started; the
                # command then sleeps far longer than any wait here, so a slow
                # runner cannot let it finish first.
                self.assertTrue(wait_for(lambda: (host.work / "started").exists(), timeout=90), "the command must start")
                send({"type": "control_request", "request_id": "qualify-interrupt", "request": {"subtype": "interrupt"}})
                self.assertTrue(wait_for(lambda: results() >= 1, timeout=50), "the interrupted turn must end")
                finished_early = (host.work / "finished").exists()
                send({"type": "user", "message": {"role": "user", "content": "Continue after the interrupt."}})
                self.assertTrue(wait_for(lambda: results() >= 2, timeout=90), "the next turn must run")
                process.stdin.close()
                process.wait(timeout=60)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                reader.join(timeout=10)
                errors = process.stderr.read()
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()
            stream = "".join(out)
            self.assertEqual(process.returncode, 0, stream[-2000:] + errors[-500:])
            host.assert_no_external_traffic()
            self.assert_hooks_fail_open(stream)
            self.assertIn('"request_id":"qualify-interrupt"', stream.replace(" ", ""))
            self.assertFalse(finished_early, "the turn must end before the command finishes")
            self.assertEqual(len(host.requests), 2)
            delivered = tool_results(host.requests[1])
            self.assertIn(TRIM_NOTE, text_after(host.requests[1], tool_id(1, 0)))
            self.assertTrue(delivered[tool_id(1, 1)].get("is_error"))
            interrupted = result_text(delivered[tool_id(1, 1)])
            self.assertNotIn("Ork Meter", interrupted)
            self.assertNotIn("Full output:", interrupted)
            # Observed on 2.1.280, 2.1.302 and 2.1.303 (all three CI platforms):
            # an interrupted call gets neither PostToolUse nor PostToolUseFailure.
            names = hook_names(stream)
            self.assertNotIn("PostToolUse:Bash", names)
            self.assertFalse([name for name in names if name.startswith("PostToolUseFailure")], names)
            stores = list(host.stores().values())
            self.assertEqual(len(stores), 1)
            self.assertEqual(rows_by_call(stores[0]), [("trim", "Read", call_sha(tool_id(1, 0)))],
                             "nothing may be credited to the interrupted call")
            saved = {path.name.split(".")[0] for path in Path(stores[0].path, session.TOOL_OUTPUT_DIR).glob("*")}
            self.assertEqual(len(saved), 1, "only the trimmed Read's snapshot may be saved")
            self.assertIn(next(iter(saved)), text_after(host.requests[1], tool_id(1, 0)))
            failures = session.read_json(stores[0].failures_path, {})
            self.assertEqual(failures, {}, "an interrupt is not a failure for the loop guard")
            self.assert_history_stable(host.requests)


if __name__ == "__main__":
    unittest.main()
