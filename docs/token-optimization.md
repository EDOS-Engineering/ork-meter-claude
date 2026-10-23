# Token optimization in the Ork Meter plugin

This note records what the plugin does to reduce the tokens a Claude Code session spends, exactly how each mechanism works, what is measured and what is only estimated, the paths that were considered and rejected, and the known risks. It is written so a reviewer can check every claim against the code in `hooks/` and `scripts/`, and against the Claude Code hooks reference it relies on. History lives in git and the issues; this file describes the current design.

Everything here runs on the developer's machine. No hook or script opens a network connection. The only files written are under the session's store (see "Session store"), `~/.local/state/ork-meter/` and, for compiled Python bytecode, `$CLAUDE_PLUGIN_DATA/pycache` (see "Hook runtime").

## Why these, and not a savings number

Anthropic's own list of Claude Code cost drivers is: long context that is re-sent every turn, cache misses after idle, model choice, verbose tool output, MCP overhead and thinking. This plugin's position: every lever has a documented mechanism, every number is labelled as an estimate or as a measurement with its source, and the plugin never prints a percentage saved. `/ork-meter:savings` prints the estimate and the measurement side by side, each labelled, and no ratio between them.

## Paths considered

Every bundled skill uses `disable-model-invocation: true`, so its description
stays out of ordinary model prompts and its instructions load only on an
explicit slash command. A new skill must keep this, and catalog advertising is
not restored without the user's authorization. `explore` was the one
authorized exception (#107) until #282 made it explicit-only too (see
"Workspace search and the explore skill"); `tests/skill_visibility_regressions.py`
allowlists no skill. The package registers no agents and no MCP servers. A
localhost installed-host test checks every description against a visible
control, checks that request 1's system prompt, tools and messages are the
same with the plugin as without it, and verifies explicit `source` and
`explore` invocation in a fresh session (Claude Code 2.1.302). This removes
skill-catalog overhead; it does not certify zero total plugin tax: hooks still
add context when they act. See Claude's [skill context-loading rules](https://code.claude.com/docs/en/skills#control-who-invokes-a-skill).

| Path | Decision | Why |
|---|---|---|
| Tool-output trimming (`PostToolUse` rewrite of oversized results) | Implemented | Tool results are re-sent on every later turn; a 30 KB build log costs its tokens on each of the next 50 requests. The mechanism (`updatedToolOutput`) is documented, deterministic and reversible (the full output is kept on disk). |
| Test-runner filter (drop passing-test lines) | Implemented | The same argument; test output is the most common oversized result, and passing-test lines carry no information Claude acts on. Only positively recognised passing lines are removed. |
| Repeat-read suppression | Implemented, narrow | Claude Code 2.1.284 and later already answer an unchanged re-read with a short `file_unchanged` result when the file's mtime is unchanged and the content is still in context (checked in the binary; see "Facts confirmed"). The plugin's version keys on the content hash, so it also catches a file rewritten with identical bytes (formatter, `touch`, checkout). It never counts the native suppression as its own. |
| Large-file read guard (`PreToolUse` page a whole-file Read) | Implemented | A whole-file Read of a 500 KB file either fails (over 256 KB) or loads up to 25,000 tokens. Paging it to 300 lines keeps exact ranges available. For suitable Python files, a bounded AST map plus 48 exact lines replaces the 300-line page only when the combined text is smaller. |
| Read compaction (`PostToolUse` elide whole lines of a whole-file Read) | Implemented, opt-in (`ORK_METER_READ_COMPACT=on`, #316) | Paging hides everything past the first page, and the head-only trim cuts where the byte budget runs out. Most of a large source file is bodies, comments and data the model may not need, while its outline (signatures, imports, the first lines) is what it navigates by. Eliding only whole lines, at their original numbers, keeps every shown line usable as an Edit anchor. See "Read compaction". |
| Subagents for exploration, logs, tests | Not shipped (#58) | No agents ship: `agents/` is empty, so `haiku_agents` reports off. See "Haiku agents". |
| Cache-miss nudge on resume | Implemented | Claude Code hands the hook `context_tokens`, `prompt_cache_likely_expired` and `estimated_cache_write_usd` on resume. Telling the person before the first turn re-writes the whole context is cheap and uses no prompt content. |
| Loop detection (repeated identical failures) | Implemented | A counter per (tool, input hash); a warning at 3, an optional hard stop at a threshold the person sets. No content stored. |
| Static setup audit (`/ork-meter:audit`) | Implemented | CLAUDE.md and rules files load into every session; their size is measurable locally. MCP servers are counted by name; their schema size is only known to the server. |
| Stale-context guard | Implemented, opt-in | A prompt sent after the conversation's prompt cache expired re-sends the whole context at the cache-write price; measured on one machine, such prompts were about a third of spend (#59). A `UserPromptSubmit` hook holds that one prompt when the context is large and offers `/clear`, a focused `/compact`, or resubmitting. Off until the person turns it on (`ORK_METER_STALE_GUARD`). See "Stale-context guard". |
| Context-resident delta delivery (re-reads of changed files, re-runs of the same command) | Implemented, opt-in, experimental | A test-fix loop re-runs the same command and re-reads the files it edits; each result is mostly identical to one still in context, yet is written to the cache again and then re-read on every later turn. The `delta` lever (`ORK_METER_DELTA=on`) sends a unified diff against that earlier result instead. See "Context-resident delta delivery". No whole-task saving is claimed until a live paired run. |
| Context-size nudges | Implemented; on by default only per token | Every turn re-sends the whole conversation. When the context reaches a threshold, a `UserPromptSubmit` hook suggests `/clear` (unrelated next task) or a focused `/compact` (keep going), to the person only. Unset, it is on for a session billed per token and off on a subscription, detected from metadata once per session (`hooks/lib/billing.py`); `ORK_METER_CONTEXT_HYGIENE` decides instead. See "Context-size nudge". |
| Cost-per-turn status line | Implemented, opt-in | A status line is a user setting the plugin can't and shouldn't overwrite, so `/ork-meter:statusline` prints a snippet the person adds. See "Cost status line". |
| Model routing via Plus classification (#61) | Rejected for now | Depends on local classification (#56), which doesn't exist yet. Routing on a remote classifier would send prompt text off the machine, which this plugin never does. |
| Cache-friendly stable context (keeping the system prompt and early context byte-identical) | Not a plugin lever | The plugin already avoids it: hooks inject nothing at the start of the context except a short status line to the person (not to the model), and `additionalContext` is only attached to the tool results it rewrites. There is nothing more to implement without controlling the harness. |
| PreCompact guidance | Rejected | A `PreCompact` hook could inject compaction instructions, but the plugin has no knowledge of the task; generic instructions risk dropping what the person needs. |
| MCP result limits | Rejected, except record handles | `updatedMCPToolOutput` exists, but MCP results have server-defined shapes; trimming them blind can break structured results Claude parses. Built-in tools have known shapes. The one exception, large received MCP record arrays as a compact schema plus an exact local artifact, is off by default pending qualification (legacy pins stay off); see [record handles](record-handles.md). |
| Gateway or proxy mode (auto-routing requests) | Rejected | Puts Ork Meter in the request path with prompt content, reversing the metadata-only position (ork-meter #211). |

## Defaults and why

Trimming, repeat-read suppression and the read guard ship **on** (a CTO decision, against the original opt-in plan), because the product is presented as optimising tokens and must actually do so. The other levers follow the same default so a reviewer flips one setting, not six. Every lever has an environment opt-out, and the status line at session start says the trim is on, its budgets and the read-guard threshold, and how to see what it did.

Every switch, tunable, default and floor below is declared once, in the `LEVERS` table of `hooks/lib/features.py` (the lever registry). The hooks ask it `features.enabled(env, lever)` and `features.value(env, lever, key)`; no other file reads an `ORK_METER_*` variable, and `tests/features.test.sh` checks that, and checks this table against `LEVERS`. The status line (`features.status_line`) is built from the same values, so it shows an override.

| Lever (`LEVERS` key) | Default | Opt-out (any set to `off`, `0`, `false` or `no`) | Tunable (`key`) | Default | Floor | Unit |
|---|---|---|---|---|---|---|
| Tool-output trimming (`trim`) | on | `ORK_METER_TRIM` (also switches off the next three) | `ORK_METER_TRIM_BUDGET_BASH` (`budget_bash`) | 12288 (12 KiB) | 1024 | bytes |
| Received MCP record handles (`trim`) | off pending qualification | `ORK_METER_TRIM` | `ORK_METER_RECORD_HANDLES` (`record_handles`; on with `on`, `1`, `true` or `yes`, off with `off`, `0`, `false` or `no`) | 0 | 0 | switch |
| | | | `ORK_METER_TRIM_BUDGET_READ` (`budget_read`, used by `dedupe-reads.py`) | 24576 (24 KiB) | 1024 | bytes |
| | | | `ORK_METER_TRIM_BUDGET_GREP` (`budget_grep`) | 8192 (8 KiB) | 1024 | bytes |
| | | | `ORK_METER_TRIM_BUDGET_GLOB` (`budget_glob`) | 4096 (4 KiB) | 1024 | bytes |
| | | | `ORK_METER_TRIM_BUDGET_WEBFETCH` (`budget_webfetch`) | 16384 (16 KiB) | 1024 | bytes |
| Test-runner filter (`test_filter`) | on | `ORK_METER_TRIM`, `ORK_METER_TEST_FILTER` | — | | | |
| Repeat-read suppression (`dedupe`) | on | `ORK_METER_TRIM`, `ORK_METER_DEDUPE` | — | | | |
| Large-file read guard (`read_guard`) | on | `ORK_METER_TRIM`, `ORK_METER_READ_GUARD` | `ORK_METER_READ_GUARD_BYTES` (`bytes`) | 98304 (96 KiB) | 4096 | bytes |
| | | | `ORK_METER_READ_GUARD_LINES` (`lines`; a plain page's length, and a file with no more lines is left alone) | 300 | 50 (ceiling 1500, so the page stays at least 500 lines shorter than Read's native 2000-line page and the token gate can admit it) | lines |
| | | | `ORK_METER_READ_GUARD_TOKENS` (`tokens`; Read compaction's threshold, counted offline as Claude Code renders the Read, see "Read compaction") | 6000 | 1000 (ceiling 20000, the largest Read the guard lets through whole for compaction) | tokens |
| | | | `ORK_METER_READ_COMPACT` (`compact`; Read compaction, **off by default** until the Write guard is hardened (#316): on with `on`, `1`, `true` or `yes`, from the next session; off with an off spelling, at once) | 0 | 0 | switch |
| | | | `ORK_METER_WRITE_GUARD_OVERRIDE` (`write_guard_override`; the Write guard's deliberate override: on with `on`, `1`, `true` or `yes` lets every Write through, from the next session; see "Write guard") | 0 | 0 | switch |
| Haiku agents (`haiku_agents`) | off: not shipped (`agents/` is empty) | none | — | | | |
| Cache-miss nudge (`cache_nudge`) | on | `ORK_METER_NUDGES` | `ORK_METER_CACHE_NUDGE_TOKENS` (`tokens`) | 40000 | 1000 | tokens |
| | | | `ORK_METER_CACHE_NUDGE_ACCEPT_SECONDS` (`accept_seconds`) | 300 | 1 | seconds |
| Loop guard (`loop_guard`) | on (warn only) | `ORK_METER_LOOP_GUARD` | `ORK_METER_LOOP_WARN` (`warn`) | 3 | 1 | failures |
| | | | `ORK_METER_LOOP_STOP` (`stop`; `off`, `0`, `false` or `no` also mean unset) | unset (never stop) | 1 | failures |
| Setup audit (`audit`) | a command the person runs | — | — | | | |
| Context-size nudge (`context_hygiene`) | **on in a session billed per token, else off** (`per_token_default`; decided once when the session is pinned, see "Context-size nudge"). `ORK_METER_CONTEXT_HYGIENE` set to `on`, `1`, `true` or `yes` turns it on, an off spelling turns it off, whatever the billing | an off spelling of `ORK_METER_CONTEXT_HYGIENE` (applies at once) | `ORK_METER_CONTEXT_NUDGE_TOKENS` (`tokens`) | 120000 | 20000 | tokens |
| | | | `ORK_METER_CONTEXT_NUDGE_STEP` (`step`) | 60000 | 10000 | tokens |
| | | | `ORK_METER_CONTEXT_NUDGE_ACCEPT_SECONDS` (`accept_seconds`) | 600 | 1 | seconds |
| Stale-context guard (`stale_guard`) | **off** (opt-in: on only when `ORK_METER_STALE_GUARD` is `on`, `1`, `true` or `yes`) | none needed: unset or anything else is off | `ORK_METER_STALE_GUARD_TOKENS` (`tokens`) | 100000 | 20000 | tokens |
| Context-resident delta delivery (`delta`) | **off** (opt-in: on only when `ORK_METER_DELTA` is `on`, `1`, `true` or `yes`) | `ORK_METER_TRIM` | — | | | |

One parser (`features.parse_int`) reads every tunable with one grammar: a non-negative integer, optionally followed by `k` or `kib` (case-insensitive; the number is multiplied by 1024), with surrounding whitespace ignored, so `12288`, `12k`, `12K` and `12KiB` are the same budget. An empty or unparseable value (`1.5k`, `-5`, `12kb`) gives the default; a value below the floor gives the floor, so `ORK_METER_TRIM_BUDGET_BASH=100` is 1024, and a value above a ceiling gives the ceiling, so `ORK_METER_READ_GUARD_LINES=5000` is 1500. One tunable, `ORK_METER_LOOP_STOP`, declares `off_means_unset`: the opt-out spellings (`off`, `0`, `false`, `no`, any case) mean unset, so the loop guard never stops; any other value is parsed as above with floor 1 (`1` stops at the first failure, `2k` at the 2048th). A tunable with unit `switch` (`ORK_METER_RECORD_HANDLES`, `ORK_METER_READ_COMPACT`) is an on/off control instead: `on`, `1`, `true` or `yes` turn it on, `off`, `0`, `false` or `no` turn it off, and anything else is its default; like an opt-out, turning it off applies at once, and turning it on waits for the next session.

A project's `.claude/settings.json` and `.claude/settings.local.json` may only turn settings off (an off spelling or mode `off`, never a budget or threshold); anything else there is ignored for the user's or managed value or the default, as `hooks/lib/project_env.py` describes.

To go back to the issues' opt-in default, a reviewer changes `LEVERS` in `hooks/lib/features.py` and the `enabled()` check to opt-in semantics; nothing else depends on the default.

## Shadow capture and native usage replay

`ORK_METER_MODE=shadow` disables automatic tool reduction, Read input rewriting,
dedupe, cache nudges and loop stopping. Existing registered tool hooks instead
store each received observation atomically under the session store's
`shadow-traces/`, with tool-call/agent/session identities, original input,
response, error/interruption fields, version and provenance. This is explicit
local-content capture for offline replay; it does not feed the hosted receiver.
Files have mode 0600 and directories 0700 on POSIX; Windows uses the user's
directory access controls. Capture/storage failure leaves the tool unchanged.
There is no savings ledger entry for unmodified shadow output.

The default mode is `reduce`, preserving existing behavior. `ORK_METER_MODE=off`
disables automatic optimization without capturing content. Invalid mode values
also pass through. These modes do not disconnect metering, remove the plugin's
agent/skill definitions, or certify a zero-overhead installed baseline. Set the
mode before starting a new session. The first hook with a valid session identity
pins parsed budgets, thresholds, feature switches, mode and plugin version.
Events that retain the same native session identity keep this pin, including
resume, clear and compaction. Immediate opt-outs remain effective;
other setting changes apply to a new session identity. A mode change never
starts reduction or content capture partway through an existing session.
Capture covers registered hook events only: Read pre-use, the five built-in
post-use tool families and tool failures. Host truncation before hook delivery
is unverified, so captures identify themselves as received data rather than
asserting they contain the original command's entire output. Source snapshots
and task outcome linkage are not captured.

For captured usage streams, run:

```sh
python3 -I -B scripts/usage.py --harness claude main-transcript.jsonl
python3 -I -B scripts/usage.py --harness codex app-server-events.jsonl
python3 -I -B scripts/usage.py --harness codex --scope interval --baseline before.jsonl after.jsonl
```

The local JSON report retains provider usage fields and normalizes observed
categories. Claude uses one reconciled snapshot per message identity. Codex
uses one cumulative `total` snapshot per thread, retaining `last` as provenance
without adding it. Delayed snapshots and duplicates are not charged twice;
conflicting snapshots, malformed lines and unreadable streams produce explicit
gaps. Reasoning output is reported separately without adding it again to total
output. An absent Codex cache-write counter is unknown, not measured zero.
Codex uncached-input billing semantics remain unverified, so that derived
category stays null. The notification contract is documented in the
[Codex app-server reference](https://learn.chatgpt.com/docs/app-server); field
fixtures follow the Codex 0.162.1 schema.

Usage report schema v2 distinguishes capture files from provider counter
epochs. Split, copied, rotated and resumed captures of the same Codex thread
merge into one cumulative observation; duplicated Claude message identities
also merge across files. A new filename or retry label does not prove newly
incurred usage. Independent thread/request identities count separately.
Use `--attribution subagent` or `auxiliary` for separately captured work.
The Python ledger API can combine differently attributed captures; a reused
counter identity cannot change its attribution silently.

The default report covers observed provider lifetime counters.
`--scope interval --baseline before.jsonl after.jsonl` subtracts the known
starting snapshot from the ending cumulative counters. The baseline file
must identify one starting usage observation per thread being measured.
An interval without a thread's baseline reports partial evidence and null
interval totals, preserving the observed lifetime counters separately.
`UsageLedger.begin_interval` supplies the same operation to local callers.
Missing optional categories in either endpoint remain unknown.

An explicit `--counter-epoch` requires `--epoch-evidence`, describing the
provider reset or independent counter namespace. That provenance is retained
as caller-supplied evidence, not independently certified. A lower cumulative
snapshot alone cannot establish a reset: it may be delayed capture. Such
observations keep the greatest compatible lifetime snapshot and report an
explicit ambiguity gap, rather than inventing another charge. Mixed-direction
counter changes are rejected as contradictory. Repeated paths count once.
Reports cover only supplied streams; missing child/auxiliary work, retries,
price tables and actual billed amounts are never inferred. Exit status 1 means
partial or unavailable evidence. No network or model requests are made.

## Mechanisms

### Failing Bash commands

A Bash command that exits nonzero reaches hooks as `PostToolUseFailure`, which can't replace the output. By then Claude Code has already rendered the error, and that rendering may have dropped diagnostics from the middle and stderr. This is native Claude Code behaviour: it was observed with the plugin off as well. So:

- **Unsupported:** automatic capture of ordinary failing Bash calls. The trimmer, the test-runner filter and delta delivery act only on runs that exit 0, and no `tool-output/` snapshot is taken of a failed run: one made from the already-rendered error would not be the full output. Shadow mode's local trace still records the failure payload as the hook received it, which is the same rendered error.
- **Never done:** rerunning a failed command to recover its missing output.
- **Supported:** for a command you expect to fail, `/ork-meter:capture` (`scripts/command.py`, next section) runs it once and saves the complete stdout and stderr before Claude Code renders them. That is an explicit route only: the skill is user-only (`disable-model-invocation: true`), so the person runs it and Claude doesn't invoke it on its own, and no Bash call is rewritten to use it.
- **Unqualified for that route:** cancellation and lifecycle (installed-host interrupt and cancel controls, POSIX hard wrapper termination, deliberate process-group escapes, detached and background commands, which keep the native path), and large-stream behaviour (both streams are buffered in memory until the command ends, with no cap).

### Command capture before native rendering

`/ork-meter:capture` uses `scripts/command.py` for a selected noninteractive argv.
It executes once in the caller's existing sandbox, preserving argv, cwd,
environment, stdin and exit status. It buffers stdout/stderr until completion;
PTY/interactive commands keep their native path. Explicitly pass a shell when
the intended command requires a pipeline. It does not rewrite other commands,
grant permission, or rerun a failed command to recover its missing evidence.
The capture skill adds no automatic tool allowlist. The wrapper invocation uses
the native Bash permission check, while original-command permission patterns
are not re-evaluated inside it. Only already-authorized argv may be routed;
a denied command must not be translated into an allowed wrapper invocation.

The invocation can stay the same across sessions and runs, so it caches as
part of a prompt: `bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script
scripts/command.py -- PROGRAM ARG...`. Without `--session` or `--scratchpad` the
wrapper uses the session Claude Code names in `CLAUDE_CODE_SESSION_ID` for its
Bash commands, and without `--plugin-data` the `CLAUDE_PLUGIN_DATA` environment
variable when set. Claude Code 2.1.302 sets neither `CLAUDE_PLUGIN_ROOT` nor
`CLAUDE_PLUGIN_DATA` for Bash commands, so the skill substitutes the root and
`--plugin-data` inline; both are stable per installation. A caller that exports
them to the CLI can use `"$CLAUDE_PLUGIN_ROOT"` and drop `--plugin-data`
(`tests/native_capture_env_qualification.py` checks both forms). The
recovery exemption recognises this form as it did the longer one: only
`CLAUDE_PLUGIN_ROOT` may spell the wrapper's path, and a pipeline or any other
expansion makes it an ordinary command (#180, #217).

The wrapper reduces only when its stdout and stderr are regular files, as
Claude Code's Bash tool gives a command (2.1.302). When the model pipes it
into its own filter (`-- pytest -v 2>&1 | tail -30`, `| grep -c PASSED`), the
original streams pass through unchanged and no trim is recorded: reducing them
would change what that filter answers (a count of PASSED lines, the real last
lines), and the model sees only the filter's output anyway. In benchmark B1
(#212) every wrapped test run was piped into `tail`, and the ledger credited
5,919 estimated tokens per trim for a real saving of about 55. The full output
is still saved for recovery. A redirect to a file (`> out.txt`) looks the same
as the Bash tool's own file and is still reduced. Benchmark B1 (#212) put
three per-run temporary paths in the prompt and in every wrapped command,
about 450 characters per call that no other run could reuse from cache.

The complete child streams are stored before host result rendering. UTF-8 text
is retained exactly; binary streams use labelled base64 in the JSON snapshot
and can be recovered byte-for-byte with `scripts/artifact.py --stream stdout`
or `--stream stderr`. Wrapper-origin artifacts explicitly identify their
complete pre-rendering capture, separately from hook-origin snapshots whose
upstream completeness is unknown. Returned reduced text starts with a recovery
path. Failed text uses a conservative 4 KiB soft budget; status remains nonzero.
Required diagnostics may exceed budgets; this does not certify arbitrary host
inline capacity. Structured and binary results pass through without reduction.
`--full`, off and shadow modes emit original streams unchanged. Captures remain
local even when no reduction occurs because the command was explicitly routed
through capture. If storage fails, the already-executed original streams return;
if emission fails, no successful reduction is recorded. Accounting failures
after emission create independent local gaps.

`--cancel-file PATH` adds explicit local cancellation: creating the path cancels
the command; a preexisting path prevents execution. Cancellation retains exact
partial streams, marks `interrupted` and `command_completed: false`, and skips
reduction/accounting. POSIX signals the separate process group, escalating after
0.5 seconds if it ignores the signal. Windows assigns a blocked bootstrap to a
private Job Object before allowing the original argv to execute; its descendants
inherit containment. The argv is sent over an anonymous pipe, never an extra
environment variable or temporary file. Signals/cancel-file terminate the job,
and hard wrapper termination closes the job and stops its descendants.
Normal completion removes the kill-on-close flag before closing the job.
See Microsoft's [Job Object lifecycle](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).
Unsupported Windows job assignment fails before the original executes.

Hard wrapper termination on Windows leaves no final artifact. Detached and
background commands keep the native path. POSIX hard wrapper termination,
deliberate process-group escapes and installed-host native cancel controls are
unqualified (#204).

The Bash post-use reducer recognizes this trusted standalone wrapper and does
not transform its output a second time. No automatic pre-use routing is
enabled: ordinary failing Bash calls still take the native, lossy error path
(#204).

```sh
python3 -I -B scripts/command.py --scratchpad /private/session-scratch -- PROGRAM ARG1 ARG2
python3 -I -B scripts/artifact.py --scratchpad /private/session-scratch --id SHA256 --stream stderr
```

### Tool-output trimming (`hooks/trim-tool-output.py`, `hooks/lib/trimmer.py`)

`PostToolUse`, matcher `Bash|Grep|Glob|WebFetch`. The hook reads the payload from stdin, measures the result's text, and if it is over the tool's byte budget prints `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": <same object, text fields replaced>}}`.

Budgets (from the lever registry, "Defaults and why"): Bash 12 KiB, Read 24 KiB, Grep 8 KiB, Glob 4 KiB, WebFetch 16 KiB. A result at or under budget is never touched; the hook prints nothing.

For text over budget, in this order:

Before line processing, recognizable JSON/JSONL, scalar JSON, markup, YAML
document headers, unknown bracket-prefixed formats and binary-like/control text
pass through. Known bracketed log tags remain eligible. The same guard applies
before passing-test filtering, so it cannot first damage a structured payload.
This conservative syntax check does not recognize every structured format;
schema-aware reducers and a broader format-confidence contract remain needed.

1. ANSI escape sequences removed.
2. Trailing whitespace removed from each line.
3. Runs of three or more identical consecutive non-diagnostic lines collapsed to the line plus `… (repeated N×)`. Diagnostic occurrences remain individually visible.
4. Non-diagnostic lines over 2,000 characters cut to 2,000 plus `… [+N chars]`. Diagnostic lines remain complete.
5. If still over budget: the head (60% of the budget) and the tail (40%) are kept, with a marker stating how many normalized lines and bytes were omitted. Lines that look like errors (case-insensitive `error|fail|panic|traceback|exception|fatal|denied|timeout|exit code`) are retained in full without a count cap. Retained diagnostics are excluded from omission counts. Their evidence may exceed the soft budget; when the resulting text would not shrink, the original passes through. This pattern-based guard does not establish that arbitrary stack-frame or multi-line diagnostic context is recognized.
6. The hook-received text is written to the session store's `tool-output/<sha256>.txt` (directory 0700, file 0600), with a JSON companion containing the exact received structured response. The text is stored once: where the response holds the same string, the companion has null in its place and records where it goes, and recovery puts it back after checking its hash (#190); snapshots written in earlier layouts still load. The result ends with `Full output: <path> (<bytes> bytes)`. When that path is outside the payload's `cwd` (always, for a scratchpad store, unless the session runs from a parent of it), a second line follows: `Outside the workspace; Bash sed -n 'A,Bp', head, tail or cat reads it.` (71 bytes with its newline, 22 tokens; a statement, not an instruction, so a test-filter note still doesn't send Claude to the original, #212). File tools confined to the working directory (`--restricted`, sandboxes, permission rules) refuse a Read or Grep of the path, and each refusal cost a request in the P2 benchmark (6 in 5 sessions, #269); Bash reads it under `--restricted` (qualified natively, `tests/native_recovery_qualification.py`). The line holds no ` (`, so parsers that take the path as the text before the last ` (` still work, and it names no path again: the `scripts/artifact.py` command would cost about 335 bytes (137 tokens) per note. A plain `cat`, `head`, `tail` or `sed -n 'A,Bp'` whose every operand is an artifact in this session's `tool-output/` (one simple command, no pipes, redirections or `$` expansions, known options only) passes through untrimmed and is not saved again (#129); anything else, including other sessions' artifacts, keeps the normal reduction. Such a read is bounded only by Claude Code's own Bash output limit, so a range (`sed -n`, `head`, `tail`) or `scripts/artifact.py --start/--limit` is the right way to read a large one. `/ork-meter:recover` searches the snapshot, expands exact lines, projects JSON keys or returns the original received response without rerunning the command. The hash binds text and response; repeated tool IDs do not overwrite earlier results. Replacements that grow the output after recovery notes pass through. The snapshot is written only after the reply passes the token gate and the deadline; a refused or late reply leaves none, and a failed write passes the output through untrimmed (#130). A snapshot does not restore content the host omitted before delivering its hook payload.

Shapes, taken from the 2.1.295 binary's tool schemas: Bash `{stdout, stderr, interrupted, isImage?, …}` (stdout and stderr replaced; stderr targets a quarter of the soft budget); Grep `{mode, numFiles, filenames[], content?, …}` (`content` in content mode, else `filenames`); Glob `{filenames[], numFiles, truncated, durationMs}` (`filenames`, with the marker as one entry); WebFetch `{result, bytes, code, codeText, durationMs, url}` (`result`). Numeric fields are left as the tool set them. A Bash result that is an image, or a background-task note, is never touched. Claude Code already replaces a valid Bash result over about 30,000 characters with a file path and a 2,000-character preview, so this hook acts on the 12–30 KB band.

Test-runner filter: when `tool_input.command` matches `go test`, `pytest`, `npm|yarn|pnpm|bun test`, `cargo test`, `mvn … test`, `gradle … test`, `jest`, `vitest` or `mocha`, lines that only say a test passed (`--- PASS`, `=== RUN`, `ok  pkg 0.1s`, `test x ... ok`, `✓ …`, `PASS suite`, `… PASSED`, pytest `-rA` short-summary `PASSED path::test` lines, pytest progress dots, Maven's zero-failure summary) are removed before the size trim. Nothing else is removed, so failure blocks keep their context. If the filtered output is still over budget and the trim can't shorten it without dropping diagnostics, it is delivered as filtered, with its note, whenever that saves more than the note costs. A failing run (nonzero exit) reaches `PostToolUseFailure` instead, which cannot rewrite output, so the filter acts only on runs that exit 0. The result ends with `… [Ork Meter test filter: N passing-test lines omitted; every other line is shown, including the summary]`, which claims only what holds: `(colour codes removed)` or `(repeated blank lines collapsed)` is added when the filter did that, and `including the summary` only when a summary line (pytest's counts, `Tests:`, `test result:`, Maven's `Tests run:`, go's `ok`/`FAIL`) is present. The note is complete in itself: it names what went and does not ask Claude to fetch it (in benchmark B1, #212, the older "recover the saved original" wording led to reads that only re-confirmed counts already shown). The full output stays saved, and the result's `Full output:` line names it for `/ork-meter:recover`. When a size trim then also cuts other lines of stdout or stderr, the note says `other lines trimmed where marked` instead, next to the trim's own marker. The command itself is never rewritten; there is no `PreToolUse` hook on Bash. JSON output (`go test -json`) is left alone. The runner is recognised from the parsed executable and its arguments, not from a substring, so `cat pytest.log` or `echo pytest` is not a test run; `python -m`, `npx`, `bash -c` and a leading `cd DIR &&` are unwrapped, and any other compound command is left alone. Go activity lines (`=== RUN`, `PAUSE`, `CONT`) are dropped only when paired with a later pass of the same test, and up to eight named pytest passes (parameterized cases included) are kept, since a small set can confirm a fix.

### Read results (`hooks/dedupe-reads.py`)

`PostToolUse`, matcher `Read`. One process handles both repeat-read suppression and size trimming for Read because Claude Code runs the hooks of one event in parallel on the original output and keeps the last `updatedToolOutput` it receives (the binary's own hook schema says so); two rewriting hooks on Read would race.

Shape: `{type: "text", file: {filePath, content, numLines, startLine, totalLines, truncatedByTokenCap?}}`. `content` is the raw file text; Claude Code adds the `cat -n` line numbers when it renders the result, numbering from `startLine`. That rendering is why Read is trimmed differently from Bash: any marker inserted into `content`, or any elided middle, would shift the numbers Claude uses for `offset` and for Edit. So:

- **Compaction:** a whole-file result over the token threshold is Read compaction's (below); the size rule applies to everything else.
- **Size:** an implicit whole-file text result over 24 KiB keeps its head, whole lines where possible, with `numLines` and `truncatedByTokenCap` set accurately. The omitted range, partial first line and error excerpts are identified in `additionalContext`. An immutable original received response is stored for recovery after file edits, written only once the reply has passed the token gate and the deadline, as for Bash (#190). Explicit ranges, `allow_large` and Read recovery paths bypass this reduction. Direct plugin recovery and source commands also bypass Bash reduction, so requested full results and exact source do not enter a second reduction loop.
- **Repeat:** the session store's `reads.json` maps the path of each whole-file Read to the SHA-256 of `content` (ranged Reads are never indexed). The same key with the same hash, more than 1 KiB, whose earlier result wasn't itself trimmed and was read for the same `prompt_id`, is replaced by its first 15 lines (`numLines: 15`) with an `additionalContext` note: unchanged since the earlier Read for this prompt, sha256 prefix, line and byte counts, that the earlier result holds the full content, and the exact `offset`/`limit` Read that returns the rest if it is no longer in context. No hook fires on `/rewind` or an edited earlier message, so a Read from an earlier prompt is never stubbed (#129). This assumes, per the hooks reference, that `prompt_id` is shared by every tool call made for one user prompt and that a new or rewound prompt gets a new one; if it were per call, dedupe would simply never fire. Parallel Read hooks read and update `reads.json` under a lock file (`reads.json.lock`, `O_CREAT|O_EXCL`, at most 0.5 s wait, broken after 5 s), so none loses another's entry (#131). Reads are locked too because Windows refuses to replace a file another process has open; every state-file replace also retries such a sharing violation for up to 0.5 s on Windows. A different hash is shown in full (subject to the size rule). A `type: "file_unchanged"` result is Claude Code's own suppression and is left alone and not counted.
- **Reset:** the index is deleted on `SessionStart` (`startup|resume|clear|compact|fork`), because after compaction the earlier result is no longer in the model's context.

### Context-resident delta delivery (`hooks/lib/delta.py`, opt-in)

Off unless `ORK_METER_DELTA` is `on`; `ORK_METER_TRIM=off` also switches it off. Two cases, both keyed by agent (the main conversation and each subagent are separate), so a base is only ever one the same context received:

- **Bash re-run.** The same command text in the same `cwd`. The base is always the last stdout the model received for that command, in full and inline. Every other delivery drops it: a delta, a filtered or shortened result, a native-persisted or potentially native-spilled result, a pass-through the trimmer won't touch (JSON-like or other unsupported formats, images, background tasks, recovery reads) and a failed run, which reaches `PostToolUseFailure`. Merely having a raw artifact is insufficient. When a later run's stdout is at least 2 KiB, below the native-spill boundary, its stderr is within the ordinary stderr budget, and a unified diff (2 lines of context) is at most half its size, stdout becomes a one-line explanation, the diff, and the usual `Full output:` recovery path. An identical re-run becomes one line. Deltas never chain: the run after a delta is delivered in full and becomes the next base. Execution status fields and stderr are unchanged.
- **Read of a changed file.** Only an implicit whole-file Read whose earlier Read of the same path was delivered in full: not trimmed, not cut short by Claude Code itself (`numLines` below `totalLines`, `startLine` past 1 or `truncatedByTokenCap`), and not itself a delta. Both Reads must be whole. The Read result itself becomes the diff, and `additionalContext` carries only Ork Meter's fixed note saying so, never file text: the diff is untrusted file content, so it stays in the tool result where the model treats it as such. `@@` numbers are real file line numbers (the Read's own left-margin numbers are not), and the diff lines are verbatim, so Edit anchors stay exact. Unchanged re-reads keep the ordinary repeat-read behaviour, except right after a delta or a cut-short Read: the model doesn't hold the whole file then, so the next Read is delivered in full.

Bases live in the session store's `delta-bases/`, are verified by SHA-256 before use (a torn or edited base is ignored), and are deleted on every `SessionStart` (compaction included) and at `SessionEnd`, whether or not the lever is still on. Output under 2 KiB, which can never be a base, is never written; it replaces any older base for the same command or file. Admission is the same offline token gate as every replacement (at least 512 tokens saved, notes included). A base that can't be read, written or deleted (disk full, permissions, long Windows paths) only skips delta delivery for that call: the ordinary trim or dedupe reply still goes out and no reducer fault is recorded.

Known limits. A `/rewind` that removes the earlier result from the conversation fires no hook, so the next re-run may be diffed against a result the model no longer holds; the full output path is always included. (Repeat-read suppression avoids this by stubbing only within one `prompt_id`; deltas do not yet.) When the hook delivers a result without its handler updating the base (entitlement lapsed, the reducer circuit breaker tripped, `trim` or `delta` off, shadow mode, or a handler error), `hook.run` drops the base for that command or file, so the next run is delivered in full.

### Large-file read guard (`hooks/read-guard.py`)

`PreToolUse`, matcher `Read`. A whole-file text Read over the threshold (96 KiB by default) is paged to its first 300 lines. For a parseable Python file over 24 KiB and at most 2 MiB, the hook can instead supply the first 48 exact lines and a bounded AST map of symbol names and original line ranges when this combination is smaller than the 300-line page. Claude can follow any range using ordinary Read `offset`/`limit`; an explicit range is never changed. Other exclusions include binary-looking files, `pages`, and `allow_large`. No `permissionDecision` is returned, so normal permission handling applies. The local estimate includes the added context text. Line counting stops after 32 MiB and says "more than". The byte cap and the deadline are checked before every chunk read, including files with fewer than 300 newlines.

With Read compaction on (below), the guard first asks whether compaction may take the Read: if the file is at most 80,000 bytes and not JSON, its text rendered as Claude Code numbers it is over `ORK_METER_READ_GUARD_TOKENS` and at most 20,000 tokens, and neither the Read result hook nor the Write guard is stopped by the circuit breaker, the guard lets the Read through whole and the paging, if any, happens on the result. Every other Read is paged here as before.

An installed-host localhost fixture checks that the AST map reaches the next model request and that an explicit follow-up Read and prior message content are preserved; provider cache hits and live task savings are unmeasured.

### Read compaction (`hooks/lib/read_compact.py`, in `hooks/dedupe-reads.py`)

`PostToolUse`, matcher `Read`, inside the Read result hook. **Off by default; `ORK_METER_READ_COMPACT=on` turns it on** (from the next session, like every switch), `ORK_METER_READ_COMPACT=off` turns it off alone, and `ORK_METER_READ_GUARD=off` or `ORK_METER_TRIM=off` turn it off with their levers. It needs the content, so it can't run before the Read; it replaces the result with `updatedToolOutput`, as the trim does.

**Why it is off by default (#316).** It shipped on by default in #307. A post-merge native review (Grakk, Claude Code 2.1.302) then found five realistic ways a Write still deletes code a compacted Read left out, past the Write guard: an Edit inside an elided range (one exact line found by Grep, or `replace_all`) changes the range's text, so the guard no longer finds it in the file and stops protecting it; seen ranges are tracked by line number, which goes stale once an Edit shifts lines; `--fork-session` starts with an empty session store while the CLI restores its read state from the transcript; on macOS a different case of the same path (`MODULE.PY` after reading `module.py`) is a different record; and the guard lets a Write through when the saved original is missing or an internal error occurs. #319 closes all five (see "Write guard"), each reproduced and now refused with the real CLI; compaction stays opt-in until a separate decision turns it back on. The Write guard keeps running whenever a compacted record exists, whatever the switch says, so a session that opted in stays guarded as far as the guard goes.

**What it takes.** A whole-file text Read result (no `offset`, `limit`, `pages` or `allow_large`; not cut short by Claude Code itself; not a recovery read; not JSON; at most 256 KiB) whose rendering, counted by the offline token counter (`token_gate.counter`: the pinned `tiktoken`, else `pure_bpe`, else ceil(bytes / 4)), is over `ORK_METER_READ_GUARD_TOKENS`. The count is of the text as the model sees it: Claude Code renders each line as `N<TAB>line` (2.1.302), numbering from `startLine`, and the numbers are counted too.

**What it does.** It elides whole lines only, in this order, range by range, until the rendering fits the threshold and is at least 812 tokens smaller (the token gate's 512-token reserve plus room for the note, so a Read just over the threshold isn't compacted only to be refused):

1. a licence or copyright header block (its first line kept);
2. comment blocks, Python docstrings and other multi-line strings (Python strings, template literals and Go raw strings, labelled `string`, never `comment`) of six lines or more, first line kept; a comment marker inside a string is not a comment;
3. Python data literals (`NAME = [...]`, `{...}`, strings and the like) and runs of twelve or more data-only lines (first line kept), and single lines over 1,000 characters (minified or generated text);
4. bodies of functions and classes of eight lines or more, farthest from the top first and inner before outer, keeping the signature: from the AST for Python; for brace languages (C, C++, C#, Go, Java, JavaScript, TypeScript, Kotlin, Rust, Swift and similar) a brace and indentation check that skips any block whose extent isn't certain (a quote, backtick or block comment on a line, a closing brace at another indentation, a body line no deeper than its header); for any other language this step is skipped.

Never elided: the first 40 lines (except a licence block), the lines within two of a `TODO`, `FIXME`, `XXX`, `HACK` or `BUG` mark or a merge-conflict marker (a licence block holding one stays whole), and any range whose marker and empty slots would cost at least what they hide. Blank lines between two elided ranges join them, and a joined marker names at most two kinds (`comment; docstring; more`); a range that reaches the end of the file (blank lines after it included) is its marker alone. The order and every choice are deterministic.

**JSON is not compacted.** JSON is all structure: eliding runs of its lines would keep each run's first line and drop the keys and nesting that make the rest usable, so a structure-preserving reduction would have to rewrite lines, which compaction never does. A JSON Read is left to the read guard and the Read budget as before, and the guard never lets one through for compaction.

**Line numbers.** Claude Code numbers every line of the replacement itself, from `startLine`, so a replacement can't carry its own numbers. An elided range therefore keeps its line slots: its first slot holds one marker, `… lines 812–960 not shown (function foo body; Read offset=812 limit=149 before editing)`, and its other slots are empty. Every shown line is the original line, byte for byte, at its original number in Claude Code's own rendering; no line is ever cut. An empty slot still costs its number (about two tokens), which is why a range is elided only where that saves tokens.

**Delivery.** When the compacted rendering fits, the result is the compacted text (`numLines` its line count, `truncatedByTokenCap` untouched) with one note in `additionalContext`, which says plainly that it is a partial view: `Ork Meter compacted this read of <path>: it is a PARTIAL view. N lines are elided, about T tokens down to C: 41–600 comment, 986–1008 function foo body, … Each elided range is one line '… lines A–B not shown' followed by empty numbered lines that are NOT empty in the file; every other line is exact at its own number. Before editing an elided range, Read it with offset and limit (a plain Read of the whole file again returns nothing new). Never rewrite this file with Write from this view (Ork Meter refuses such a Write); use Edit, and note that Edit with replace_all also changes elided lines. Original: <snapshot>` (at most ten ranges are listed, then how many more). When it still doesn't fit, the result is paged as the read guard would have paged the file (the first `ORK_METER_READ_GUARD_LINES` lines, or the Python outline page when that is smaller), with the guard's alert, which names the file's and the compacted token counts and that a Write from the page is refused; a file the guard would have left whole (under its byte threshold, no outline) goes to the Read budget, whose note then says the compaction didn't fit. The original Read response is saved as a trim saves it (`tool-output/<sha256>.txt` and its JSON companion), so `/ork-meter:recover` returns it.

**Re-reading.** A plain whole-file Read of a file Claude Code has not seen change is answered by the CLI itself (2.1.302: "Wasted call — file unchanged since your last Read. Refer to that earlier tool_result instead."; the Read result hook sees `type: "file_unchanged"` and leaves it alone), so it can never bring the elided lines back. Only a ranged Read (`offset` and `limit`) does, and it is never compacted; the markers and the note point there.

**Write guard (`hooks/write-guard.py`, `hooks/lib/write_guard.py`, #319).** Claude Code records the whole file it read for read-before-edit before the hook replaces the result, so it accepts a Write rebuilt from a compacted view, and that Write deletes the elided code (Grakk's review reproduced it on 2.1.302: 1,006 non-blank lines became 257). A `PreToolUse` hook on `Write` therefore refuses (`permissionDecision: "deny"`) a Write that would delete a line the model has not seen. Its reason names those lines (`A–B kind`, current line numbers) and says to Read them with `offset` and `limit`, or to use Edit.

- **The record.** A compacted Read (or one paged on its result) leaves one record per guarded file in `$XDG_STATE_HOME/ork-meter/read-guard/` (0700): `<name>.json` (0600, written atomically, and only when it changed), named by the SHA-256 of the file's resolved path, plus one tiny pointer file per alias (`<alias>.alias`).
  - **Contents:** the file's per-line hashes (the first 16 hex digits of each line's SHA-256), and, per *view* (a session, or a subagent within it), the unread lines as intervals with their kind (`function foo body`, `comment`, …).
  - **Aliases:** the SHA-256 of the file's `(st_dev, st_ino)`, of its resolved path, and of that path case-folded.
  - **No path, no line text, no snapshot dependency.**
- **Locking.** Each record has its own lock file. A Write check that can't take it within a second refuses the Write ("its record is busy"). Marking lines seen skips instead of waiting, which is stricter, never looser. Nothing is ever written unlocked, and no other record is rewritten, so parallel sessions and hooks never lose a record.
- **Following the file.** Before every decision (a Write, a ranged Read, output that shows the file), the record is re-synced to the file on disk by a line diff of the stored hashes against the disk's. Unread lines move with the lines around them. A line inside a block an Edit replaced stays unread, its new text included (conservatively); a deleted line is gone; an inserted line is read. So an Edit inside an elided range, a `replace_all` across it, or one that shifts every line below it, leaves the range guarded at its current numbers.
- **What counts as seen, per view.** A line counts as seen only when output shown to that session (or subagent) carries its current text:
  - a ranged Read, by the returned lines at the numbers they came from, so stale marker offsets mark only the lines actually returned;
  - Grep content output for one file (`N:text`, and `N-text` context lines from `-A`, `-B` or `-C`), or for several (`path:N:text`);
  - a simple Bash `cat -n` or `nl` of the file (numbered);
  - unnumbered `cat`, `head`, `tail`, `sed -n`, `awk` and similar output of one file, in runs of three or more lines matching the file in order.

  A whole-file Read delivered whole empties that view only. A subagent's reads count for the subagent, not its parent. Pipes, compound commands, scripts and editors are not tracked: the reason points to a ranged Read.
- **The Write check.** The unread lines are the writing view's own. **A subagent, a fork, or a resumed session in a new process starts from the union of every view (the strictest)**: that is what it is checked against until it has a view of its own, and the view it creates by reading starts from that union too. The file's line hashes are aligned with the Write's (`difflib.SequenceMatcher`), and an unread line the alignment doesn't match is deleted by the Write. A copy of the same text elsewhere doesn't keep it: two identical function bodies, one shown and one elided, count as two, and a repeated `}` keeps only the one it aligns with. Moving an unread block within the file may count as deleting it (refused, recoverably). A counting pre-check runs first: an unread line whose text the Write holds fewer times than the unread lines need is reported without aligning. Only when every count is met does the alignment run, and it is capped at 10,000 lines of the file or the Write (about 0.5 s on a laptop for a scrambled Write of that size; 20,000 lines took 1.5 to 3 s and 40,000 about 9 s, past the hook's 5 s limit). Past the cap the Write is refused as too long to compare, never guessed: Read the unread lines, or use Edit.
- **When it lets a Write through.** When no unread line would be deleted (they have been read, or the Write keeps them in place), or when `ORK_METER_WRITE_GUARD_OVERRIDE` is on. The override is the deliberate-replace path, **for every file at once, not per file**, and applies from the next session like every switch. There is no automatic "deliberate" exception: a Write that keeps almost nothing of the file is exactly as destructive to the unread lines as one rebuilt from the view.
- **Fork, resume, other sessions.** The records are per user, not per session, so `--resume` and `--fork-session` (whose Claude Code restores read state from the transcript) keep the guard: a fork has no view of its own, so it gets the strictest.
- **Another spelling.** `MODULE.PY` after reading `module.py` on a case-insensitive disk, `dir/./module.py`, and a file an Edit replaced by renaming (a new inode) all find the record through its aliases.
- **Failing closed.** With a record for the file, any problem refuses the Write with a recoverable reason ("Read the lines … with offset and limit, or use Edit"): a busy lock, an internal error, a file or Write too long to compare, or an unreadable record. An unreadable record becomes a *tombstone* (the file's aliases). Every Write to the file is then refused until the file is delivered whole to the writing session (the tombstone lists the sessions cleared), or the override is set. No change to the file clears it, not even the model's own Edit, which is exactly what the refusal suggests. Corruption affects only that one record. Without a record the hook prints nothing.
- **Every mode.** Guarding is not a saving: the hook runs in every mode (`EVERY_MODE` in `hooks/lib/hook.py`), shadow and off included, and whatever the levers say. Switching modes or turning compaction off mid-session doesn't unguard a view already in context, and with compaction off from the start there is never a record.
  - **Reads in every mode:** a `PostToolUse` part of the same hook (Read, Bash, Grep) marks lines read whenever the reducing hook for that result won't (outside `reduce` mode, its lever off, or its circuit tripped). In `reduce` mode the reducing hooks (`dedupe-reads.py`, `trim-tool-output.py`) do it themselves from what they delivered, so the two never disagree about a result one of them replaced. A ranged Read is never replaced, so it is always marked by the guard's own part.
  - **Cost when idle:** with no record anywhere (the default, with compaction off), `hooks/py.sh` exits before starting Python. It checks whether any record file exists under `$XDG_STATE_HOME`, `$HOME/.local/state` or `%USERPROFILE%\.local\state`, then drains the payload. Measured on this laptop, the hook costs 5.6 ms per Read, Bash, Grep or Write call instead of 54 ms.
  - **Circuit breaker:** two faults stop the hook for the session (circuit scope `write-guard:Write`), which also stops Read compaction, so no compacted view is delivered without its guard.
- **Expiry and disk.** A record goes when every view is empty, or 30 days after any session last looked at the file. Every Write check, every read it marks and every compaction touches the record, so a session resumed after a week's idle is still guarded. A session that comes back after more than 30 days can't Write the file without reading it first (Claude Code's read-before-edit check), and that Read is compacted anew or delivered whole. There is no count limit, so no record younger than that is ever evicted. Pointers go with their record. A session's transcript is not checked: its path would have to be kept beside the record. The disk use is bounded by how many files were compacted in a week: a record is about 19 bytes per line of its file (a 256 KiB source file of 8,000 lines is about 150 KB) plus three pointer files of 64 bytes.
- **Retention.** Retention never prunes the current session's own saved output (`hooks/lib/retention.py`), and the guard no longer needs it in any case.
- **Not guarded.** Write is the only tool that replaces a whole file in 2.1.302: `MultiEdit` is a rule name there, not a tool, and `NotebookEdit` edits notebooks, which are never compacted. A Bash command that overwrites the file (`cat > file`, a heredoc, `python -c`, `sed -i` across the file, `cp`) is not guarded; the note's "never rewrite this file with Write from this view" is the only defence, and Claude Code's own `Edit`/`Write` deny rules also cover Bash rewrites only by the auto-mode classifier's judgement. An Edit with `replace_all` changes elided lines the model hasn't seen, by design of that tool, and the note says so.

**Order and ownership.** One process handles every Read result (Claude Code runs a tool's hooks in parallel on the original and keeps the last `updatedToolOutput`), in this order: explicit ranges and recovery reads pass through; an unchanged repeat for the same prompt is stubbed (dedupe); a changed re-read may be a delta (opt-in); then compaction owns a whole-file result over its threshold, compacted or paged; the Read byte budget applies to everything else (under the threshold, cut short by Claude Code, compaction off, over 256 KiB) and to what compaction gives back (no fit and no guard page, a search past its deadline, a reply not admitted). One reply per result: a compacted result is never trimmed after, and a compacted or paged result is recorded as partial in the reads index, so a repeat Read is compacted again (to the same text) rather than stubbed, and never becomes a delta base. A ranged Read of a compacted file is recorded for the Write guard before it passes through.

**Ledger.** A compacted result is a `read_compact` line (`path_sha256`, `lines_elided`, bytes and `est_tokens_*` before and after, the note included); a paged one is a `read_guard` line. No content and no path.

**Why 6,000 tokens.** It matches the Read byte budget it replaces for whole files: 24 KiB of source is about 6,000 tokens at the ledger's 4 bytes per token, so the default never lets a whole-file Read deliver more than the trim did, while a file just over it now arrives as an outline of the whole file rather than its first 24 KiB. Below it a Read is left as it was. Raising it delivers more text per Read; the ceiling is 20,000, where the guard stops letting a Read through for compaction.

**Edits after a compaction** (installed CLI, `tests/native_read_compact_qualification.py`, Claude Code 2.1.302, localhost fixture). Claude Code records the whole file it read for read-before-edit before the hook replaces the result, so Edit works as after any Read: an Edit of a shown line succeeds; an Edit of a line inside an elided range with that line's exact text **also succeeds**, because Edit matches `old_string` against the file on disk; an Edit whose `old_string` is marker text, or anything else not in the file, fails safely with `String to replace not found in file` and changes nothing. A ranged Read of an elided range returns its exact lines. A Write rebuilt from the compacted view is refused by the Write guard and the file stays unchanged; a plain whole-file re-read is answered by the CLI as unchanged; after a ranged Read covering every elided range, the same Write goes through. The same test checks that exactly one ledger line (`read_compact`) is written for the Read, with `ORK_METER_READ_COMPACT=off` that the guard pages before the Read as before, and that plain text compaction can't shrink is paged on its result with the compacted size named.

**Time.** About 0.7 ms per KiB of source on a laptop (Apple Silicon, `tiktoken`), most of it the AST; a 256 KiB module takes about 0.2 s, and a search past the hook's budget less 0.3 s stops without a reply of its own (the Read budget then acts). Whole hook processes, median of five, `bash hooks/py.sh` on this laptop: on a 2 MiB file (2,071,170 bytes of Python, 1,944,000 of text) the guard takes 815 and 215 ms and the result hook 275 and 246 ms, the same as before this change (791, 211, 260 and 246 ms on `main`), since neither file is a compaction candidate; a 31 KiB module that is compacted, 197 ms for the guard's check and 240 ms for the compaction; a 250 KiB module that is tried and paged, 518 ms. Fail-open has a size here: when the guard has let a Read through for compaction and the Read result hook then fails or is killed at its timeout, the whole file reaches the model uncompacted, at most 80,000 bytes (the deferral ceiling; Claude Code's own 25,000-token cap bounds it too), where the guard used to deliver a 300-line page. Every hook keeps its 5 s timeout in `hooks.json`, its 1.5 s budget and the circuit breaker: two faults in the Read result hook stop it for the session, and the guard then stops letting Reads through for it.

**Not verified.** The CLI's own cap for a whole-file Read is 25,000 tokens; it estimates bytes / 4 first and counts with the API only above a quarter of the cap, so the guard's byte ceiling follows the estimate and its token ceiling keeps a margin for the API count, which uses Claude's tokenizer, not `o200k_base`. No live model run has been made with compaction: whether models read the markers as intended, and what it does to task tokens and outcomes, is for the `read-guard-legacy` benchmark arm against `read-guard-only`. The rendering format was checked on 2.1.302 only; a host that renders line numbers differently still numbers the replacement itself, so the shown lines keep their numbers.

### Haiku agents (not shipped)

Three subagent definitions (`explore`, `logs`, `test-runner`) are parked in `docs/optional-agents/` and are not registered; they declare `model: claude-opus-5-5`, and copying them into `agents/` is for an explicit experiment only, with delegated usage and task quality counted in the comparison. Their prompts forbid pasting whole files and cap the summary.

`hooks/agent-stop.py` (`SubagentStop`, matcher `^ork-meter:(explore|logs|test-runner)$`) writes one `haiku_agent` ledger line per run. The SubagentStop payload carries no usage figures (checked: `agent_id`, `agent_type`, `agent_transcript_path`, `last_assistant_message`), so the hook sums the subagent's own transcript at `agent_transcript_path` and records the result as `measured_*` fields. Those are tokens the subagent used, measured; they are not a saving. The saving, if any, is the main-context tokens that did not happen, which only a comparison of main transcripts can show.

### Cache-miss nudge (`hooks/trim-session.py`)

`SessionStart`. When the payload has `prompt_cache_likely_expired: true` and `context_tokens` at or above `ORK_METER_CACHE_NUDGE_TOKENS` (default 40,000), the hook adds to its `systemMessage`: the context size, Claude Code's `estimated_cache_write_usd` if present, and the two options (`/clear` and restate the task, or continue and pay the rewrite once). The three fields are documented in the hooks reference for `resume` and `fork` sources on Claude Code 2.1.251 and later; the hook acts only when they are present. The nudge is logged as `cache_nudge` with the numbers. The hook also writes `~/.local/state/ork-meter/cache-nudges/<safe-session-id>.json`; a `SessionStart` with source `clear` for that same session within `ORK_METER_CACHE_NUDGE_ACCEPT_SECONDS` (default 300) of it is logged as `cache_nudge_accepted` with `est_cache_write_usd_avoided` set to that estimate. "Accepted" is itself an inference from timing, and the dollar figure is Claude Code's estimate, not a measurement.

### Stale-context guard (`hooks/stale-context.py`)

Opt-in: nothing happens unless `ORK_METER_STALE_GUARD` is `on` (or `1`, `true`, `yes`), and, like every lever, only with Ork Meter Plus and in `reduce` mode. On `UserPromptSubmit` the hook reads the last main-conversation turn from the transcript tail (`measure.last_turn`): its context size (as `measure.context_tokens`), its timestamp, and the prompt-cache lifetime it was written with, taken from the newest `cache_creation` split in the tail (1 hour or 5 minutes; 1 hour when there is no split). Subagent (sidechain) turns and Claude Code's own `<synthetic>` rows are skipped, and a compaction after the last turn means there is nothing to hold (the context that turn described is gone). No message content is read. Built-in commands such as `/compact` and `/clear` do not fire `UserPromptSubmit` (checked with Claude Code 2.1.300 in `-p` mode), so the guard never holds the command it recommends. When the context-size nudge is also on, it stays quiet for a prompt the guard holds, so there is one message and the guard's advice is not credited to the nudge.

When that turn is older than the cache lifetime and its context is at or above `ORK_METER_STALE_GUARD_TOKENS` (default 100,000), the hook blocks this one prompt (`"decision": "block"`) and tells the person how many tokens it would re-send and how long the conversation was idle, with three choices: `/clear` and restate the task (cheapest), a focused `/compact` (re-sends once, then continues small), or submit the same prompt again to continue as is. The guard holds a prompt at most once per idle episode, keyed by the last turn's timestamp: a resubmission goes through. The state is one file per session, `~/.local/state/ork-meter/stale-guard/<safe-session-id>.json`, holding only the session id, the episode timestamp, whether the person continued, and a time. `ORK_METER_STALE_GUARD=off` stops it at once.

An unattended prompt is never held (`features.unattended`): one sent while Claude Code runs non-interactively (`-p`, stream-json input or an SDK, where `CLAUDE_CODE_ENTRYPOINT` starts with `sdk`), or one Claude Code wrote itself, such as a background task's `<task-notification>` or another session's `<cross-session-message>`. Nobody is there to resubmit such a prompt, so holding it loses it. In Ork Bot crew sessions the guard dropped chat messages, task notifications and peer messages this way (#329 review); the prompt now goes through as sent, and the context nudge doesn't defer to a guard that won't speak.

Each hold is logged as `stale_guard` and a resubmission as `stale_guard_continued`, both with `context_tokens` and `idle_minutes`. The plugin prints no savings figure; while it is on, `stale_guard` is in the active-lever file for the person's Ork Meter ledger. The prompt being held is erased by Claude Code; the person recalls it with the Up arrow.
### Cache report (`/ork-meter:cache`, `scripts/cache_report.py`)

A command the person runs; nothing automatic. It reads Claude Code's transcripts for this project (`$CLAUDE_CONFIG_DIR/projects/<project>` or `~/.claude/projects/<project>`, named from the real path of the project or its nearest parent that has transcripts; `--all` for every project) and uses only per-request usage counters, timestamps, model and effort names, request-shape hashes (`api-request` rows) and `compact_boundary` markers. Streamed copies of one message, and messages a forked or resumed session repeats in a new file, are counted once; subagent (sidechain) requests form their own stream. Each file is ordered by time with its copied messages included, so the first request after an idle resume is compared with the last copied one, whichever file sorts first.

Token counts are measured: they are the usage Claude Code recorded. Dollar figures are estimates at API list prices per model id (longest prefix, so dated ids match; an unrecognised model is priced as Opus 5.5 and named; Claude Haiku 5.5 requests whose prompt, counting cache reads and writes, is over 100,000 tokens use its long-prompt card of $0.50 / $2.50), with cache writes at 1.25x base input for the 5-minute TTL and 2x for the 1-hour TTL; on a subscription they are relative weights. A cache miss is a request that wrote at least half of a context of 8,000 tokens or more and read less than half of the context the previous request had cached; a context that merely grew reads its prefix and is not a miss. Its cause is the first that matches: compaction, idle past the cache lifetime of that write, model switch, effort change, request-shape change, else unexplained. The first request of each transcript is reported on its own line, not as a miss. The report also counts the gaps between requests and compares the 1-hour TTL's extra write premium with the reads it kept across 5-60 minute gaps.

Measured on one machine (all projects, 3,500 requests): cache writes were 56% of estimated cost, and prompts resumed after the 1-hour cache expired were 36.8%. That finding motivated the stale-context guard.

### Context-size nudge (`hooks/context-nudge.py`, issue #60)

Default: **on when the session is billed per token, off on a subscription.** An explicit `ORK_METER_CONTEXT_HYGIENE` in the person's or managed settings always wins (`on`, `1`, `true`, `yes` or an off spelling); a project's settings can still only turn it off (`hooks/lib/project_env.py`). Like every lever it runs only with Ork Meter Plus and in `reduce` mode.

The billing mode is decided once, when the session's policy is pinned (`policy.pin_environment`), and only while the switch is unset; the pin records the resulting `on` or `off`, so the default never flips mid-session, and an unset switch keeps the pinned value, as does a value that is neither an on nor an off spelling (`maybe`): only an off spelling turns it off at once, and turning it on waits for the next session. `hooks/lib/billing.py` reads metadata only, and the first signal that answers wins:

1. The prompt-cache lifetime Claude Code chose. With no override, Claude Code writes the main conversation's cache with a 1-hour lifetime on a subscription within its usage limits and a 5-minute one on an API key, Bedrock, Vertex or Foundry (the `promptCacheTtl` setting's description in Claude Code 2.1.302), so a subscription past its limits on usage credits shows as 5 minutes too. The newest main-conversation write's `cache_creation` split (`ephemeral_5m_input_tokens` means per token, `ephemeral_1h_input_tokens` subscription) is read from this session's transcript, else from the three newest other transcripts in its folder modified in the last 7 days, at most 256 KiB from the end of each; subagent rows are skipped. When the lifetime was chosen by hand (`CLAUDE_CODE_PROMPT_CACHE_TTL`, `ENABLE_PROMPT_CACHING_1H` or `FORCE_PROMPT_CACHING_5M` set, or `promptCacheTtl` in user or managed settings) this signal is skipped. It is also skipped when any of the project's settings files (the ones `project_env` reads) sets an auth, cloud-provider or cache-lifetime variable, `apiKeyHelper` or `promptCacheTtl`, or can't be read: such a project really makes its sessions bill per token or cache for 5 minutes, and reading that as billing would let it turn the nudge on.
2. Claude Code's auth configuration, by name only: `apiKeyHelper` in user or managed settings (never a project's), `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` or `CLAUDE_CODE_USE_FOUNDRY` on, or `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` present. A key's value is never read, logged or stored. A name a project's settings file sets is not counted, nor is any when a project file can't be read.
3. Otherwise unknown: off.

The plan the person declares in `/ork-meter:connect` would be the best signal and would come first, but the plugin runtime keeps it with the pairing record in the OS credential store, which hooks never read; no plugin-data file carries it today. Detection is bounded (0.25 s) and any error reads as unknown (off); it never trips the reducer circuit. `/ork-meter:audit` prints the default for a new session in the project and the signal behind it, reading the switch as the hooks do (a project's `on` ignored). On `UserPromptSubmit` the hook reads the current context size from the transcript tail (`measure.context_tokens`): the last assistant message's `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` and `output_tokens`, read from at most the last 512 KiB of the file, without reading any message content. At or above `ORK_METER_CONTEXT_NUDGE_TOKENS` (default 120,000) it shows the person a `systemMessage` with the size and two options: `/clear` and restate the task when the next work is unrelated, or `/compact` with a focus to keep going. It adds nothing to the model's context, so the prompt cache is untouched and the turn costs nothing extra.

Rate limit: after a nudge, the next waits until the context has grown by `ORK_METER_CONTEXT_NUDGE_STEP` (default 60,000), and a session gets at most three. The state is one file per session, `~/.local/state/ork-meter/context-nudges/<safe-session-id>.json`, holding only the session id, the count, the size at the last nudge and its time. Setting the switch to an off spelling dismisses the nudges at once (pinned session settings apply the off immediately; turning it on waits for the next session). Each nudge is logged as `context_nudge`. A `SessionStart` with source `clear` or `compact` for the same session within `ORK_METER_CONTEXT_NUDGE_ACCEPT_SECONDS` (default 600) is logged once as `context_nudge_accepted` with the action; "accepted" is an inference from timing.

The plugin prints no savings figure for this lever. While it is on, `context_hygiene` is in the active-lever file, so the person's own Ork Meter ledger can compare observed tokens on days with and without it ("Active-lever file"). The nudge thresholds are starting points, not measured optima, and the task-switch heuristic the issue describes is not implemented: the nudge keys on size alone.

### Cost status line (`scripts/statusline.py`, `/ork-meter:statusline`, issue #60)

Opt-in, and not a hook: Claude Code runs a status line command after each assistant message, after `/compact`, on a few other events and every `refreshInterval` seconds, debounced at 300 ms, cancelling a run still in flight, with the session's details as JSON on stdin (`session_id`, `transcript_path`, `model`, `cost`, `context_window`, `prompt_cache`, ...). A plugin's settings only take `agent` and `subagentStatusLine`, so the plugin can't turn on a main status line; `/ork-meter:statusline` prints a snippet (`python3 -I -B "<plugin root>/scripts/statusline.py"`, forward slashes, `refreshInterval: 60` so the cache countdown moves while idle) and the person adds it. The plugin ships no default `subagentStatusLine` either: that would be on by default. On Windows Claude Code runs status line commands through Git Bash when it is installed (which the plugin's hooks need anyway) and PowerShell otherwise; the snippet works under both.

The line is `ctx <tokens> | turn ~$<x> | session ~$<y> | cache <1h|5m>: <n>m left|expired (est.)`. `ctx` is the newest main-conversation request's fresh input + cache reads + cache writes (the figure Claude Code's own `used_percentage` is computed from), or `compacted` after a `compact_boundary` until the next request. `turn` sums the requests since the person's last prompt (a `user` row whose content is a string, text or image; tool results and meta rows don't count), `session` every request in the transcript and in its `subagents/*.jsonl`, each message id once (streamed blocks repeat and grow its usage; only the growth is added). Both are priced exactly as `scripts/cache_report.py` prices a request (it imports `PRICES`, `priced` and `prices`, so the Haiku 5.5 long-prompt card applies), with cache writes at 1.25x or 2x base input by their `ephemeral_5m` / `ephemeral_1h` split. A trailing `+` means part of a long transcript is still unread, so the figure is a lower bound; `?` means a model id was not recognised and was priced as Opus 5.5. The cache lifetime is the one the newest main-conversation write used, from its usage split; when the transcript has none, Claude Code's `prompt_cache.ttl` from stdin; when neither exists, no cache marker is shown. The countdown runs from the newest request that read or wrote the cache. The line never shows a percentage.

Speed and bounds: the script reads only what was appended since its last run, tracked by a per-session state file (`$XDG_STATE_HOME/ork-meter/statusline/<sha256(session id)>.json`, 0600 in a 0700 directory, offsets and running totals, no content; files unused for 14 days are deleted when a new session starts). A transcript first seen over 2 MiB is read from its last 2 MiB, so the live figures are right at once, and its earlier part is read forward on later runs, at most 4 MiB a run across all files. It doesn't import the hook runtime. Measured on one Mac (Apple silicon, Python 3.15), on an 8.7 MB transcript with 33 subagent transcripts: once caught up, a median of 35 ms a run (90th percentile 39 ms) including interpreter start-up, which alone took 22 ms; the first run on that session, reading 4 MiB, took 57 ms. Any error prints `ork-meter` and exits 0. Tests: `tests/statusline_regressions.py`, on synthetic transcripts; it was not run inside a live Claude Code session, and Windows runs only in CI (Git Bash and native Python), not by hand.

### Loop guard (`hooks/loop-guard.py`)

`PostToolUseFailure`, every tool. The session store's `failures.json` is keyed by the SHA-256 of `tool_name` plus the JSON of `tool_input`; the input is not stored. Interrupted calls don't count. At `ORK_METER_LOOP_WARN` failures (default 3, and again at each multiple) the hook returns a `systemMessage` for the person and an `additionalContext` stating the count for Claude. When `ORK_METER_LOOP_STOP=N` is set to a number and the count reaches N, it returns `continue: false` with a `stopReason`; unset, `off` or `0` never stops. Counters reset at session start and after compaction. Each update holds `failures.json.lock` (an `O_EXCL` file, `session._lock`; at most 0.5 s of waiting, then the update runs unlocked as before), so parallel identical failures are all counted. Stopping is evaluated independently of the warning threshold.

### Setup audit (`scripts/audit.py`, `/ork-meter:audit`)

Lists every `CLAUDE.md`, `CLAUDE.local.md`, `.claude/CLAUDE.md` and `.claude/rules/*.md` from the working directory up to the root, plus `~/.claude/CLAUDE.md` and `~/.claude/rules/*.md`, with bytes, lines and a bytes/4 token estimate (a project under `$HOME` walks up through `~`; its `~/.claude` files and settings are counted once, as the user's, by real path); flags any CLAUDE.md over 200 lines; lists MCP server names from `~/.claude.json` (global and this project), `.mcp.json` up the tree and the settings files, never their commands, arguments or environment; ranks the files by size. Nothing is changed.

Two recommendations, each printed with the counts it is based on (`--json`: `recommendations[].basis`):

- **Code-intelligence (LSP) plugins.** A walk of the project (at most 20,000 entries; hidden directories and `node_modules`, `vendor`, `target`, `dist`, `build`, `out`, `venv`, `__pycache__`, `Pods`, `third_party` skipped) counts source files by extension and notes config files by name: TypeScript (`tsconfig.json`), Go (`go.mod`), Rust (`Cargo.toml`), Java (`pom.xml`, `build.gradle`), Kotlin, C# (`*.csproj`, `*.sln`), C/C++ (`compile_commands.json`, `CMakeLists.txt`), Swift (`Package.swift`) and type-checked Python (`mypy.ini`, `.mypy.ini`, `pyrightconfig.json`, `py.typed` or `.pyi` stubs; plain `.py` files don't count, and `pyproject.toml` would have to be read). A language counts with a config file or at least 5 source files, and the matching plugin from `claude-plugins-official` (`typescript-lsp`, `gopls-lsp`, `rust-analyzer-lsp`, `jdtls-lsp`, `kotlin-lsp`, `csharp-lsp`, `clangd-lsp`, `swift-lsp`, `pyright-lsp`) is recommended unless `enabledPlugins` in the user's or project's settings already enables it. No file is opened.
- **A 1-hour prompt cache.** From this project's transcripts (`scripts/cache_report.py`'s reader: usage counters and timestamps only), main-conversation requests only: recommended when some wrote the 5-minute cache, none wrote the 1-hour one (a subscription within its limits already has it), at least 3 came after a 5-60 minute pause and re-wrote at least half their context without reading the previous prefix (no compaction between), and at API list prices those re-writes cost more than the 1-hour cache's write premium (0.75x base input on every 5-minute write) would have. The advice names `"promptCacheTtl": "1h"` in `~/.claude/settings.json` (or `CLAUDE_CODE_PROMPT_CACHE_TTL=1h`). The dollar figures are estimates; the counts are measured.

It also prints whether the context-size nudge is on by default for a new session in the project and the billing signal behind it.

### Savings ledger (`hooks/lib/ledger.py`) and `/ork-meter:savings`

Every intervention and every event appends one JSON line to the session store's `savings.jsonl` (0600):

```json
{"ts": "…Z", "session_id": "…", "tool": "Bash", "kind": "trim", "bytes_before": 21725, "bytes_after": 12005,
 "est_tokens_before": 5432, "est_tokens_after": 3002, "est_tokens_avoided": 2430}
```

Common fields include reducer and policy versions as well as the counts shown above. `est_tokens_* = ceil(bytes/4)`. `bytes_after` includes any `additionalContext` the hook added. No tool content is ever written to the ledger; a `dedupe`, `read_guard` or `read_compact` line carries `path_sha256`, not the path. A line written for a tool call (by `ctx.record` in a hook whose payload has a `tool_use_id`) also carries `tool_call_sha256`, the SHA-256 of that id, never the id itself, so two lines for one call can be told apart from two identical interventions on different calls. Lines written outside a tool-call hook (`scripts/command.py`, `scripts/artifact.py`, session events) and lines from earlier versions have no `tool_call_sha256`.

The kinds are declared once, in `ledger.KINDS`, with their order, and each kind has one constructor in `hooks/lib/ledger.py` that builds its line. In production, `ctx.record(row)` adds `ts` and `session_id` and stages the row until reply emission. Bash byte accounting includes the final recovery note exactly once, after the last filter or trim stage. Byte interventions describe emitted transformations and carry the bytes before and after; count-only events record local events with zero bytes. Host acceptance is verified separately. The label is what `/ork-meter:savings` prints for a byte intervention, and the event's name otherwise. `tests/ledger.test.sh` checks that this table matches `KINDS` and prints the expected table when it doesn't:

| Kind | Counts as | Label | Written by | Fields beyond the common ones |
|---|---|---|---|---|
| `trim` | byte intervention | trim | `ledger.trimmed` in `hooks/trim-tool-output.py`, `hooks/dedupe-reads.py`, `scripts/command.py`, `hooks/compact-records.py` | none |
| `test_filter` | byte intervention | test_filter | `ledger.test_filtered` in `hooks/trim-tool-output.py` | none |
| `dedupe` | byte intervention | dedupe | `ledger.deduped` in `hooks/dedupe-reads.py` | `path_sha256` |
| `read_guard` | byte intervention | read_guard | `ledger.read_guarded` in `hooks/read-guard.py`, `hooks/dedupe-reads.py` | `path_sha256` |
| `read_compact` | byte intervention | read_compact | `ledger.read_compacted` in `hooks/dedupe-reads.py` | `path_sha256`, `lines_elided` |
| `delta` | byte intervention | delta | `ledger.delta_delivered` in `hooks/trim-tool-output.py`, `hooks/dedupe-reads.py` | `hunks` |
| `recovery` | byte intervention | recovery | `ledger.recovered` in `scripts/artifact.py` | none |
| `haiku_agent` | count-only event | Haiku agent run | `ledger.agent` in `hooks/agent-stop.py` | `agent_type`, `measured_status`; when known: `measured_input_tokens`, `measured_cache_read_input_tokens`, `measured_cache_creation_input_tokens`, `measured_output_tokens`, `measured_assistant_messages` |
| `cache_nudge` | count-only event | cache nudge shown | `ledger.cache_nudge` in `hooks/trim-session.py` | `context_tokens`; when known: `est_cache_write_usd` |
| `cache_nudge_accepted` | count-only event | cache nudge followed by /clear | `ledger.cache_nudge_accepted` in `hooks/trim-session.py` | `context_tokens`, `nudged_session_id`; when known: `est_cache_write_usd_avoided` |
| `context_nudge` | count-only event | context nudge shown | `ledger.context_nudge` in `hooks/context-nudge.py` | `context_tokens` |
| `context_nudge_accepted` | count-only event | context nudge followed by /clear or /compact | `ledger.context_nudge_accepted` in `hooks/trim-session.py` | `context_tokens`, `nudged_session_id`, `action` |
| `policy_fallback` | count-only event | session settings unusable: savings off | `ledger.policy_fallback` in `hooks/lib/policy.py` | `reason` |
| `stale_guard` | count-only event | stale-context prompt held | `ledger.stale_guard` in `hooks/stale-context.py` | `context_tokens`, `idle_minutes` |
| `stale_guard_continued` | count-only event | stale-context prompt resubmitted as is | `ledger.stale_guard_continued` in `hooks/stale-context.py` | `context_tokens`, `idle_minutes` |
| `loop_warning` | count-only event | loop guard warning | `ledger.loop` in `hooks/loop-guard.py` | `failures` |
| `loop_stop` | count-only event | loop guard stop | `ledger.loop` in `hooks/loop-guard.py` | `failures` |

`ledger.summarise` is the one summary of a ledger. Per kind it gives `count`, `bytes_before`, `bytes_after`, `bytes_avoided`, `est_tokens_avoided` and `counts_as` (`intervention` or `event`). Its `totals` count **interventions as byte-intervention lines only**; count-only events are counted separately in `totals.events` and never added to the interventions or the byte and token totals. A kind the code doesn't know (another version's, or a hand-edited line) counts as an intervention when its lines carry bytes. A byte-intervention line whose `tool_call_sha256` and kind repeat an earlier line's is a duplicate (one call's output is replaced once): only the first is counted, and the rest are left out of `by_kind` and every total and reported as `totals.duplicate_call_rows` and `totals.duplicate_call_bytes`; `/ork-meter:savings` prints a "Duplicate rows" line when there are any. Lines without a call id are never duplicates. The summary's `note` says the token figures are estimates. `hooks/session-summary.py` writes that summary as `estimated_tokens_avoided`, and `scripts/savings.py` renders it: the table has one line per byte-intervention kind and a total line equal to `totals`; for one session the events follow (Haiku agent runs, cache nudges, loop guard); `--json` prints the same summary as `estimated`, so its totals and the printed total agree. Both forms carry the sentence "Estimated with a 4-bytes-per-token heuristic from avoided tool-output bytes. Not measured by the model's tokenizer and not a billing figure." `--all` aggregates every session found in either layout of the session store (see below), tempdir-fallback sessions included. `--session <id>` finds a session in either layout.

### Activation report (`scripts/activation_report.py`, issue #300)

`/ork-meter:savings` answers "what did the hooks do in this session". The activation report answers "which features fire in real work", across every session store on the machine, grouped by plugin version. It is a developer script, not a skill:

```bash
python3 -I scripts/activation_report.py                       # markdown tables
python3 -I scripts/activation_report.py --json                # the same aggregates as JSON
python3 -I scripts/activation_report.py --since 2026-10-21 --until 2026-10-27
```

It finds stores with `Store.all(env, every=True)`: both layouts (scratchpad and the tempdir fallback `<tmp>/claude-ork-meter-<uid>/<session>/ork-meter`), with or without a ledger, so a session in which nothing fired still counts as seen. `--temp-base <dir>` (repeatable) replaces the temp directories searched, for a copy of another machine's stores or for tests. From each store it reads, with `session.read_bounded` (no links followed, a size cap) and only when the store directory is owner-only (0700) and the file is this user's own 0600 file:

| File | What the report uses |
|---|---|
| `savings.jsonl` | per valid line: `kind`, `ts`, `est_tokens_avoided`, `reducer_version`, `policy_version`, and `agent_type` for the three parked agents; malformed or invalid lines (`ledger.valid_row`) and duplicate tool-call lines (`ledger.split_duplicates`) are left out and counted |
| `session-policy.json` | `plugin_version` (the group); a store without one is `unknown` |
| `session-summary.json` | `ts` and `features`, the levers on at session end |

Per plugin version it gives the reducer/policy versions seen in the ledger, sessions seen, sessions with a ledger and with at least one firing, the first and last recorded time; per kind, the events, the sessions with at least one, and for byte interventions the sum, median and 90th percentile (nearest rank) of `est_tokens_avoided` per firing; the nudges shown and followed up as recorded (`cache_nudge_accepted`, `context_nudge_accepted`, `stale_guard_continued`, `loop_stop`); Haiku agent runs; and how many sessions had each feature on. `--since` and `--until` take a UTC date (`--until` includes that day) or a UTC time: a ledger line counts when its `ts` is in range, a session when any of its recorded times is (a store with no recorded time uses its policy file's modification time).

The output holds aggregate numbers, version strings, kind and feature names and UTC times only: no session id, path, hash of a path, prompt or tool content. A version, kind or agent name that doesn't look like one is replaced by `unknown` or `other`. Counts are absolute: no percentages and no savings claims (by policy); the token figures are the ledger's ceil(bytes/4) estimates. Files that could not be read are counted by reason (`not_read`), never named.

Not recorded by the plugin, so not in the report: **skill invocations** (no hook sees `/ork-meter:explore`, `/ork-meter:map` or the other skills being used), agent runs other than the parked Haiku agents, and each feature's fixed cost per request. Counting skill use would need a new hook on the `Skill` tool and a new ledger kind; until then, skill invocation counts are not available.

#### Collecting Phase 0

Phase 0 of #300 measures activation in real use at no benchmark spend:

1. Install the plugin version under study in the crew's normal Claude Code setup and leave the defaults (and any opt-ins the plan names) as they are. Note the version and the start date.
2. Work normally for five working days. Check once that the account is entitled (Ork Meter Plus gate, below): without it every lever is off and nothing fires. Nothing else to switch on: the hooks write the ledger, the policy pin and the session summary in every session. Stores live under the temp directory and are pruned with it, so take the report before a reboot clears them, and at the end of each day if the machine reboots often.
3. Run `python3 -I scripts/activation_report.py --since <start date>` (add `--json` for the record). Paste only the tables into the issue. Run it on each machine the crew uses; the reports add up per version because they are counts.
4. Read the result per version: a kind with no sessions with a firing never acted; a nudge shown but never followed is unaccepted. Skill use is not in the report (above).

### Measurement (`hooks/lib/measure.py`, `hooks/session-summary.py`)

The hook runtime serializes, writes and flushes its reply before committing
staged byte interventions. A serialization/handler/stdout failure, or a handler
returning no replacement, cannot claim avoided bytes for that invocation.
Count-only local events can commit without a reply. If a ledger append fails
after emission, the safe replacement remains delivered and a private
`accounting-gaps/` record identifies the planned and written stage counts
without content or paths. `/ork-meter:savings` reports partial accounting in
text and JSON, including gap-only sessions in `--all`. If even the gap record
cannot be stored, persistence is unavailable; emitted data is still preserved.
These statistics describe hook emission, not independently proven host
acceptance or billed savings. Direct test contexts retain immediate writes;
production hooks use staged accounting through `hook.run`.

The avoided-byte/token columns are gross reduction diagnostics. Signed net
fields telescope recorded byte interventions and subtract recorded recovery:
`net_bytes_avoided = bytes_before - bytes_after` over those intervention rows,
and `est_net_tokens_avoided = net_bytes_avoided / 4`. Negative results are kept.
For example, reducing 1,000 bytes to 100 and recovering 1,500 yields gross
900 avoided bytes but net -600 bytes (-150 estimated tokens). These local
estimates do not establish the native baseline and exclude prompt carry,
reasoning, unrecorded native Read recovery and other external work. Reports
and session summaries suppress net figures when accounting gaps are present.

Ledger read coverage is also explicit. `ledger.scan` reports malformed rows,
truncated tails, unreadable/missing files and invalid counters without logging
their contents. Surviving valid rows remain useful gross diagnostics, while
coverage is partial/unavailable and net estimates are suppressed. A writer-side
gap marker is not required to detect a later damaged ledger. The legacy
`ledger.load` is a row-only projection; reports and session summaries use scan
coverage instead of interpreting silently omitted recovery rows as zero cost.

Recovery CLI responses are also emitted before their local charge is recorded.
A failed recovery ledger append leaves the requested evidence and exit success
intact, writes the same independent accounting-gap evidence, and makes later
reports partial. If both persistence paths fail, stderr identifies unavailable
accounting; the CLI does not invent a recovery-byte total. Artifact range and
literal-search coordinates use physical LF/CRLF/CR boundaries, preserving
embedded form feeds and Unicode separators as content.

At `SessionEnd`, the hook writes the session store's `session-summary.json` with the ledger aggregate (estimates) next to the session's measured usage: the sums of `message.usage.input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` and `output_tokens` over the assistant lines of the transcript at `transcript_path`, counting each `message.id` once (Claude Code writes one line per content block, repeating the usage). The transcript shape was checked against a real transcript written by Claude Code 2.1.296 on 2026-10-13; a transcript that doesn't match it yields `"status": "unavailable"` with a reason, never a guess. A measurement needs a stable message identity and all four non-negative integer counters: distinct messages missing usage, and interrupted or malformed scans, make it `partial`; missing identities, invalid counters or conflicting copies of one message make it `unavailable`. Aggregate reports keep partial or unavailable session coverage instead of labelling the subtotal complete. SessionEnd hooks share a 1.5-second budget that plugin timeouts can't raise, so the scan stops after one second and says `partial`; `savings.py` finishes it later without a limit. `/ork-meter:savings` prints both, each labelled, and no ratio or percentage between them: the estimate is not a measured saving, and with `--all` the two can cover different sessions.

The honest summary: **the measured numbers are what the session used; the estimated numbers are what the hooks removed from tool results before the model saw them.** How much of the removed bytes would have cost tokens on later turns, and whether the model then needed to fetch them back, is not measured here. ork-meter #209's per-feature comparison in the customer's own ledger is where a saving can be measured.

### Active-lever file (`hooks/lib/features.py`, ork-meter #209)

On every `SessionStart` and `SessionEnd` the plugin writes `$XDG_STATE_HOME/ork-meter/plugin-features.json` (`~/.local/state/ork-meter/plugin-features.json` when `XDG_STATE_HOME` is unset; a relative `XDG_STATE_HOME` falls back to the home directory, matching the platform reader), directory 0700, file 0600. Its payload is:

```json
{"plugin_version": "0.4.0", "features": ["audit", "cache_nudge", "dedupe", "loop_guard", "read_guard", "test_filter", "trim"], "updated_at": "2026-10-13T12:00:00Z"}
```

`features` is the sorted list of levers from the lever registry (`features.LEVERS`) that are on. The plugin runtime (its reader is ork-meter `internal/pluginstate`) may read it and tag usage snapshots with it, so that a customer's own ledger can compare periods with and without a lever. Nothing else goes in the file, and the plugin itself sends nothing.

It also writes each session's own levers to `session-features/<sha256(session_id)>.json`;
that file is the authoritative record for one session. The staged session-aware
capture integration uses only the session file selected by a local telemetry
routing hint and never sends the session identifier or its hash. The global
file lists the levers that are on in **every live session**: the intersection
of the session files refreshed within the last two hours whose session hasn't
sent `SessionEnd` (an empty marker in `session-features-ended/` records that;
a resumed session, or a hook that refreshes the session's file after
`SessionEnd`, removes it). With no live session it lists the writer's own
levers. Each write rescans under `plugin-features.json.lock`, so the result
doesn't depend on which session writes last. It is only as good as "live": a
session idle for more than two hours drops out (its levers no longer hold the
list down), and a session that crashed without `SessionEnd` keeps holding it
down for up to two hours, so the global file can't credit a lever only some
parallel sessions have on. Session files and markers older than 48 hours
(twice the reader's limit) are deleted.

The reader ignores, silently, any file that breaks one of these rules, so `tests/features-contract.test.sh` writes the file through `features.write_features_file` and checks each of them:

- a regular file, mode 0600 (no group or other bits), at most 4096 bytes, holding exactly one JSON object and nothing after it;
- exactly the keys `plugin_version`, `features`, `updated_at`; no others;
- `plugin_version`: a non-empty string matching `^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-[A-Za-z0-9.]{1,16})?$`. The plugin reads it from `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json` (or the plugin that holds `features.py` when that variable is unset) and writes `0.0.0` when the file can't be read, so the feature list still reaches the reader;
- `updated_at`: RFC 3339 UTC with a `Z` suffix and whole seconds (`2026-10-13T12:00:00Z`), not more than five minutes in the future and not older than 24 hours (the plugin rewrites the file at every `SessionStart` and `SessionEnd`);
- `features`: a list, sorted and without duplicates, at most 16 entries, each matching `^[a-z_]{2,32}$`; an empty list is valid.

Without Ork Meter Plus (below), `features` is empty: gated levers are reported as off.

### Ork Meter Plus gate (`hooks/lib/entitlement.py`, ork-meter #261/#264)

The savings levers are an Ork Meter Plus feature; free Ork Meter is visibility only. The plugin runtime asks the platform (`GET /v1/entitlement`) at most every 15 minutes and keeps the answer in `$CLAUDE_PLUGIN_DATA/entitlement.json`, owner-only, with exactly `{"optimize", "state", "until", "checked_at"}`; `state` is `promo`, `trial`, `active`, `past_due` or `free`. The hooks never touch the network. `features.gate` reads that file for the live environment of every hook (`hooks/lib/hook.py`, after the session policy is bound and before the reducer circuit) and for `scripts/command.py`:

- entitled means `optimize` is true and now is at most 24 hours past `until` (or past `checked_at` when `until` is null); the runtime keeps the last answer while offline, so after that the levers fail closed;
- no file (not connected), a malformed or non-owner-only file, `optimize: false` (`free`) or a stale answer turns every lever off: the hooks pass Claude's results through untouched and `plugin-features.json` lists no features;
- env opt-outs (`ORK_METER_TRIM=off` and the others) still switch levers off on top of the gate while entitled; no env switch turns a gated lever on;
- the `SessionStart` status line (startup and resume) says once: "Ork Meter savings are off: they are part of Ork Meter Plus. Upgrade at https://orkmeter.com/billing". When not connected the trim hook says nothing; the runtime's own line asks the person to connect.

Metering and capture do not read this file; usage keeps flowing either way. The pure functions in `features.py` never read it, so their tests stay environment-only; hook tests run entitled through `tests/lib.sh` and `tests/entitled.py`, and `tests/entitlement.test.sh` covers the gate. A person who edits their own plugin can bypass a local gate.

Since the signed policy (next) exists, the unsigned `entitlement.json` alone decides nothing after `LEGACY_UNTIL` (2026-11-30T00:00:00Z, `hooks/lib/entitlement.py`): until then it keeps the rules above when no envelope is present, so an older runtime still entitles; from then on it counts for status lines only.

#### Signed policy (`hooks/lib/plus_policy.py`, `hooks/lib/entitlement.py`, ork-meter `docs/plus-policy.md`)

The entitlement and the levers' settings come from the platform inside a signed envelope, so nothing on disk is trusted without the platform's signature and a fork keeps only the built-in defaults. The runtime asks `GET /v1/plus/policy/claude-code` on the same cadence and writes, owner-only, into `$CLAUDE_PLUGIN_DATA`:

- `plus-policy.json`: the envelope bytes verbatim (`{"payload", "signature", "key_id"}`, at most 8 KiB; the payload is the exact bytes signed, never re-serialised);
- `connection.json`: exactly `{"reference": "<public pairing reference>", "origin": "https://..."}`, the pairing the envelope is bound to; the runtime removes both files on disconnect;
- `entitlement.json`: the four plain fields of the verified entitlement, for status lines and older readers.

`hooks/lib/plus_policy.py` is the contract's reference verifier, vendored byte for byte (`tests/plus-policy.test.sh` pins its SHA-256; Ed25519 in pure Python, about 4 ms, so every hook verifies on every read). On each read `entitlement.resolve` checks, in order: the envelope's shape and size; a known `key_id` and a valid signature over the payload bytes; the payload's eight fields and version; that `issuer` is both the pairing origin from `connection.json` and the origin the key is bound to, `plugin` is `claude-code` and `audience` is the pairing reference; that `issued_at` is not more than 5 minutes ahead and now is at most `min(expires_at, issued_at + 24 h 15 min)`; then the entitlement rule above (`optimize`, 24 h past `until`). Any failure, a missing `connection.json` or an envelope for another audience, origin or plugin means not entitled, every lever off. No envelope and no files means not connected; `connection.json` alone (paired, nothing cached yet) too. The test fixtures under `tests/fixtures/plus-policy/` are the contract's, signed by its test key, which is bound to `https://plus-policy-test.invalid` and can sign nothing a real pairing accepts; `tests/plus_policy_sign.py` signs fresh ones for the hook tests.

While entitled, the envelope's `policy` replaces the built-in defaults (`features.gate` threads it through `RuntimeEnvironment.policy`; `features.enabled` and `features.value` consult it):

- a lever is available only where `policy["levers"][name]` is true; missing means off. Environment and project settings keep their meaning on top and can only turn levers off relative to the policy: an opt-out still switches a lever off, an opt-in lever (`stale_guard`, `delta`, `context_hygiene`) needs the policy *and* its switch, and nothing in the environment turns on a lever the policy leaves off;
- `policy["tunables"]["lever.key"]` (an integer) is the default the environment variable would otherwise take, still clamped by the floor and ceiling in `LEVERS`; an explicit environment value still wins. A session's pin records these defaults at its first hook, like the built-in ones, so a changed policy applies at the next session; the lever switches apply at once;
- unknown levers or tunables are ignored; a policy that names none turns every lever off.

The `SessionStart` line names the active policy ("Policy claude-code-defaults-2026-10-22.") and says when the policy, rather than `ORK_METER_TRIM`, turned trim off. Without a policy (the legacy window) the built-in defaults apply as before.

### Hook runtime (`hooks/lib/hook.py`, `hooks/lib/run.py`)

Every Python hook above runs through one runtime. `hooks/py.sh <name>.py` (the command in `hooks/hooks.json`) finds a Python 3 and runs `python3 -I --check-hash-based-pycs always -X pycache_prefix=$CLAUDE_PLUGIN_DATA/pycache hooks/lib/run.py hooks/<name>.py`, so compiled bytecode is reused between calls and an unchecked-hash `.pyc` is never trusted over its source. That directory is owner-only (0700); when `CLAUDE_PLUGIN_DATA` is not an absolute path, is someone else's or is group- or other-writable, or the cache directory is a link, someone else's or can't be made, `-B` replaces the option and nothing is cached. The two prompt hooks end in `run.py` before the hook library is imported when their lever is certainly off from the hook's environment alone (where Claude Code has already merged user and managed settings; a project can only turn a lever off): `stale-context.py` when `ORK_METER_STALE_GUARD` is not on, and `context-nudge.py` only when `ORK_METER_CONTEXT_HYGIENE` is set to an off spelling, since unset it can default on per token. Both also end there when `ORK_METER_MODE` reads as off. Otherwise `run.py` loads the hook by path and hands it to `hook.run`, the only place the hook contract lives. The module docstring of `hooks/lib/hook.py` states that contract once: event guard, early exit, session pin, Plus gate, reducer circuit, mode, opt-out, deadline, staged accounting and fail-open (any exception: nothing printed, exit 0). Every threshold comes from `features.value`. A hook module imports no `sys`/`json` plumbing. The time caps: 5 s per Python hook in `hooks.json` (a finished reply is printed until 4.5 s after `py.sh` started, `EMIT_LIMIT_FROM_LAUNCH`; `py.sh` passes its launch time from bash 5's `$EPOCHREALTIME`, Git Bash included, in `ORK_HOOK_LAUNCHED_AT` to the hook's Python only, and `run.py` removes it from the environment at once and ignores a value in the future or more than 30 s old. Without it, as on macOS's bash 3.2, the limit is 4 s after the interpreter started, `EMIT_LIMIT`, a full second under the kill because bash and interpreter start-up are unseen), the 1.5 s budget inside (the transcript scans in `agent-stop.py` and `session-summary.py` stop at `min(1.0, ctx.remaining())`), and the same 5 s for `hooks/session-start.sh` and `hooks/python-check.sh`, which start a process tree (the runtime, or the hooks' Python loading the tokenizer) that a cold Windows host can take seconds to start: Claude Code cancelled the check at its former 2 s.

The hooks need `python3` on `PATH`, 3.10 or later: the plugin has one Python floor, the same one `scripts/build_runtime.py` states, and carries no compatibility code for older interpreters. Without `python3` the hooks do nothing and the session runs as before.

The skills `/ork-meter:savings` and `/ork-meter:audit` run their scripts through the same wrapper: `hooks/py.sh --script scripts/savings.py …` execs `python3 -I` with the same bytecode option on the path under the plugin root, without the hook runtime. A script is the opposite case from a hook: the person asked for its output, so its stderr stays visible, its exit status is passed through, and a missing `python3` is reported on stderr with exit 1 rather than swallowed.

### Session store (`hooks/lib/session.py`)

One module knows where a session's files are and what they are called; nothing else spells a file name (`tests/session-store.test.sh` checks that). A `session.Store` is the session's `ork-meter/` directory, in one of two layouts:

| Layout | Directory | When |
|---|---|---|
| scratchpad | `${scratchpad_dir}/ork-meter`, where Claude Code's `scratchpad_dir` is `<tmp>/claude-<uid>/<project>/<session_id>/scratchpad` | Claude Code 2.1.257 and later send `scratchpad_dir` |
| tempdir fallback | `<tmp>/claude-ork-meter-<uid>/<session_id>/ork-meter` (`<tmp>` is Python's `tempfile.gettempdir()`; an odd session id is replaced by a hash) | the payload has no `scratchpad_dir` |

`Store.for_payload(payload)` applies that rule and creates the directory (0700); `Store.find(session_id, env)` looks for a session in both layouts; `Store.all(env)` lists every store that has a ledger, in both layouts, sorted by path (`every=True` keeps stores without one, and `bases` replaces the temp directories searched). `/ork-meter:savings --session` and `--all` use those two, so a fallback session is found like any other.

The files, directories 0700 and files 0600, written atomically (temp file beside the target, then `os.replace`):

| File | Written by | Read by |
|---|---|---|
| `savings.jsonl` | `ctx.record` in every hook (append) | `session-summary.py`, `scripts/savings.py`, `scripts/activation_report.py` |
| `session.json` (session id, transcript path) | `ctx.store()`, once per session | `scripts/savings.py` (to measure the transcript later) |
| `reads.json` | `dedupe-reads.py` | `dedupe-reads.py`; removed by `trim-session.py` on every start and after compaction |
| `delta-bases/<sha256>.txt` and `.json` (the earlier full result per agent + tool + command/path, with its hash) | `dedupe-reads.py`, `trim-tool-output.py` when `delta` is on | the same hooks; removed by `trim-session.py` on every start and after compaction, and by `session-summary.py` at `SessionEnd`; output under 2 KiB is never written |
| `failures.json` | `loop-guard.py` | `loop-guard.py`; removed by `trim-session.py` on every start and after compaction |
| `tool-output/<sha256>.txt` and `.json` | Tool-output and Read reducers | Claude through the recovery path or `/ork-meter:recover`; pruned by `hooks/lib/retention.py` at SessionStart and SessionEnd (7 days, 256 MiB across sessions oldest first but never a file younger than 24 hours, the session's own on `/clear` or logout; only the pruning session is exempt, other running sessions are not) |
| `session-summary.json` | `session-summary.py` | `scripts/savings.py`, `scripts/activation_report.py` (`ts`, `features`) |

`$XDG_STATE_HOME/ork-meter/` (`plugin-features.json`, `cache-nudges/<safe-session-id>.json`) is per user; pending nudges are scoped to the originating session; `features.state_dir()` owns that path and the files there are written with the same `session.write_private`. Per-session state files (`cache-nudges/`, `context-nudges/`, `stale-guard/`) untouched for 30 days are removed at a startup `SessionStart`.

A store stays put when the scratchpad field appears or disappears: an existing
fallback store takes precedence over a later scratchpad, and an empty
scratchpad never hides an existing ledger. Private writes use exclusive random
staging files and refuse linked or foreign-owned destination directories.
`session.json` is initialized with an atomic create-if-absent hard link to an
already-written private sibling, so concurrent hooks reuse the winning complete
document instead of racing to replace it; an unsupported filesystem fails open
at the hook boundary. On Windows every filesystem boundary (private directory
checks, hard-link publication, reads, atomic replacement, ledger access,
diagnostic scans) uses the extended-length path form for deep paths, as
[CreateHardLinkW requires](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createhardlinkw),
while display and recovery handles keep their ordinary spelling.

### Project map (`scripts/project_map.py`, `/ork-meter:map`, issue #109)

Exploration turns re-send the conversation each time, so a repository that Claude has to learn by listing directories and opening files costs tokens on every task. Anthropic's cost guidance suggests a skill that describes the architecture instead. `/ork-meter:map` builds one locally, only when the person runs it, and `/ork-meter:map` again refreshes it.

**Where it lives, and why.** The map is written to `<project>/.claude/skills/ork-meter-map/SKILL.md`. Claude Code discovers project skills in `.claude/skills/<name>/SKILL.md` in the start directory and its parents up to the repository root, lists only their descriptions, and loads a skill body only when it is used ([Skills: "Choose where skills load", "Control who invokes a skill"](https://code.claude.com/docs/en/skills)). Plugin skills come only from the plugin's own `skills/` directory, which an update replaces and which every project shares, and `${CLAUDE_PLUGIN_DATA}` is a data directory, not a skill location; so a per-project map kept there would not be discovered. Claude Code watches `.claude/skills/` for changes, so a refresh is picked up in the running session; if `.claude/skills/` did not exist when the session started, `/reload-skills` (or a restart) is needed, and the command says so. The generated skill is model-invocable (no `disable-model-invocation`), unlike the plugin's own skills: loading it on demand is its purpose. Its fixed, ~300-character description is listed in every session in that project; its body (at most 6 KB) costs nothing until Claude loads it.

**What it writes.** Exactly two files: that `SKILL.md`, and a `.gitignore` containing `*` beside it, so git ignores the generated skill unless the person deletes the `.gitignore` to commit it. It refuses to overwrite or remove a `SKILL.md` without its marker, and refuses to write through a `.claude`, `skills` or `ork-meter-map` that is a symlink or not a directory. `/ork-meter:map preview` prints the map and writes nothing; `/ork-meter:map remove` deletes only what it wrote.

**What it reads.** Files inside the project directory (`--cwd "${CLAUDE_PROJECT_DIR}"`), opened with `O_NOFOLLOW` and checked to resolve inside it; symlinks are counted and never followed. Ignore rules: nested `.gitignore` files and `.git/info/exclude` (a subset of git's syntax: negation, anchoring, directory-only and `**`; the user's global excludes file lives outside the project and is not read). Skipped without being opened: `.git`, vendored and build directories (`node_modules`, `vendor`, `dist`, `build`, `target`, virtualenvs, caches), lockfiles and generated files (`*.min.js`, `*_pb2.py`, binaries), credential directories (`.ssh`, `.aws`, `.gnupg`, `.azure`, `.kube`, `.docker`, in any letter case) and secret-looking names (`.env*`, `.envrc`, keys, certificates, `id_rsa`, `.netrc`, `.npmrc`, kubeconfigs, Terraform state, `.vault-token`, `token.txt`, names containing `secret`, `credential` or `password`). Of what remains it reads manifests (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `Makefile`, `justfile`, ...) up to two levels down, the root README's headings, and top-level public classes and functions of Python files through the same AST walk as `scripts/source.py` (other languages get no outline). Each read is capped at 256 KiB and the walk at 50,000 files.

**Bounds and determinism.** Sections are filled in priority order (summary, commands, entry points, manifests, layout, conventions, README outline, Python symbols), each with a line cap, and the whole file is capped at 6,144 bytes; every cut says how many lines were omitted. No timestamps or modification times are used and everything is sorted, so the same tree gives the same bytes and an unchanged repository reports `Unchanged`. Repository text is reduced to single inert lines: no backticks (so no `` !`command` `` placeholder Claude Code would execute when loading the skill), no control characters and no HTML comments, and the frontmatter never contains repository text.

**Privacy.** No model, no network. The map never leaves the machine and is never sent to Ork Meter; the plugin's feature file and ledger record nothing about it.

**Not yet measured.** Issue #109's acceptance asks for a shadow replay comparing exploration tokens with and without the map; that is a follow-up. Until then there is no savings claim. Also unverified in a live session: that the installed CLI lists the generated skill and that Claude chooses to load it when orienting.

### Lean main agent (`scripts/lean_agent.py`, `/ork-meter:lean`, issue #108)

The plugin trims context; it does nothing about the number of turns. Cost is roughly turns times context, because every turn re-sends the conversation. `docs/optional-agents/lean.md` is a main-session agent, `ork-meter-lean`, whose prompt asks for fewer turns and smaller reads: delegate an open, many-file question to a read-only subagent (the built-in Explore agent) and ask for `path:line` findings; use the `ork-meter:explore` skill's search and batch reads when it is available, else make independent Grep, Glob and Read calls in one turn; search, then Read ranges with `offset` and `limit`; don't re-read a file already in context unless it may have changed; plan related edits and make them together. It must not trade correctness for tokens, so it also says to read the code being changed (and enough of its callers and tests) before editing, to run the relevant tests and to report failures. It names the skill, not a script path, because `${CLAUDE_PLUGIN_ROOT}` is not substituted in an agent file copied out of the plugin. It sets no `tools` or `model`, so it inherits the session's.

**Opt-in only.** A plugin can make an agent the session default through `settings.agent`, but that would switch the main agent for everyone who enables the plugin, so the plugin does not use it, and `agents/` stays empty (an agent there is listed in every session). `/ork-meter:lean install` (explicit-only, like every skill but `explore`) copies the definition to `<project>/.claude/agents/ork-meter-lean.md` and, unless one exists, writes `.claude/agents/.gitignore` ignoring only that file and itself; a `.gitignore` of the person's is left alone and the output says how to add the line. It reuses the project map's safe writes: atomic writes, no writing through a `.claude` or `agents` that is a symlink or not a directory, and no overwriting or removing a file without the agent's marker comment. Re-running updates an older copy and is otherwise a no-op; `status` changes nothing; `remove` deletes only what it wrote and the directory only if nothing else is in it. It never reads or edits a settings file: the person starts `claude --agent ork-meter-lean`, or sets `"agent": "ork-meter-lean"` in `.claude/settings.local.json` themselves.

**What the CLI does with it** (checked with the installed CLI against the localhost fixture, `tests/native_lean_qualification.py`). Before install, `--agent ork-meter-lean` is refused and nothing reaches the model; after install the agent's prompt *replaces* Claude Code's default system prompt (environment details still arrive), which is why the agent restates the default prompt's essential ground rules (follow the person's and project's instructions, confirm hard-to-reverse actions, a denied call is a decision, report outcomes faithfully). `claude --append-system-prompt-file .claude/agents/ork-meter-lean.md` instead keeps the default prompt and appends the rules; the test checks that too. Installing adds one cost to every session in that project, used or not: the agent's one-line description is listed among the available agents (the description says not to delegate to it). After `remove` the agent is refused again. The test also checks that `~/.claude/settings.json` is unchanged.

**Not yet measured.** No savings claim. The comparison that counts is whole-task: turns, tokens (cache reads and writes included) and success rate per accepted task, with and without the agent, from a session benchmark (#109, #187) or shadow replay; report success rate alongside cost.

## Session policy and attribution

`hooks/lib/policy.py` atomically publishes one immutable metadata-only policy
under the session store. Parallel hooks use the same winning snapshot. The pin
contains canonical values from the lever registry, including defaults, rather
than a copy of the process environment. It contains no credentials or tool data.

Each automatic hook applies that policy and then immediate opt-outs. A pinned
disabled feature cannot be enabled by changing the environment; an explicit
opt-out can disable an enabled feature at once. `ORK_METER_LOOP_STOP=off` also
disables a pinned hard stop. Removing the override restores the pinned behavior.
Budget/threshold changes need a new native session identity. Clear, resume,
fork events that retain the same identity, and compaction do not discard the pin.

A pin written by a build with a different set of levers is still read: its digest is checked against the settings exactly as pinned, then
pinned values are kept, a lever switch the pin lacks reads `off` (an update
never starts a lever mid-session), a tunable it lacks reads its current
default unless it is an on/off control (unit `switch`), which reads `0`
whatever its default, and settings this build doesn't know are ignored, so a
plugin update never silently turns running sessions to pass-through. A pin that is corrupt, tampered, for another session or
plugin version, or without a mode still passes through, and the first such
fallback in a session is logged once as `policy_fallback` with its reason.
Explicit command capture shares an existing session pin when its store identifies
the session. Manually invoked capture without an identified session remains a
standalone operation using its current configuration.

A damaged, oversized, linked, incompatible-version or invalid policy restores
pass-through. Hooks do not overwrite damaged state or rerun tools. Start a new
session with a compatible plugin to obtain a fresh policy; this pins configuration,
not old executable code. Hooks without a valid session identity cannot persist
a pin and retain their standalone configuration semantics.

Local ledger rows carry `policy_sha256` for their actual effective settings.
SessionEnd summaries keep both that hash and `session_policy_sha256`, which
identifies the immutable base. `/ork-meter:savings --json` reports the saved
settings separately from the distinct hashes observed in recorded rows. Missing
policy identities remain unavailable; a report does not infer current behavior
from its own environment. Policy diagnostics never modify the saved pin.

Feature state retains exactly its three platform keys. It is refreshed when
effective levers change, on session boundaries and at least hourly during active
hooks, so an emergency opt-out is reflected even when the disabled handler is
skipped. Configuration hashes and settings stay local; the existing hosted
contract labels active levers only. Session-specific hosted attribution still
depends on the pending platform capture integration.

## Repeated internal reducer failures

`hooks/lib/circuit.py` tracks automatic reducer paths independently by tool.
Two distinct tool-call identities with the same internal failure category and
session policy disable that path for the session. Duplicate observations of a
call count once. Ordinary command failures, unsupported formats, closed stdout
and accounting-write failures do not count as reducer failures. A changed
execution-status field or an invalid serialized reply is rejected before
emission and counted as an internal fault.

Fault records contain scope, hashed call identity, policy hash, exception class
and time; they contain no tool inputs, outputs or raw exception messages.
Immutable atomic publication retains concurrent observations. A disable marker
keeps quarantine stable across compaction and later threshold changes. Damaged,
linked, oversized or excessive diagnostics bypass the affected path; reporting
does not repair or delete the evidence. Diagnostic reads are bounded.

A broken Bash output path leaves Grep, Glob, WebFetch and Read paths available.
Manual exact recovery stays available. The explicit command wrapper also returns
the already-captured original streams and child status if its reducer fails,
without another execution; subsequent calls bypass that reducer. Binary and
interrupted output remain unreduced. These controls do not certify recognition
of every possible schema or recovery failure.

Local rows carry a separate `reducer_health_sha256`; saved configuration and
changing fault state are distinct. `/ork-meter:savings` names disabled paths,
and its JSON/SessionEnd report contains reducer health and incomplete-diagnostic
status. Hosted feature state keeps its three-field contract: a partially
available lever remains available, and a wholly unavailable family is removed.
The hosted contract does not encode individual reducer scopes.

The installed-host fixture runs a failing Bash reducer twice, skips it on the
third call and still reduces Grep and Glob, with native tool status retained.
This is resilience evidence, not a token or cost saving.

## Bounded source batches

`/ork-meter:source` exposes `scripts/source.py --workspace PATH --batch JSON`.
The JSON array selects independent outlines, exact symbols/ranges or literal
searches. This can return a caller-selected target, imports and related tests
in one Bash invocation. It does not infer semantic dependencies or apply edits.

Paths resolve within the explicit task workspace, rather than the plugin's
working directory. Each resolved file has one byte snapshot within the call;
every selection includes its SHA-256. A later selection with `expected_hash`
rejects stale source. Python symbols preserve decorators and the entire body.
Malformed Python and other languages retain exact-range retrieval; search scans
physical lines and reports returned/omitted matches.

Batches allow at most 16 requests, 2 MiB per file and 8 MiB of unique source
snapshots. The output, including metadata, has a default 24 KiB UTF-8 budget
(configurable from 1 KiB to 1 MiB). An exact selection that cannot fit is withheld
whole with its path/hash and `output_budget_exceeded`; smaller independent
items can still be returned. `complete: false` and per-item errors make partial
evidence explicit. No persistent source index, source execution or network
operation is involved. A requested expansion uses a larger budget or exact
single-file retrieval and bypasses the normal Bash reducer.
Single-file retrieval reads only regular files that resolve, symlinks followed,
inside the current working directory or Ork Meter's own `tool-output` store;
anything else is refused. A script's reads are invisible to Claude Code's
`Read` deny rules, so this keeps the pre-approved command from reaching files
such as `~/.ssh` or `~/.aws/credentials`. For the same reason batch, single-file
and `--related` retrieval refuse, as a per-item error, any file that workspace
search would skip as secret-looking (`project_map.secret_path`: every `.env*`
name plus `env.local`, `.flaskenv` and `.dev.vars`, keys, certificates,
keytabs, kubeconfigs, Terraform state and `.terraformrc`, token files,
`.claude.json`, `.my.cnf`, `.netrc`/`_netrc`, `.pgpass`/`_pgpass`/
`pgpass.conf`, shell, `psql`, `mysql` and Python histories,
`local.settings.json`, `wp-config.php`, names containing `secret`,
`credential` or `password`; ordinary names in particular directories:
`gh/hosts.yml`, `.m2/settings.xml`, `.gradle/gradle.properties`,
`.vscode/sftp.json`, `sops/keys.txt` and `age/keys.txt`; and anything under
`.git`, `.ssh`, `.aws`, `.gnupg`, `.azure`, `.kube` or `.docker`), in any
letter case. The name rules apply to the path as written and to the resolved
path relative to the workspace. The directory rules (`secret_location`)
apply to the resolved absolute path and to the workspace itself as well, so
`--workspace ~/.docker` with `config.json`, or `~/.config/gh` with
`hosts.yml`, is refused too, in batch, single-file, related and search
retrieval and in the regex scan child. A project merely named like a secret
(`secret-santa/`) is not refused. On Windows a name is compared as Windows
would open it: trailing dots and spaces dropped, and an `:stream` suffix
removed. A hard link to a secret file under an innocent name is not detected:
it is the same file, with nothing in its path to recognise. The check and the
open are separate steps, so a file swapped in between is read. The person's
Read tool, under their permission rules, remains the way to read such a
file. `--workspace` may be
given only once and options can't be abbreviated, so text appended after a
pre-approved `--workspace "${CLAUDE_PROJECT_DIR}" --batch` or `--search`
prefix can't swap the workspace (argparse would otherwise keep the last one).

Emitted retrieval evidence belongs in actual task usage; it is never recorded
as avoided tokens in the local ledger.

`--related SYMBOL --workspace ROOT` adds bounded static Python dependency
selection to the same CLI. It returns exact decorated definitions, enclosing
context, referenced module assignments, same-file functions and resolvable
workspace imports. Nested function arguments, local imports, comprehensions,
lambda scopes and method scopes cannot suppress parent/module dependencies.
Rebound imports are reported as ambiguous rather than guessed. Up to eight
explicit `--references` Python files contribute literal reference/test bodies;
there is no automatic repository-wide test search. No workspace code is loaded
or executed.

Dependency traversal allows 0..2 hops and 1..16 returned selections (default 8),
at most 16 Python files, 2 MiB per file and 8 MiB total input. The same 24 KiB
serialized output budget withholds whole selections. Inspect issues, omission
counts and per-item status. `complete` describes this bounded selection only;
`semantic_closure` is always false. Each file has one independent snapshot and
hash; `--expected-hash` checks the primary file, not every dependency. Verify
each file's hash before edits. External/dynamic bindings, unsupported syntax
and other languages need exact source or lexical search.

Installed-host fixtures cover batch and related-context delivery across two
workspaces, stale and escape refusal, and intact selections above the Bash trim
budget. Source-token and task-quality targets are unmeasured (#202).

## Workspace search and the explore skill

Exploring code one Read or Grep at a time costs a model round-trip per call,
and each round-trip re-sends the conversation. `/ork-meter:explore` (#107)
searches the workspace and then reads the ranges it needs in two read-only
Bash calls.

It is user-invocable only (`disable-model-invocation: true`, #282): run
`/ork-meter:explore` with what to find, and its body loads then. Until #282 it
was the one model-invocable bundled skill, listed in every session so Claude
could choose it. Claude never did: 0 invocations and 0 explore Bash calls in
23 live sessions (P2b, P3a, and the re-probe of the #284 listing, whose
`when_to_use` named moments in a task: before reading 3+ files, to find where
a name is defined or used, or instead of `find`/`grep`/`cat | head` in Bash).
The fallback agreed in #282 removes its listing line from every request:
request 1 (system, tools and messages) goes from 51,649 to 51,394 bytes, 255
bytes less (the 253-byte line and its newline; about 62 `o200k_base` tokens),
with Claude Code 2.1.302 against a localhost fixture, and is then identical
to a session without the plugin. Claude Code no longer offers a skill with
this setting to the model, nor preloads it into subagents.

`scripts/source.py --workspace DIR --search QUERY` is literal and
case-sensitive by default (`--regex` for Python `re`, `--ignore-case`),
optionally restricted by repeatable `--glob` patterns. Output is compact text
by default and the full JSON record with `--json`. Files are ranked by a
deterministic score: lines that define the query (`def`, `class`, `function`,
`fn`, `func`, `type`, `const`, `X =`, `X :=` and similar) weigh most, then a
file name or directory containing it, then the hit count on a logarithmic
scale; ties sort by path. Each file is one snapshot, returned with its SHA-256
(usable as `expected_hash` in a later `--batch`), its hit line numbers and the
exact physical lines around them (`--context` 0..5, default 1, overlaps
merged). Up to `--per-file` hits per file (default 8, definitions first). A
snippet block identical to one a higher-ranked file already shows is not
repeated.

The text form is one summary line (files matched of files scanned, matches
total, shown, duplicate and not
shown; `complete`, with non-zero skip counts, or `incomplete`), then per file
a header `path sha256=<hex>` (`[def]` when it defines the query, `+N hits not
shown` past the per-file cap) and its lines: `N: text` for hits and `N- text`
for context, as grep prints them, with `--` between ranges when there is
context and the line terminator removed. A duplicate block is one line,
`N= path:M` (or `N..E=`), naming where it was shown. An incomplete result ends
with one `incomplete:` line: reasons, files withheld and omitted, matches not
shown and skip counts. Source text cannot forge a header: every source line
and dedupe note starts with its line number, and headers, the summary and the
footer never do (a path that could start with a digit, a dash or a quote, or
contains spaces, is written as a JSON string). A line containing a control or
Unicode line-separator character is written `N:"..."` as a JSON string. On
this repository, `--search may_emit --context 0` printed 2,040 bytes of text
against 4,608 of JSON for the same 8 files and 19 matches (`grep -rn` printed
1,708 bytes, without hashes or ranking). In JSON the first copy of a
duplicate block lists the other locations in `also_at` (eight, then a count),
the later file reports `deduplicated_lines` and, in `same_as`, where each of
its blocks is shown, and CRLF is kept exactly.

Bounds, all reported: the output budget (`--budget`, default 12 KiB, the Bash
trim budget; 1 KiB to 64 KiB, since a search is for orientation and exact
reading belongs to `--batch`; applied to the text or JSON actually printed,
with room kept for the summary at its longest) withholds whole files as a
path-and-hash stub
or, past that, only counts them; at most 20,000 files and 64 MiB are read;
files over 1 MiB, binary (a NUL byte) and non-UTF-8 files are skipped; and the
whole call is bounded at 2 seconds. The output always carries files scanned, skipped
by reason, matched, returned, withheld and omitted, matches total, returned,
deduplicated and omitted, and `complete` with `incomplete_reasons`
(`time_limit`, `file_limit`, `input_limit`, `walk_file_limit`, `per_file_cap`,
`output_budget`). Skipped files are counted but not searched; they do not by
themselves make a result incomplete.

The time bound is hard on every platform. A literal scan runs in process and
checks the clock between files and on every line. A regex scan runs in a
child process (`python -I -B scripts/source.py --scan-worker`, the same
file-reading and scanning code, given the walked file list on stdin) that
streams one JSON record per file; the parent reads until the deadline, then
kills and reaps the child, keeps every record it received, and reports the
file in progress as `interrupted_file` (`stopped in` in text), counted as not
scanned, with `time_limit`. Only a file the scan had opened is named; one
the clock stopped before is just not scanned. The deadline runs from the start
of the call, so a job write that blocks is cut off too. A child that dies or
prints a truncated line gives `worker_failed`, with the last 200 bytes of its
stderr (`worker_stderr`). The child also caps itself in case the parent is
killed first: on POSIX `RLIMIT_CPU` (5 s, or the time limit plus 3) ends even
a single runaway match, and on every platform a thread exits it once its
stdin, which the parent holds open for the whole scan, reaches end of file.
That thread runs between lines; it can't preempt one match, which holds the
GIL, so on Windows a parent killed outright can leave a child inside one
pathological match running until it ends. Any pattern that compiles (at most 256
characters) is accepted: no shape rule is needed, and none could be complete,
since a single quantifier such as `\w+\(` on one 300 KB line is already
quadratic. Measured on macOS through the CLI with the 2-second default:
`(a+)+$`, `\w*\w*\w*\w*\w*\w*!` and `.*.*.*.*.*.*x` on 80 characters,
`a*a*a*a*a*a*a*a*b` on 40, `\w{0,200}\w{0,200}\w{0,200}!` on 2,000, `\w+!`
on a 100,000-character line and `\w+\(` on a 300 KB JSON line each returned
in 2.1 to 2.2 s with `complete: false` (the #220 reviews had measured 5 s to
over 90 s for these). An ordinary regex search of this repository took 0.17 s
against 0.13 s literal; the child's start-up is the difference.

The file walk is `scripts/project_map.py`'s, loaded by path: `.gitignore`
files and `.git/info/exclude` are honoured; vendored, build and generated
directories and files, credential directories in any letter case and
secret-looking names (`.env*`, keys, certificates, credentials, the same
`secret_path` rules as above) are skipped without being opened; symlinks are counted and never
followed; each read uses `O_NOFOLLOW` and must resolve inside the workspace,
which must be an existing directory. Directory depth beyond 24 levels is not
walked. Nothing is executed and nothing touches the network. Because the
command is a `scripts/source.py` invocation, the trim hook passes its output
through unchanged, as it does for `--batch`; `$CLAUDE_PROJECT_DIR` is accepted
as an argument for that check (`hooks/lib/recovery.py`).

Why this shape: an MCP server would add its tool schemas to every prompt, and
a top-level `bin/` directory makes claude.ai organization sync reject the
plugin, so the feature is a mode of the existing script plus a skill. The
skill pre-approves only these two commands with the project directory as the
workspace; Edit and Write remain the way to change files, and the person's own
Read and Grep stay available.

**Not yet measured.** Fewer round-trips is the intent, not a measured result.
Whether Claude chooses the skill, and whether whole-task tokens fall, needs the
session benchmark (#109, #187); until then there is no savings claim.

## Facts confirmed before coding

From the hooks reference (https://code.claude.com/docs/en/hooks, fetched 2026-10-13):

- `PostToolUse` input has `tool_name`, `tool_input`, `tool_response` (the tool's structured output object, shape per tool), `tool_use_id`, `duration_ms`, plus the common fields `session_id`, `prompt_id`, `transcript_path`, `cwd`, `scratchpad_dir` (2.1.257+), `permission_mode`, `hook_event_name`.
- `hookSpecificOutput.updatedToolOutput` "replaces the tool's output with the provided value before it is sent to Claude. The value must match the tool's output shape"; "for built-in tools, a value that doesn't match the tool's output schema is ignored and the original output is used." The documented example for Bash is an object `{stdout, stderr, interrupted, isImage}`. The hooks reference documents only the Bash shape; the Read, Grep, Glob and WebFetch shapes above were read from the 2.1.295 binary's output schemas and renderers.
- `additionalContext` on PostToolUse is inserted "next to the tool result" as a system reminder; strings over 10,000 characters are written to a file and replaced by the path and a 2,000-character preview.
- `PreToolUse` `updatedInput` "replaces the entire input object, so include unchanged fields alongside modified ones"; permission rules are evaluated against the returned input.
- `SessionStart` matchers: `startup`, `resume`, `clear`, `compact`, `fork`; on `resume`/`fork` with at least one response, the input carries `seconds_since_last_response`, `context_tokens`, `prompt_cache_likely_expired`, `estimated_cache_write_usd` (2.1.251+).
- `PostCompact` exists, matchers `manual|auto`, input `trigger` and `compact_summary`, no decision control, `systemMessage` discarded. The plugin doesn't use it: `SessionStart` with source `compact` fires after both manual and automatic compaction (2.1.302, qualified natively), and the CLI appends each `PostCompact` hook's command text to the `/compact` result (`Compacted PostCompact [<command>] completed successfully`, where stock prints `Compacted `), #268.
- `PostToolUseFailure` input: `tool_name`, `tool_input`, `error`, `is_interrupt`; output `additionalContext`; universal `continue: false` + `stopReason`.
- `SubagentStop` input: `agent_id`, `agent_type`, `agent_transcript_path`, `last_assistant_message`; no usage fields.
- `SessionEnd`: hooks share a 1.5 s budget; "timeouts set on plugin-provided hooks don't raise the budget"; JSON output fields are discarded.
- `systemMessage` is shown to the person and not added to the model's context.

From the Claude Code 2.1.295 binary (strings, not executed):

- Hook output schema: `updatedToolOutput` is untyped at the hook layer and validated per tool; the Bash replacement is validated against `{stdout, stderr, interrupted, isImage, timedOutAfterMs, noOutputExpected}` with other keys stripped. The schema description says hooks "run in parallel on the ORIGINAL output, so an identity rewrite competes last-write-wins with sibling rewrites".
- Read output: `{type: "text", file: {filePath, content, numLines, startLine, totalLines, truncatedByTokenCap?}}`; the renderer numbers `content` from `startLine`. A native `{type: "file_unchanged"}` result exists (also in 2.1.284) and is produced when the earlier read of the same path, offset and limit is not a partial view, is still in the model's context, and the file's mtime is unchanged; it renders as "File unchanged since last read …".
- Grep output: `{mode, numFiles, filenames, content?, numLines?, numMatches?, …}`; Glob output: `{durationMs, numFiles, filenames, truncated, totalMatches?}`, rendered as the file names joined by newlines; WebFetch output: `{bytes, code, codeText, result, durationMs, url}`.
- A hook from the person's own files (or a plugin they installed) can have its `updatedToolOutput` and `updatedInput` ignored when the organisation sets `CLAUDE_CODE_RESTRICT_PERSONAL_CONFIG`; the plugin then does nothing, which is the intended failure mode.

From the tools reference: on macOS, Linux and WSL the Grep and Glob tools are not in the default tool set (Claude searches with `grep`/`find` through Bash), so most search output arrives as Bash results; the Grep and Glob matchers cover Windows and subagents that get those tools back.

## Not verified without a live session

The default suite feeds hook fixtures and checks preservation and recovery.
An opt-in installed-CLI qualification uses localhost synthetic inference:
`RUN_NATIVE_CLAUDE_TESTS=1 python -I -B tests/native_qualification.py`.
On Windows Claude 2.1.296 it verifies Read output replacement, the recovery note
in the next model request, explicit-range pass-through, session feature binding
and stable earlier message content. Claude moves its provider cache breakpoints
as the conversation grows; these directives are excluded from content equality.
This test makes no paid requests and does not establish actual cache savings.

`tests/native_output_qualification.py` additionally verifies installed-CLI
Bash, Grep and Glob replacements against stock, an installed no-op plugin and
shadow capture. It exposed and guards against a larger Grep replacement
defeating the native disk preview. Results at least 20 KiB that already exceed
their configured non-Bash budget use a 1 KiB preview budget. Exact fixture
evidence and limits are in `tests/native_output_qualification.py`.

WebFetch replacement, automatic compaction, resume, fork, continue, background
Bash results and control-protocol interrupts are qualified in the installed CLI
(#205; the one unqualified path is the terminal UI's Esc interrupt). Still
unverified by this qualification: retrieval skill permission matching; live hosted receiving and browser pairing; provider prompt-cache
reuse (#203); and whole-task correctness and billed-cost improvement (#202). SessionEnd timing remains covered by local
transcript scans, rather than a customer acceptance run.

## Known risks and mitigations

- **Claude needed the elided content.** Mitigation: the full output file and its path in the result; error-looking lines kept from the elided range; Read trimming keeps real line numbers and names the unshown range; the repeat-read note names the earlier result. The opt-out is one environment variable.
- **A passing-test line that wasn't noise.** The filter only removes lines matched by a narrow regex per runner and never removes failures, summaries or unrecognised lines; the note states the count and that every other line is shown, and the result names the saved full output.
- **The read guard pages a file Claude wanted whole.** It never touches a call that sets `offset`, `limit`, `pages` or `allow_large`, and the note tells Claude how to read the rest.
- **Claude needed an elided range, or reads an empty slot as an empty line.** Every elided range has a marker with its exact `offset` and `limit`, the note says the view is partial and the empty numbered lines are not empty in the file, and an Edit aimed at text that isn't in the file fails without changing it. `ORK_METER_READ_COMPACT=off` restores paging alone.
- **A Write rebuilt from a compacted view deletes the elided code.** The Write guard refuses it until the elided ranges have been read (see "Write guard"). Not covered: a Bash command that rewrites the file, and `Edit` with `replace_all`, which the note warns about.
- **Repeat-read note after the earlier result was compacted away.** The index resets on `SessionStart` with source `compact`, once per compaction.
- **A nudge the person doesn't want.** `ORK_METER_NUDGES=off`; thresholds are configurable. The context-size nudge is off on a subscription unless turned on (on by default only per token), rate-limited by growth, capped at three per session, and `ORK_METER_CONTEXT_HYGIENE=off` dismisses it.
- **A context-size nudge at a bad moment.** It is a suggestion to the person only; it never runs `/clear` or `/compact` and never adds to the model's context.
- **A loop stop in an unattended run.** The hard stop is off unless `ORK_METER_LOOP_STOP` is set.
- **Time.** Each Python hook is one process (about 25 ms here) capped at 5 s by `hooks.json`, with a 1.5 s internal budget from `hooks/lib/hook.py`; `hooks/session-start.sh` and `hooks/python-check.sh` have the same 5 s. On any failure a hook prints nothing and exits 0.

## Offline token admission

UTF-8 byte shrinkage can increase token use, and recovery notes can consume
small reductions. Post-tool replacements, read
paging and explicit command capture now require a 512-token reserve after
charging the complete replacement and additional context. Read paging compares
against a bounded first page rather than treating the whole file as delivered.
Exact requested ranges continue to pass through.

Install `requirements-hooks.txt` into the Python environment used by the hooks
for the fastest counts, with `pip install --require-hashes --only-binary=:all:
-r requirements-hooks.txt`. It locks `tiktoken` and everything it needs
(`regex`, `requests`, `charset-normalizer`, `idna`, `urllib3`, `certifi`) at
exact versions, with the SHA-256 of every wheel and sdist of each (#265), so
pip installs only those files and builds nothing. It has wheels for macOS
arm64 and x86_64, Linux x86_64 and aarch64 (glibc 2.28 or later) and Windows
x64, on CPython 3.10 through 3.15; elsewhere (Windows arm64, for one) the
install fails and the pure-Python encoder below counts instead. The pinned
`tiktoken==0.14.0` and bundled hash-checked
`o200k_base` vocabulary run locally; without `tiktoken` the built-in
`hooks/lib/pure_bpe.py` counts the same tokens (below). A replacement over 2,097,152 characters or insufficient savings leave
the original intact. Abandoned replacements earn no savings credit.

The gate tokenizes the bounded replacement and its note exactly, in both JSON
layouts. It does not tokenize the original when its size proves the saving: no
`o200k_base` token is longer than 128 bytes, so an original of n UTF-8 bytes has
at least n / 128 tokens. Only an original too small for that bound to decide is
tokenized, each distinct layout once, so the decision is the same as an exact
count. The pinned tokenizer merges long pieces in near-linear time, so the
largest outputs fit the hook's budget. The count is a surrogate, not Claude provider usage. No hook installs dependencies,
downloads a vocabulary or calls a model.

Without `tiktoken` (missing, the wrong version or no `regex`) the gate counts
with `hooks/lib/pure_bpe.py`, a standard-library implementation of the same
encoding's `encode_ordinary`. It splits text with an equivalent of the
`o200k_base` pattern over per-character Unicode classes (pinned to Unicode
16.0, the version tiktoken 0.14.0's regex crate uses) and applies tiktoken's
byte-pair merge. `tests/pure_bpe_regressions.py` compares the two token for
token on the repository, synthetic logs, every code point in context, long runs,
random strings and seeded fuzzing. Its vocabulary load costs about 70 ms per
hook process, then typical text takes about 0.2 ms per KiB; a long run of one
character about 2.6 us per byte, so a piece over 64 KiB, or over 128 KiB of
long pieces in one text, is refused rather than counted slowly.

When no exact encoder loads (in practice, a vocabulary that fails its check)
the gate falls back to byte counts. It admits a
replacement only when ceil(original bytes / 128), the fewest tokens the
original can have, is at least the replacement's bytes plus the note's bytes
plus the 512-token reserve. Bytes bound the replacement from above because the
tokenizer starts each piece as one token per byte and only merges, and every
single byte is an `o200k_base` token. So the fallback never admits a reply the
exact gate would refuse, but it needs an original about 128 times the size of
what replaces it: a 12 KiB Bash preview admits only past about 1.6 MB of output.
Ledger rows are the same either way: byte counts and the ceil(bytes/4)
estimate, never a tokenizer count.

### The hooks' Python

`hooks/hook-python.sh` picks the interpreter for every hook and plugin script:
a private one under the plugin's data directory if it exists, else `python3`
from `PATH`. The private one is `$CLAUDE_PLUGIN_DATA/hook-python/bin/python3`, or
`$CLAUDE_PLUGIN_DATA\hook-python\Scripts\python.exe` for a Windows venv; it is
only used when `CLAUDE_PLUGIN_DATA` is absolute (`/...`, `C:\...` or `C:/...`),
so a relative value can't let the working directory choose it.

A private venv is the recommended setup, because the system Python can change
under the plugin (a Homebrew upgrade can bring a `python3` without `tiktoken`;
the pure-Python encoder then keeps trims on, more slowly). `pip install --user` doesn't help either:
the hooks run Python with `-I`, which ignores user site-packages, and Homebrew
and other PEP 668 Pythons refuse a plain `pip install`.

At startup and resume, with Ork Meter Plus, `hooks/python-check.sh` runs
`hooks/lib/tokenizer_check.py` with the hooks' interpreter. A missing `tiktoken`
alone is not reported: the pure-Python encoder counts exactly. If the token gate
can't count exactly at all, it prints one notice naming the interpreter and the reason,
saying that trimming uses the conservative byte-count gate, and giving the
commands that install the tokenizer for full savings, with
this machine's paths filled in:

```sh
"<python3 from PATH>" -m venv --clear "$CLAUDE_PLUGIN_DATA/hook-python"
"$CLAUDE_PLUGIN_DATA/hook-python/bin/python3" -m pip install --require-hashes --only-binary=:all: \
  -r "$CLAUDE_PLUGIN_ROOT/requirements-hooks.txt"
```

Without `CLAUDE_PLUGIN_DATA` it suggests installing into the hooks' own
interpreter instead. Nothing is installed automatically: hooks don't touch the
network. Without Plus the check is silent, since every lever is off anyway and
the session start already says why. Until it's fixed only replacements the
byte-count gate proves are delivered and credited
(`tests/tokenizer_check_regressions.py`, `tests/tokenizer-check.test.sh`).

Economic qualification remains open (#202). Cache writes, reads, output,
retries and recovery must be accounted separately in Meter-captured live
comparisons. Keep the 24 KiB Python outline boundary; do not expand it on byte
savings alone.
