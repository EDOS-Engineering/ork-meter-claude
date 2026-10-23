---
disable-model-invocation: true
name: capture
description: Capture a selected noninteractive command before native error rendering can truncate its diagnostics. Preserve original streams and status for exact recovery without rerunning.
argument-hint: "<command argv>"
---

Use for an authorized test/build/log command expected to produce bulky output, before its first execution. Run its argv through the local capture script:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/command.py --session "${CLAUDE_SESSION_ID}" --plugin-data "${CLAUDE_PLUGIN_DATA}" -- PROGRAM ARG1 ARG2
```

Without `--session` the wrapper uses `CLAUDE_CODE_SESSION_ID`, which Claude Code sets for Bash commands, and without `--plugin-data` it uses `CLAUDE_PLUGIN_DATA` when that is set. Quote each argument as shell data. This executes argv directly without evaluating a command string. For an already-authorized pipeline, pass the original shell explicitly. The skill adds no tool allowlist: the invocation goes through the normal Bash permission decision and sandbox. Original-command permission patterns are not re-applied inside the wrapper; use it only for argv already authorized for execution, and never translate a denied command into an allowed wrapper. Interactive/PTY commands should use their original execution path.

The command runs once. Full stdout, stderr and exit status are saved before native rendering. Recognized large text can be reduced with an early `Full output` path; failing commands remain failures. Passing-test lines omitted from a pytest run are counted in a note that says every other line is shown. If the wrapper's output is piped into another command (`| tail -30`, `| grep -c PASSED`), it passes through unreduced so that command sees the real output, and no saving is recorded. Original bytes are available through `/ork-meter:recover` and `scripts/artifact.py --stream stdout|stderr`. `--full` emits the original streams unchanged, subject to the host's own display limits. Off/shadow modes preserve stream output without reduction. Unsupported binary/structured outputs pass through; capture remains local.

For explicit cancellation control, pass `--cancel-file PATH` before `--`. Creating that path cancels the contained command and descendants; a preexisting path prevents execution. Interrupted streams remain exact, the artifact marks interruption and incomplete command execution, and no reduction savings are recorded. POSIX cancellation signals the process group and escalates after 0.5 seconds; Windows terminates its private job. Use the native path for interactive commands, intentional detached/background processes, or commands that escape their process group. Hard termination can prevent final artifact publication; it does not make an interrupted run recoverable.

Never rerun an already-failed command solely to reconstruct missing diagnostics. The capture path cannot recover bytes that were lost before it was used. Automatic rewriting of arbitrary existing Bash calls is not enabled: permission equivalence and lifecycle checks remain unqualified.
