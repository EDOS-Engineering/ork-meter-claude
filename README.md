# Ork Meter for Claude Code

Reduction counts tokens with the pinned tokenizer (`requirements-hooks.txt`)
in the Python environment used by `hooks/py.sh` when it's installed, and
otherwise with a built-in pure-Python encoder that gives the same counts more
slowly. The bundled vocabulary is offline. If neither can load, a conservative
byte-count gate still admits trims whose size alone proves the saving (very
large outputs only), and the session start says so. Complete previews and
recovery notes must save at least 512 surrogate tokens before delivery.
See [token admission](docs/token-optimization.md#offline-token-admission) and
[the hooks' Python](docs/token-optimization.md#the-hooks-python).

Bundled skills are explicit commands only. Their names and descriptions stay
out of the normal model prompt; automatic hooks provide the default behavior.
`/ork-meter:explore` was model-invocable until #282: Claude invoked it 0 times
in 23 live sessions, so it is now a command like the others, and request 1 of
a plugin session is the same as without the plugin.
The package registers no agents, MCP servers or MCP tool schemas. Agent
definitions are parked under `docs/optional-agents/` for explicit experiments
only; `/ork-meter:lean install` copies one of them, the lean main agent, into a
project when you ask.

Connect once, then use Claude Code normally. Ork Meter captures token-counter
metadata automatically, without a separate helper install or special launch
command. Prompts, responses, tool content and account identifiers stay out of
the hosted usage store.

**Status: public source.** This repository is the public distribution of the
plugin. Until the first tagged release is published here, the marketplace
installs `main`; from then on it installs the release tag the marketplace
names ([docs/release.md](docs/release.md), "Release channel"). The automatic
setup, receiver and disconnect flow has local synthetic tests, and CI runs the
installed-host qualification on macOS, Linux, Windows and WSL2.

## Connect

Install the plugin through the EDOS marketplace. It installs the commit the
marketplace names: `main` until the first tagged release, then that release
tag; see [docs/release.md](docs/release.md), "Release channel":

```text
/plugin marketplace add EDOS-Engineering/ork-meter-claude
/plugin install ork-meter@edos
```

Requires Claude Code 2.1.280 or later (connect refuses an older one) on
macOS, Linux, Windows with Git Bash, or WSL2. The VS Code and Cursor
extensions are not tested. Which platform, surface and version is verified,
and how, is in [docs/platform-matrix.md](docs/platform-matrix.md), with the
known limitations (Windows without Git Bash, WSL credential store, macOS's
Python 3.9).

Then run `/ork-meter:connect`. For the dev platform, use
`/ork-meter:connect dev`. Nothing is built on your machine: the
plugin carries prebuilt runtime binaries for macOS (Apple silicon and Intel),
Linux (x86-64 and arm64) and Windows (x86-64). Before it runs the one for
your platform, `scripts/runtime.sh` checks it against the SHA-256 shipped
beside it, which catches a corrupted or partly replaced install but not a
swapped binary and hash. The first session after an install or update sets
the runtime up in the background and says so; capture is checked from the
next session. Provenance comes from the build attestation each
binary gets when it is built, which CI and publishing verify
([docs/release.md](docs/release.md#how-the-runtime-is-verified)).

Every tagged release also has a GitHub Release `v<version>` on this
repository with the plugin tree as a zip, each runtime binary, a `.sha256`
per asset, a `manifest.json` and build provenance attestations
(`gh attestation verify <asset> --repo EDOS-Engineering/ork-meter-claude`). It
is for inspection and verification; install through the marketplace commands
above (see [docs/release.md](docs/release.md), "Publishing the GitHub
Release").

1. The plugin checks Claude compatibility and known telemetry conflicts.
2. A setup page asks for your Anthropic plan and telemetry permission.
3. Sign in to Ork Meter and approve the matching Claude connection in your
   browser. Approval returns directly to the plugin; no claim code is copied.
4. Credentials are saved in the OS credential store. The plugin adds its
   metrics configuration to Claude's user settings, preserving unrelated keys.
5. Restart Claude once, then launch it normally. The internal receiver starts
   automatically when Claude requests its telemetry header or the plugin's
   session-start hook runs.

The plugin never selects a plan or consents for you. Known file-based managed
telemetry settings and conflicting existing exporters stop setup. All managed
policy still applies, including policies the plugin cannot inspect. Team and
Enterprise capture remains unqualified; check your organization's permission.

## Status

Run `/ork-meter:status`. The connection is remembered; no reference is needed.

- **Setup pending:** finish the browser flow.
- **Connected, waiting for usage:** authorization is verified, but a recent
  export from the current receiver has not been proved.
- **Receiving usage:** a recent export was acknowledged after this receiver
  started. Check its capture and receipt times.
- **Authorization unverified:** check network/account access and retry. This
  does not by itself prove revocation or require re-pairing.
- **Authorization refused:** Ork Meter refused this computer's credential
  (usually revoked or expired; a proxy, firewall or lost account access can
  answer the same way) and delivery has stopped until you reconnect.
- **Authorization expiring / expired:** status gives the credential's expiry
  date. The runtime renews it in the background, while Claude Code is in use,
  once fewer than seven days remain, so an expiring notice usually clears on
  its own. Its next step says to disconnect and connect again to renew before
  the date; that deletes observed usage still waiting, so with days left keep
  using Claude Code and check status again first. Once expired, usage is no
  longer sent and session start says so once (at the next session when the
  first one after an install or update set the runtime up).

Status also reports the last delivery answer and any observed token events
waiting, set aside or rejected. Disconnecting deletes those waiting events;
they can never be sent afterwards.

What happens to Claude Code, the hooks and capture when Ork Meter is down,
slow or unreachable, or a credential is revoked, expiring or expired, is
tabled in [docs/failure-behaviour.md](docs/failure-behaviour.md).

Claude normally exports metrics every 60 seconds. Session-only exports are
discarded locally. A missing receipt means no retained evidence, not zero usage.
Cumulative samples overlap: they are **not additive token totals or charges**.

## Token trim (local)

With a connected Ork Meter Plus account (trial included), the plugin also trims tokens on this machine. Without Plus, or when not connected, every trim lever below stays off (without Plus the session start says so once); usage capture is unaffected (see [the Plus gate](docs/token-optimization.md#ork-meter-plus-gate-hookslibentitlementpy-ork-meter-261264)). Oversized tool output is trimmed before it enters the model's context (the full output stays on disk), unchanged re-reads are shortened, whole-file reads of very large files are paged, test runners lose their passing-test noise, a resumed session with an expired prompt cache gets a one-line nudge, and repeated identical failures get a warning. The design, what is measured and what is only estimated are in [docs/token-optimization.md](docs/token-optimization.md). The plugin never prints a percentage saved. These hooks need Python 3.10 or later on `PATH` (or a private one, see [the hooks' Python](docs/token-optimization.md#the-hooks-python)); without it they do nothing and the session runs as before, and a one-line notice at session start (`hooks/python-check.sh`) names the Python that was found. With Plus, the same notice says when that Python lacks the pinned tokenizer, so trims are refused and nothing is saved. The entitlement and the levers' default settings arrive from the platform in a signed envelope that the hooks verify on every read, so a hand-written file entitles nothing and your `ORK_METER_*` settings can only turn levers off relative to that policy (see [Signed policy](docs/token-optimization.md#signed-policy-hookslibplus_policypy-hookslibentitlementpy-ork-meter-docsplus-policymd)).

| Part | What it does | What it runs |
|---|---|---|
| Session-start status (`hooks/trim-session.py`) | One line saying the trim is on, its budgets and the read-guard threshold (the values in force, overrides included); a cache-miss nudge when Claude Code reports an expired prompt cache on a large resumed session; resets the per-session state. | Writes `plugin-features.json` (see below) and the session store (`<scratchpad>/ork-meter/`, or `<tmp>/claude-ork-meter-<uid>/<session>/ork-meter/` on a Claude Code that sends no scratchpad directory). Local only. |
| Tool-output trim (`hooks/trim-tool-output.py`, PostToolUse on Bash, Grep, Glob, WebFetch) | Replaces a result over its budget (Bash 12 KiB, Grep 8 KiB, Glob 4 KiB, WebFetch 16 KiB) with a head-and-tail version that keeps error lines and says what was elided; drops passing-test lines from test-runner output. | Writes the full original to `<scratchpad>/ork-meter/tool-output/<id>.txt` (0600) and names that path in the result. Appends a line to the local savings ledger. `ORK_METER_TRIM=off` turns it off. |
| Read trim and repeat-read note (`hooks/dedupe-reads.py`, PostToolUse on Read) | A Read over 24 KiB that Read compaction (below) doesn't take keeps its head with correct line numbers and a note naming the rest; a second identical read this session is shortened to its first 15 lines plus a note. | Keeps `<scratchpad>/ork-meter/reads.json` (path, offset, limit, content hash). Local only. |
| Large-file read guard (`hooks/read-guard.py`, PreToolUse on Read) | Pages a whole-file Read of a text file over 96 KiB to its first 300 lines. For suitable Python files over 24 KiB, it gives the first 48 exact lines plus a bounded AST symbol map with original line numbers when that saves bytes. | Stats and sniffs the file for a NUL byte. Never touches a Read that sets `offset`, `limit`, `pages` or `allow_large`; exact ranges remain available. |
| Read compaction (`hooks/dedupe-reads.py`, PostToolUse on Read; `hooks/lib/read_compact.py`) | **Off unless you set `ORK_METER_READ_COMPACT=on`** (off by default until its Write guard is hardened, #316). A whole-file Read over 6,000 tokens (counted offline as Claude Code renders it) first loses whole lines: a licence header, long comment blocks and docstrings, long data literals and minified lines, then bodies of large functions far from the top. Every shown line is exact at its original line number; each elided range is one marker line and empty numbered lines. If that still doesn't fit, the read guard's page applies, with its note. | Only whole lines are removed, never the first 40 or those around a `TODO`/`FIXME`-style marker; JSON is never compacted. The original is saved for recovery. `ORK_METER_READ_COMPACT=on` turns it on, `off` turns it off. |
| Write guard (`hooks/write-guard.py`, PreToolUse on Write; PostToolUse on Read, Bash, Grep) | Refuses a Write that would delete lines a compacted Read left out and that the writing session hasn't read since, naming them at their current line numbers, to Read with `offset`/`limit` or change with Edit. Follows the file through Edits, other sessions (fork, resume) and other spellings of its path; aligns the Write with the file, so a duplicate shown elsewhere doesn't count as keeping a line; refuses when it can't check; runs in every mode. `ORK_METER_WRITE_GUARD_OVERRIDE=on` is the deliberate override, for every file. | Keeps one record per guarded file in `$XDG_STATE_HOME/ork-meter/read-guard/` (line hashes and file-identity hashes, no paths or text), each for 7 days at most. Local only. |
| Loop guard (`hooks/loop-guard.py`, PostToolUseFailure) | Warns you when the same tool call has failed 3 times; stops the session only if you set `ORK_METER_LOOP_STOP`. | A counter file keyed by a hash of the input. Local only. |
| Stale-context guard (`hooks/stale-context.py`, UserPromptSubmit) | **Off unless you set `ORK_METER_STALE_GUARD=on`.** When you send a prompt after the conversation's prompt cache expired and the context is 100,000 tokens or more, holds that one prompt and says how many tokens it would re-send: `/clear`, a focused `/compact`, or submit it again to continue. Holds at most once per idle period. | Reads token counts and timestamps from the end of the transcript, not message content. A small state file per session. Local only. |
| Context-size nudge (`hooks/context-nudge.py`, UserPromptSubmit) | **On by default only when the session pays per token** (an API key, Bedrock, Vertex or Foundry, or a subscription on usage credits); off on a subscription. `ORK_METER_CONTEXT_HYGIENE=on` or `off` decides instead; a project can only turn it off. The billing mode is read once per session from metadata: the prompt-cache lifetime Claude Code chose in this project's recent transcripts (5 minutes per token, 1 hour on a subscription), else `apiKeyHelper` or a cloud-provider or API-key variable being set (by name; a key's value is never read). When the conversation's context passes 120,000 tokens, suggests `/clear` (unrelated next task) or a focused `/compact`. At most three times a session, only as the context grows; shown to you, never added to Claude's context. | Reads token counts from the end of the transcript, not message content. A small state file per session. Local only. |
| Delta delivery (`hooks/lib/delta.py`, inside the Bash and Read PostToolUse hooks) | **Off unless you set `ORK_METER_DELTA=on`.** Experimental. A re-run of the same Bash command, or a whole-file re-read of a file that changed, is delivered as a unified diff against the earlier result still in Claude's context (an identical re-run becomes one line), when that is smaller than the ordinary result. | Keeps the earlier full result per agent and command or path under `<scratchpad>/ork-meter/delta-bases/` (0600, hash-checked), deleted on every session start and compaction. The full new output is still saved and named. Local only. |
| Session summary (`hooks/session-summary.py`, SessionEnd) | Writes the session's ledger totals next to the token usage measured from the session transcript. | Reads the transcript, writes `<scratchpad>/ork-meter/session-summary.json`. Local only. |
| `/ork-meter:savings [--all]` | Prints what the hooks did: interventions, bytes avoided, **estimated** tokens avoided, and the session's **measured** token usage, each labelled. | `hooks/py.sh --script scripts/savings.py` (`python3 -I`). Reads local files only. |
| `/ork-meter:cache [--all]` | Where this project's tokens went (measured cache writes, cache reads and output) and which prompt-cache misses re-sent whole conversations, by cause: idle past the cache lifetime, model or effort switch, compaction, tool or MCP changes. Dollar figures are labelled list-price estimates. | `hooks/py.sh --script scripts/cache_report.py` (`python3 -I`). Reads Claude Code's local transcripts for usage counters and timestamps only, never message content. Sends nothing. |
| `/ork-meter:audit` | Lists what loads into every session here: CLAUDE.md and rules files with line counts and estimated tokens, MCP server names, and the largest items. Recommends a code-intelligence (LSP) plugin for each typed language it finds, and a 1-hour prompt cache only when your transcripts show the 5-minute cache being re-written after 5-60 minute pauses; each recommendation shows the counts it is based on. Says whether the context-size nudge is on by default here, and why. | `hooks/py.sh --script scripts/audit.py` (`python3 -I`). Reads local files only; prints MCP server names, never their configuration. Languages come from file names and config-file presence, never file contents; the cache advice from transcript usage counters and timestamps. |
| `/ork-meter:explore` | Searches the whole workspace with ranked, deduplicated snippets in grep-like text (each file with its hash; `--json` for the full record) and reads several files or ranges in one Bash call, instead of many sequential Read calls or grep/find runs in Bash. Ignored, vendored, generated, binary and secret-looking files are skipped and counted; a 2-second, file-count and 12 KiB output limit is reported, never silent. Savings not yet measured. | Read-only `scripts/source.py --search` and `--batch` in the project directory. No MCP server, no network; follows no symlinks. |
| `/ork-meter:source` | Optional exact-source diagnostic. Ordinary large Python Read calls now receive an AST map automatically; Claude can follow its line ranges with Read. | Read-only `scripts/source.py` remains available for explicit batches and hash-checked selections. |

Every hook runs with a 5-second cap in `hooks/hooks.json` (the session-start hook and the Python and tokenizer check included), and the Python hooks have a 1.5-second internal budget (`hooks/lib/hook.py`). Every hook prints nothing and exits 0 on any error, so Claude Code is never blocked. Opt-outs and tuning: `ORK_METER_TRIM=off`, `ORK_METER_DEDUPE=off`, `ORK_METER_READ_GUARD=off`, `ORK_METER_TEST_FILTER=off`, `ORK_METER_NUDGES=off`, `ORK_METER_LOOP_GUARD=off`, `ORK_METER_READ_COMPACT=on` (opt-in), `ORK_METER_TRIM_BUDGET_<TOOL>`, `ORK_METER_READ_GUARD_BYTES`, `ORK_METER_READ_GUARD_TOKENS`; the full list, with defaults and floors, is the lever registry in `hooks/lib/features.py`, tabled in [docs/token-optimization.md](docs/token-optimization.md#defaults-and-why). A project's own `.claude/settings.json` or `.claude/settings.local.json` can only turn these off: an `env` value there that would turn a lever or `ORK_METER_MODE=shadow` on, or change a budget or threshold, is ignored in favour of your user or managed settings (or the default), and the session start names it (`hooks/lib/project_env.py`; a project's `ORK_METER_RETENTION_DAYS` never changes how long other projects' saved output is kept: one at or below yours applies to that project's own sessions only, a higher one is ignored; managed settings from macOS MDM profiles or the Windows registry aren't read). Set levers you want on in `~/.claude/settings.json` or your shell; a shell value the project also sets is treated as the project's.

**A failing Bash command is not trimmed or captured.** When a command exits nonzero, Claude Code renders its error before any hook runs, and that rendering may already have dropped diagnostics from the middle of the output and stderr. This is Claude Code's own behaviour and happens with the plugin off too. The hook that then runs (`PostToolUseFailure`) can't replace the output, so the plugin doesn't capture failing commands automatically and never reruns one to recover what was lost. In shadow mode (`ORK_METER_MODE=shadow`) the local trace still records the failure payload, which is that same rendered error. For the full output of a command you expect to fail, run it yourself through `/ork-meter:capture` (below), which saves both streams before Claude Code renders them. The skill is user-only (`disable-model-invocation`), so you run it; Claude doesn't invoke it on its own. See [failing Bash commands](docs/token-optimization.md#failing-bash-commands) for what that route does and doesn't cover.

**The token-trim hooks send nothing.** They write only local files: the session's `<scratchpad>/ork-meter/` directory (full copies of trimmed tool output, a read index, a savings ledger of byte counts and estimates with no content and no paths, a session summary), `~/.local/state/ork-meter/plugin-features.json`, which holds the plugin version, the sorted list of levers that are on, and a timestamp, and `~/.local/state/ork-meter/retention-state.json`, which lets saved-output retention resume between sessions (a position and the byte size of each session's saved output, keyed by SHA-256 hashes of the directories, so no project or session name; at most 10,000 entries for each of up to 8 temp roots). The plugin runtime reads that file and sends the plugin version and the lever list with each usage snapshot, as metadata; Ork Meter stores them only if you opt in to measurement, so that your own ledger can compare periods with and without a lever. Nothing else from those files is read. To start faster, the hooks also keep Python's compiled bytecode of their own and the standard library's modules in `$CLAUDE_PLUGIN_DATA/pycache`, owner-only; nothing is cached when that variable is unset or not an absolute path.

## Cost status line (opt-in)

`/ork-meter:statusline` prints a `statusLine` snippet for your Claude Code settings. Once you add it to `~/.claude/settings.json` (or a project's `.claude/settings.local.json`), the bar under the prompt shows, for example:

```text
ctx 84.2k | turn ~$0.41 | session ~$12.30 | cache 1h: 37m left (est.)
```

`ctx` is the context size of the latest request, in tokens, measured from the session transcript. `turn` (since your last prompt) and `session` (subagents included) are **estimates at API list prices**, the same prices as `/ork-meter:cache`; on a subscription they are relative weights, not a bill. `cache` is the prompt-cache lifetime the transcript's usage records show (1 hour or 5 minutes, not an assumed 5) and the time left; `expired` means your next prompt will likely re-send the whole context at the cache-write price. A plugin can't set your status line (plugin settings only take `agent` and `subagentStatusLine`), so nothing changes until you add the snippet, and the skill never edits your settings unless you ask. The snippet names this plugin version's path: after a plugin update the line says `plugin updated: rerun /ork-meter:statusline`. It needs `python3` on `PATH`, like the hooks, reads only usage counters, model ids, timestamps and record types (never message content), keeps a small owner-only file per session under `~/.local/state/ork-meter/statusline/`, and sends nothing. Details: [docs/token-optimization.md](docs/token-optimization.md#cost-status-line-scriptsstatuslinepy-ork-meterstatusline-issue-60).

## Recover output and retrieve source

`/ork-meter:capture <command argv>` captures a selected noninteractive command
before native error rendering can shorten it. The command runs once, retains
its exit status, and saves exact stdout/stderr for recovery. Only commands
you route through it are captured; ordinary failing Bash calls are not (see
"A failing Bash command" above). Its cancellation, background-command and
process-group lifecycle, and its behaviour with very large streams, are not
qualified. See [command capture](docs/token-optimization.md#command-capture-before-native-rendering).

For local replay capture, set `ORK_METER_MODE=shadow` before starting Claude.
Registered tool hooks preserve inputs/results in private local traces and leave
tool behavior unchanged. `ORK_METER_MODE=off` disables automatic optimization
without capturing content. Native usage streams can be normalized with
`python3 -I -B scripts/usage.py --harness claude|codex <jsonl-file>`; duplicates,
streaming updates and missing counters are handled explicitly. See
[capture and usage details](docs/token-optimization.md#shadow-capture-and-native-usage-replay).

Reduced results already include a local full-output path that Claude can follow
with ordinary Read calls. That path is in the plugin's session store, outside
the project, so where file tools are confined to the working directory
(`--restricted`, sandboxes, permission rules) Read and Grep refuse it; the note
then adds one line saying Bash `sed -n 'A,Bp'`, `head`, `tail` or `cat` reads
it, which still works there and arrives untrimmed. `/ork-meter:recover <artifact-id>` remains an optional
diagnostic for exact searches and expansions without rerunning the command.
Use the SHA-256 filename from its full-output path.
Full recovery preserves the original tool response, including separate output
streams and status fields. Read reductions also retain an original snapshot,
so later edits do not erase the evidence. Recovery files and explicit Read
ranges bypass reduction. Recovery counts and returned bytes remain local and
appear in `/ork-meter:savings` as additional work.

`/ork-meter:source <file>` provides Python outlines and exact symbol bodies,
including decorators, with a SHA-256 freshness check. Other languages use exact
line ranges and normal lexical search. These commands need Python 3.10+ and
use deterministic local parsing. They make no additional model calls.

The plugin publishes a session-bound file under
`session-features/<sha256(session_id)>.json`, for the platform's newer
session-aware capture path, and the global feature file, which lists only the
levers on in every live session (what counts as live is in
docs/token-optimization.md, "Active-lever file"). The payload contains only version, feature names
and update time. Raw session identities and artifact content stay local.

## Project map (local, opt-in)

`/ork-meter:map` builds a compact map of the current project (layout with
sizes, entry points, build and test commands, manifests, conventions, README
headings and top-level Python symbols) and writes it as the project skill
`.claude/skills/ork-meter-map/SKILL.md`, with a `.gitignore` beside it so git
ignores it. Claude loads it only when it needs orientation; until then only its
one-line description is listed. The map is at most 6 KB, deterministic, honours
`.gitignore`, skips vendored, generated and secret-looking files without
opening them, follows no symlinks and reads nothing outside the project. Run it
again to refresh; `/ork-meter:map preview` prints without writing and
`/ork-meter:map remove` deletes it. It runs no model and sends nothing: the map
never leaves the machine and is never sent to Ork Meter. It is not gated on
Plus. The token effect is not measured yet. Details:
[project map](docs/token-optimization.md#project-map-scriptsproject_mappy-ork-metermap-issue-109).

## Lean main agent (opt-in)

`/ork-meter:lean install` copies an agent, `ork-meter-lean`, into this
project's `.claude/agents/` with a `.gitignore` that ignores only that file. It
asks Claude for fewer turns and smaller reads: delegate broad exploration to the
Explore subagent or use `/ork-meter:explore`'s search and batch reads, read
ranges rather than whole files, batch related edits and don't re-read unchanged
files, while still reading code before changing it and running the tests. It is
never on by default and the command edits no settings: start a session with
`claude --agent ork-meter-lean`, or add `"agent": "ork-meter-lean"` to
`.claude/settings.local.json` yourself to make it the project default.
`--agent` replaces Claude Code's default system prompt with the agent's (which
restates the essential ground rules); `claude --append-system-prompt-file
.claude/agents/ork-meter-lean.md` keeps the default prompt and adds the rules
instead. While installed, its one-line description is listed in every session in
the project. `/ork-meter:lean status` reports what is installed and
`/ork-meter:lean remove` deletes only what it wrote. It refuses to overwrite a
file it didn't write or to write through a symlinked `.claude` or `agents`
directory. The token effect is not measured yet. Details:
[lean main agent](docs/token-optimization.md#lean-main-agent-scriptslean_agentpy-ork-meterlean-issue-108).

## Disconnect

Run `/ork-meter:disconnect`. The plugin stops its receiver, removes only its
unchanged configuration entries and deletes the local connection's credential.
Unrelated configuration and later edits are preserved. It opens your connection
in Ork Meter for you to confirm hosted revocation.

Local disconnect does not prove hosted revocation and does not erase usage
already received. Finish the browser revocation review before uninstalling the
plugin with `/plugin uninstall ork-meter@edos`.

## Runtime and data

The plugin includes an internal `ork-meter-plugin-runtime` component. It
verifies its SHA-256 and installs it in persistent plugin data so plugin-cache
updates do not remove Claude's header callback. It does **not** run the legacy
`ork-meter` or `ork-meter.exe` program.

Each runtime version is installed once, into
`$CLAUDE_PLUGIN_DATA/runtime/<sha256>/`. When a session starts and that copy
is missing or damaged, the SessionStart hook does not install it while Claude
waits: it starts the install detached (its own process session where the
platform has one, an environment holding only `PATH`, the temp and Windows
system variables, a lock so that concurrent sessions install once), prints
"Ork Meter: setting up its runtime in the background; it is ready from your
next session." and returns. The install verifies the package's SHA-256,
copies it through a staging file it verifies again, and runs the new copy
once with `--version`, so a first-run antivirus scan (Windows Defender) is
not paid inside a later hook. `/ork-meter:status` and the other skills
install in the foreground if they run first.

Later sessions do not hash the 12 MB runtime again. The install writes a
stamp beside the copy (`.verified`: the verified SHA-256 and the copy's
inode, size, mtime and ctime), and the copy is hashed again only when the
stamp no longer matches; any write, `chmod` or replacement changes the ctime,
which only root can set. The stamp is trusted only when the data directory,
`runtime/`, the version's directory, the stamp and the binary are owned by
you and nobody else may write them; otherwise every session hashes the copy,
as before. The package is hashed only when it is installed: the copy that
runs is the one that matches `SHA256`.

Credentials, the callback capability and local receiver token are held in the
OS credential store: macOS Keychain, Windows Credential Manager, or Linux
Secret Service. No plaintext fallback is provided. Local settings writes use
private staging: Unix permissions or a Windows current-user-only ACL.

Only helper-generated snapshot identity/time, the four supported token-counter
categories, client `claude-code`, your self-declared plan/confirmation time,
and the plugin version and enabled-lever labels (stored only if you opt in to
measurement) are forwarded. Raw telemetry, resource attributes, model labels,
identities, prompts, completions and tool content are discarded before delivery. Claude's
export is acknowledged at once and delivered in the background, so a slow or
unreachable Ork Meter never holds Claude Code. Only the latest unacknowledged
filtered snapshot is retained in memory; a graceful stop gives it one bounded
delivery attempt, otherwise stopping the receiver loses it (the next
cumulative export carries its values). Observed token events are kept in an
owner-only file in the plugin data directory and retried with backoff,
honouring Ork Meter's `Retry-After`; a rejected event is set aside and
re-sent, never silently dropped, and a refused credential stops delivery
until you reconnect. Collector credentials last at most 30 days and are
renewed in the background once fewer than seven days remain (the platform
extends one at most once a day, never in its first day).

Saved tool output stays on your machine. When a lever shortens a result, the
full original is kept under the session scratchpad's `ork-meter/tool-output/`
(0600) so Claude can reopen it. It is deleted after 7 days
(`ORK_METER_RETENTION_DAYS`; `0` deletes a session's own copies when it ends),
oldest first once all sessions' copies pass 256 MiB (sparing anything modified
in the last 24 hours), and at once when a session ends with `/clear` or logout.
Only the session doing the pruning is fully protected. Another session that is
still running can lose copies older than 7 days, or older than 24 hours while
the 256 MiB cap is exceeded. A project's `.claude/settings.json` can lower
`ORK_METER_RETENTION_DAYS` for its own sessions only (the project is
`$CLAUDE_PROJECT_DIR`, else the starting directory's own checkout, decided once
when the session first starts and pinned in its store's `retention.json`; a
resume never changes it). It never lengthens retention or affects another
project's copies. At `0`, a session's copies go when it ends, and the same
project's other sessions' copies once older than 24 hours.

Endpoints are fixed: `https://orkmeter.com`, or
`https://dev.ork-meter.edos.io` for dev, plus an authenticated
`http://127.0.0.1:<port>` local receiver/setup page. No arbitrary destination is
accepted. Browser pairing approval remains explicit. Only the approving page
can return its short-lived claim to the initiating local callback.

## Development

Requires Claude Code 2.1.280+ ([why](docs/platform-matrix.md#minimum-claude-code-version)), Go matching the platform's `go.mod`, Python 3.10+
for the development builder, and Bash for the plugin entry points. Customers
receive the runtime inside the plugin package and do not need Go or Python.

Build a development runtime from the matching platform checkout:

```sh
python3 scripts/build_runtime.py --platform-source ../ork-meter
claude plugin validate --strict .claude-plugin/plugin.json
claude plugin validate --strict .claude-plugin/marketplace.json
bash tests/run.sh
claude --plugin-dir .
for f in hooks/*.py hooks/lib/*.py scripts/*.py; do python3 -m py_compile "$f"; done
for f in hooks/*.sh tests/*.sh; do bash -n "$f"; done
python3 tests/fixtures/make-fixtures.py                      # regenerate the synthetic payloads
```

The builder writes only `.local/runtime/<os>-<arch>` and its checksum. It can
cross-compile with `--os` and `--arch`; compilation is not native acceptance.
A release is a commit on `main` holding `runtime/<os>-<arch>/ork-meter-plugin-runtime`
(`.exe` on Windows) and the corresponding `SHA256` file. A source checkout
runs the released `runtime/` unless you set `ORK_DEV_RUNTIME=1`, which runs
the `.local/runtime` build instead and says so on stderr; an install missing
its runtime fails explicitly. Publication remains a reviewed
release operation ([docs/release.md](docs/release.md)).

The internal component's source and integration tests live in the platform's
`cmd/claude-plugin-runtime` and `internal/pluginruntime`. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the local dev loop and CI.

Security: **security@edos.io**, or GitHub private vulnerability reporting on this repository (see [SECURITY.md](SECURITY.md)).
Support: **support@edos.io** or a repository issue.

Claude Code is an Anthropic product. Ork Meter is independent and is not
endorsed by Anthropic. Copyright 2026 Enterprise DevOps Solutions LLC. Licensed
for use with an Ork Meter or Ork Meter Plus account under the Ork Meter Limited
Use License; see [LICENSE](LICENSE). Third-party components are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
