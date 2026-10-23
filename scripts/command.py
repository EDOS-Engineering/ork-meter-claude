"""Capture a selected noninteractive command once before native result rendering.

Argv, cwd, environment, stdin and exit status are retained. Commands run in the
calling shell tool's sandbox. This wrapper does not grant permissions, retry,
evaluate shell strings, or support PTYs. Explicitly pass a shell for pipelines.

The short, stable form needs no per-run path in the command:

    bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/command.py -- PROGRAM ARG...

Without ``--session`` or ``--scratchpad`` the session is the one Claude Code
names in ``CLAUDE_CODE_SESSION_ID`` for its Bash commands, and without
``--plugin-data`` the data directory is ``CLAUDE_PLUGIN_DATA`` when set.

Output is reduced only when stdout and stderr are regular files, as Claude
Code's Bash tool gives a command (2.1.302). Into a pipe (``... -- pytest |
tail -30`` or ``| grep -c PASSED``) the original streams pass through
unchanged: reducing them would change what the caller's own filter answers,
and the model sees only that filter's output anyway (benchmark B1, #212). The
full output is still saved; no trim is recorded.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks/lib"))
import artifacts
import circuit
import features
import ledger
import policy
import process_tree
import session
import trimmer
import token_gate

POLICY_VERSION = "command-capture-v1"
FAILURE_BUDGET = 4096


def decode_stream(raw):
    """Lossless JSON representation; unsupported binary stays unreduced."""
    try:
        value = raw.decode("utf-8")
        if "\x00" not in value:
            return value, "utf-8"
    except UnicodeDecodeError:
        pass
    return base64.b64encode(raw).decode("ascii"), "base64"


def reduce_stream(value, budget):
    reduced, _ = trimmer.trim_text(value, budget)
    return reduced


# python, python3, a versioned python3.12 (Homebrew's sys.executable), a
# free-threaded python3.13t, each with an optional .exe; never python3-config.
PYTHON_NAME = re.compile(r"python(?:3(?:\.\d{1,2}t?)?)?(?:\.exe)?")


def is_pytest_argv(argv):
    """Recognize direct pytest argv, never strings embedded in shell/program code."""
    name = Path(argv[0]).name.lower() if argv else ""
    if name in {"pytest", "pytest.exe", "py.test", "py.test.exe"}:
        return True
    if not PYTHON_NAME.fullmatch(name) or "-m" not in argv:
        return False
    index = argv.index("-m")
    return (argv[index + 1:index + 2] == ["pytest"]
            and all(flag in {"-I", "-B", "-u", "-E", "-s"} for flag in argv[1:index]))


def capture(argv, cancel_file=None):
    """One execution, pipe draining, and bounded whole-tree cancellation."""
    if cancel_file and os.path.exists(cancel_file):
        return b"", b"", 130, True, "not-started"
    tree = process_tree.Tree(argv)
    process = tree.process
    interrupted = []
    previous = {}
    deadline = None
    def forward(number, _frame):
        nonlocal deadline
        interrupted.append(number)
        deadline = time.monotonic() + 0.5
        tree.terminate(number)
    for number in (signal.SIGINT, signal.SIGTERM):
        previous[number] = signal.signal(number, forward)
    try:
        while True:
            if cancel_file and not interrupted and os.path.exists(cancel_file):
                forward(signal.SIGINT, None)
            if deadline is not None and time.monotonic() >= deadline:
                tree.terminate(interrupted[-1], force=True)
                deadline = None
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        tree.close(normal=process.returncode is not None and not interrupted)
    return stdout, stderr, process.returncode, bool(interrupted), tree.scope


def to_regular_files():
    """True when stdout and stderr are regular files, as Claude Code's Bash
    tool gives them (2.1.302). A pipe or FIFO means a command of the caller's
    own pipeline reads the output before the model does; a redirect to a file
    is not told apart from the tool's own file."""
    try:
        return all(stat.S_ISREG(os.fstat(fd).st_mode) for fd in (1, 2))
    except (OSError, ValueError):
        return False


def emit(stdout, stderr):
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(stderr)
    sys.stderr.buffer.flush()


def run(argv, store, env, full=False, call_id=None, cancel_file=None):
    env = features.gate(policy.for_store(store, env))
    health = session_id = None
    if store is not None:
        env, health = circuit.environment(store, env)
        session_id = store.session_info().get("session_id")   # written once (Store.remember), read once
    effective_policy_hash = policy.digest(features.plugin_version(env), features.policy_settings(env))
    session_policy_hash = None
    if store is not None:
        try:
            session_policy_hash = policy.read(store, session_id, env)["policy_sha256"]
        except (OSError, ValueError, TypeError):
            pass
    identity = call_id or uuid.uuid4().hex
    def fault(category, error):
        if store is not None:
            try:
                circuit.record(store, "command-capture:Bash", identity, session_policy_hash, category, type(error).__name__,
                               session_id)
            except Exception:
                pass
    raw_stdout, raw_stderr, code, interrupted, cancellation_scope = capture(argv, cancel_file)
    stdout, stdout_encoding = decode_stream(raw_stdout)
    stderr, stderr_encoding = decode_stream(raw_stderr)
    response = {"stdout": stdout, "stderr": stderr, "exitCode": code, "interrupted": interrupted,
                "isImage": False, "stream_encodings": {"stdout": stdout_encoding, "stderr": stderr_encoding},
                "producer": "command-wrapper", "capture_scope": "captured child stdout/stderr before native rendering",
                "cancellation_scope": cancellation_scope,
                "command_sha256": ledger.sha256_hex(json.dumps(argv)), "call_id": call_id,
                "policy_version": POLICY_VERSION}
    published = None
    try:
        text = stdout + ("\n--- stderr ---\n" + stderr if stderr else "")
        published = artifacts.save(store, "Bash", response, text,
                                   completeness={"received_response": "complete", "before_hook": "complete",
                                                 "producer": "command-wrapper", "interrupted": interrupted,
                                                 "command_completed": not interrupted})
    except Exception as exc:
        # Storage failure returns these already-captured streams, never a rerun.
        fault("storage", exc)
    delivered_stdout, delivered_stderr = raw_stdout, raw_stderr
    if (published and not interrupted and not full and to_regular_files()
            and features.mode(env) == "reduce" and features.enabled(env, "trim")
            and stdout_encoding == stderr_encoding == "utf-8"
            and (health is None or not circuit.disabled(health, "command-capture:Bash"))):
        try:
            budget = features.trim_budget(env, "Bash")
            if code != 0:
                budget = min(budget, FAILURE_BUDGET)
            new_stderr = reduce_stream(stderr, max(1024, budget // 4))
            candidate_stdout, omitted = stdout, 0
            if features.enabled(env, "test_filter") and is_pytest_argv(argv):
                candidate_stdout, omitted = trimmer.filter_test_output(stdout)
            new_stdout = reduce_stream(candidate_stdout, max(1024, budget - trimmer.nbytes(new_stderr)))
            if omitted and (new_stdout != candidate_stdout or new_stderr != stderr):
                new_stdout = trimmer.after_trim(new_stdout)
            if new_stdout != stdout or new_stderr != stderr:
                # Put recovery provenance early so native error previews retain it.
                note = "Full output: %s (%d bytes; exit %d)\n" % (published, len(raw_stdout) + len(raw_stderr), code)
                candidate = (note + new_stdout).encode("utf-8")
                if (len(candidate) + trimmer.nbytes(new_stderr) < len(raw_stdout) + len(raw_stderr)
                        and token_gate.permits({"stdout": stdout, "stderr": stderr},
                                               {"stdout": candidate.decode("utf-8"), "stderr": new_stderr})):
                    delivered_stdout, delivered_stderr = candidate, new_stderr.encode("utf-8")
        except Exception as exc:
            fault("reduction", exc)
            delivered_stdout, delivered_stderr = raw_stdout, raw_stderr
    emit(delivered_stdout, delivered_stderr)
    if delivered_stdout != raw_stdout or delivered_stderr != raw_stderr:
        row = ledger.stamp(ledger.trimmed("Bash", len(raw_stdout) + len(raw_stderr),
                                         len(delivered_stdout) + len(delivered_stderr)), session_id)
        row["policy_version"] = POLICY_VERSION
        row["policy_sha256"] = effective_policy_hash
        if health is not None:
            row["reducer_health_sha256"] = health["sha256"]
        try:
            ledger.append(store, row)
        except Exception as exc:
            store.record_gap("command-capture", "Bash", 1, 0, exc)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--session", help="default: $CLAUDE_CODE_SESSION_ID, which Claude Code sets for Bash commands")
    location.add_argument("--scratchpad")
    parser.add_argument("--full", action="store_true", help="emit exact original streams without reduction")
    parser.add_argument("--call-id", help="optional originating tool-call identity")
    parser.add_argument("--cancel-file", help="cancel the command tree when this path exists; never executes if already present")
    parser.add_argument("--plugin-data", help="the plugin data directory holding entitlement.json (Ork Meter Plus)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provide a command argv after --")
    if args.session is None and not args.scratchpad:
        args.session = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not args.session:
            parser.error("provide --session or --scratchpad (CLAUDE_CODE_SESSION_ID is not set)")
    if args.session is not None and session.safe_name(args.session) != args.session:
        # The id becomes a path component; "", "../x" or an absolute path must not.
        parser.error("session id must be a plain name (letters, digits, '-', '_', '.')")
    if args.scratchpad is not None and not args.scratchpad:
        parser.error("--scratchpad must not be empty")
    try:
        if args.scratchpad:
            store = session.Store.for_payload({"scratchpad_dir": args.scratchpad})
        else:
            store = session.Store.find(args.session, os.environ)
            if store is None:
                store = session.Store.for_payload({"session_id": args.session})
            store.remember({"session_id": args.session})
    except (OSError, ValueError):
        store = None
    try:
        env = os.environ
        if args.plugin_data:
            env = dict(os.environ, CLAUDE_PLUGIN_DATA=args.plugin_data)
        code = run(command, store, env, args.full, args.call_id, args.cancel_file)
    except FileNotFoundError:
        print("ork-meter: command executable not found", file=sys.stderr)
        return 127
    except OSError as exc:
        print("ork-meter: command capture failed (%s)" % exc.__class__.__name__, file=sys.stderr)
        return 1
    if code < 0 and os.name != "nt":
        number = -code
        if number not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)
    return code


if __name__ == "__main__":
    sys.exit(main())
