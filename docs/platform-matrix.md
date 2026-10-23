# Platform and version matrix

Where the plugin is verified, against which Claude Code versions, and how
(#30). A release re-runs the manual rows ("Release checklist" below); CI
re-runs its rows on every pull request, merge group and nightly run on `main`
(`.github/workflows/ci.yml`).

**Surfaces in scope:** the Claude Code CLI on macOS, Linux, Windows (native,
with Git Bash) and Windows WSL2, and the Claude desktop app's **Code** tab as
a manual walkthrough. **Out of scope: the VS Code and Cursor extensions.**
They are not tested, and nothing here claims they work (scope set for #30 on
2026-10-20).

## Minimum Claude Code version

**Minimum: Claude Code 2.1.280.** The plugin needs it for two reasons:

1. **Connect refuses anything older.** The runtime's `connect` command runs
   `claude --version` from `PATH` and stops with "Claude Code 2.1.280 or
   later is required on PATH" below 2.1.280 (ork-meter
   `cmd/claude-plugin-runtime/main.go`, `compatibleClaude`). Without a
   connection there is no capture, and without Plus on a connected account
   every trim lever stays off, so an older Claude Code gets nothing from the
   plugin.
2. **The hooks rely on host behaviour from 2.1.275 and earlier** (table
   below). The newest of these is the `SubagentStop` matcher fix in 2.1.275,
   which the plugin's `agent-stop.py` matcher needs to fire only for its own
   agents.

CI tests exactly this floor and the latest release (`native-host`, both
versions on every OS), so the floor is tested, not only stated. Claude Code
does not enforce it at install: the plugin manifest has no minimum-version
field (none is documented in the plugins reference), and an older Claude Code
installs and enables the plugin. The install-only run on 2.1.145 in #30
(2026-10-11)
showed exactly that; 2.1.145 is below the floor and is not supported.

What the plugin relies on, and when Claude Code added it. "Changelog" is the
Claude Code
[CHANGELOG.md](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md);
"hooks reference" is <https://code.claude.com/docs/en/hooks>; both read on
2026-10-20.

| Feature | Used by | Since | Source |
|---|---|---|---|
| Per-command hook `timeout` | every hook (5 s) | 1.0.41 | changelog |
| `SessionEnd` hook | `session-summary.py` | 1.0.85 | changelog |
| `SubagentStop` hook, and its matcher on agent type | `agent-stop.py` (`^ork-meter:(explore\|logs\|test-runner)$`) | 1.0.41; the matcher fired for every subagent with an empty agent type until **2.1.275** | changelog |
| `${CLAUDE_PLUGIN_ROOT}` substituted in skill `allowed-tools` | every skill | 2.1.0 (fix) | changelog |
| `disable-model-invocation` skill frontmatter | all 13 skills (`explore` since #282) | before 2.1.110 (fixed then for mid-message `/skill`); since 2.1.222 Claude asks the person to run such a skill instead of copying its steps | changelog |
| `PostCompact` hook | `trim-session.py` | 2.1.76 | changelog |
| `${CLAUDE_PLUGIN_DATA}` | runtime data, bytecode cache, private hook Python | 2.1.78 | changelog |
| `PostToolUseFailure` hook | `loop-guard.py`, failed Bash trimming | before 2.1.119 (`duration_ms` added then) | changelog |
| `updatedToolOutput` for built-in tools | `trim-tool-output.py`, `dedupe-reads.py` | 2.1.121 (MCP-only before) | changelog |
| `SessionStart` source `fork` | the `startup\|resume\|clear\|compact\|fork` matcher | 2.1.214 (a fork reported `resume` before) | changelog |
| `SessionStart` resume/fork cache fields (`prompt_cache_likely_expired`, `context_tokens`, `estimated_cache_write_usd`) | cache-miss nudge | 2.1.251 | hooks reference, [token-optimization.md](token-optimization.md#cache-miss-nudge-hookstrim-sessionpy) |
| `scratchpad_dir` in hook input | session store | 2.1.257 | hooks reference |
| `claude --include-hook-events` | tests only (`tests/native_host.py`) | not in the changelog; present at 2.1.280, which CI runs | CI |
| Plugin `bin/` executables | not used | 2.1.91 | changelog |

Changes after the floor that matter but are not required: 2.1.284 stopped
adding missing plugin `bin/` directories to `PATH` on Windows (a Bash tool
failure with many plugins); 2.1.296 stopped a late-loading plugin's
`SessionStart` output reaching the conversation after `/clear` in headless
sessions. Versions also differ where it is not a feature: a Bash result that
Claude Code persisted reached the model as the hook's replacement on Windows
2.1.280 but as Claude Code's own preview on macOS 2.1.300, and
`hooks/trim-tool-output.py` handles both
(issue #98).

The floor lives in four places that must agree: the runtime's
`compatibleClaude` (ork-meter), `native-host`'s `claude` matrix in
`ci.yml`, the README's requirements, and this page.
`tests/ci_workflow_regressions.py` checks the last three. Raising it means
changing the runtime first.

## Matrix

"CI" names the job in `ci.yml`; its result for a change is on the pull
request. `native-host` runs the installed-host suite
(`.github/scripts/check-native-host.py`: manifests validated with
`--strict`, then the plugin loaded into the installed `claude` against a
localhost inference fixture, with no skipped cases); `unit` runs
`tests/run.sh` without Claude Code. On pull requests `native-host` skips
macOS with 2.1.280 to save macOS minutes; merge groups and nightly runs keep
it, so every listed CI row passes before a change reaches `main`. A merge
group whose tree equals its up-to-date PR's head reuses that PR's passing run
for the other rows and runs only this one (CONTRIBUTING, "CI").

| Platform | Surface | Claude Code | How it is verified |
|---|---|---|---|
| macOS arm64 | CLI | 2.1.280 | CI: `native-host (macos-latest, claude 2.1.280)` (merge group and nightly), `unit (macos-latest)` |
| macOS arm64 | CLI | latest | CI: `native-host (macos-latest, claude latest)`, `unit (macos-latest)`. Manual: connect through hosted receipt on 2.1.296/2.1.300, 2026-10-14; marketplace install on 2.1.296, 2026-10-11 |
| macOS x64 (Intel) | CLI | 2.1.280, latest | **Not verified.** `macos-latest` is arm64. The `darwin-amd64` runtime is built and attested but has never run. Manual walkthrough row |
| Linux x64 | CLI | 2.1.280 | CI: `native-host (ubuntu-latest, claude 2.1.280)`, `unit (ubuntu-latest)` |
| Linux x64 | CLI | latest | CI: `native-host (ubuntu-latest, claude latest)`, `unit (ubuntu-latest)`, `connected failure modes (linux)` (fake Ork Meter, gnome-keyring) |
| Linux arm64 | CLI | 2.1.280, latest | CI: `unit (ubuntu-24.04-arm)` (no Claude Code). Manual: Debian 12 container, 2.1.296, local marketplace, hook fired, 2026-10-11. The installed-host suite and connect are not verified on arm64 |
| Windows x64, native (Git Bash) | CLI | 2.1.280 | CI: `native-host (windows-latest, claude 2.1.280)`, `unit (windows-latest)` |
| Windows x64, native (Git Bash) | CLI | latest | CI: `native-host (windows-latest, claude latest)`, `unit (windows-latest)`. Manual: isolated settings and header test on 2.1.296 (README "Status") |
| Windows x64, WSL2 Ubuntu 24.04 | CLI | latest | CI: `wsl (ubuntu-24.04, claude latest)`: `tests/run.sh`, manifest validation and the installed-host suite inside the distribution. Connect (browser, keyring) is a manual walkthrough row |
| Windows x64, WSL2 Ubuntu 24.04 | CLI | 2.1.280 | **Not verified** in WSL; the same Linux build passes `native-host (ubuntu-latest, claude 2.1.280)` |
| Windows arm64 | CLI | any | **Not verified.** There is no `windows-arm64` runtime; the x64 one would run under emulation |
| macOS | Desktop app, Code tab | the app's own copy | **Manual walkthrough only; not yet walked** |
| Windows x64 | Desktop app, Code tab | the app's own copy | **Manual walkthrough only; not yet walked** |
| any | VS Code, Cursor | any | **Out of scope** |

The manual rows still to walk for a release: macOS x64, Linux arm64
(installed-host suite and connect), WSL2 connect, and the desktop app's Code
tab on macOS and Windows. Follow [platform-walkthrough.md](platform-walkthrough.md)
and record the result as below.

### Recording a walkthrough

Add a comment to the release's tracking issue (or #30) with the date, the
platform and architecture, `claude --version` (in the desktop app, `/status`
in a Code tab session), the install source (GitHub marketplace, local
marketplace or `--plugin-dir`), the plugin version and each step's result,
then link it from the row above in the same pull request that prepares the
release.

## Known incompatibilities and limitations

**Windows needs Git Bash.** Every hook and skill command is `bash "…"`.
Claude Code runs hooks through Git Bash when Git for Windows is installed,
and through PowerShell otherwise; without Git Bash Claude Code also does not
register its Bash tool (hooks reference), so the `Bash` matchers never fire
and the skills that run through Bash are unavailable. A `bash` on `PATH`
that is WSL's `bash.exe` launcher is not Git Bash and does not work either.
Install Git for Windows, or point `CLAUDE_CODE_GIT_BASH_PATH` at its
`bash.exe`. The hooks' Python is the Windows `python3`/`python` (3.10+), not
Git Bash's.

**WSL2.** Install and run Claude Code inside the distribution: it is then a
Linux install with the `linux-amd64` runtime, and the CI row above covers its
hooks. Notes:

- Keep projects and `~/.claude` in the Linux filesystem. Under a Windows drive mount (`/mnt/<drive>`) file
  modes are Windows', so the owner-only checks on the bytecode cache and the
  session store fall back (no cache) and everything is slower. CI clones into
  the distribution for this reason.
- Connect saves credentials in the Secret Service, which a WSL distribution
  usually does not run. Without one, connect cannot save the credential and
  status reports `connection_unverified` ("Unlock the OS credential store and
  retry"); there is no plaintext fallback ([failure-behaviour.md](failure-behaviour.md)).
  A gnome-keyring in a D-Bus session is what the Linux CI job uses.
- Connect opens the setup page with `xdg-open`. In WSL that needs something
  that reaches the Windows browser (for example `wslu`'s `wslview` as the
  `BROWSER`); otherwise connect says the browser could not be opened. The
  page is on `127.0.0.1` inside WSL, which Windows reaches through WSL2's
  localhost forwarding (on by default).
- The desktop app's WSL sessions do not support plugins (desktop WSL
  documentation); use the CLI inside WSL.

**macOS: bash 3.2 and Python 3.9.** Hooks run `bash` from `PATH`, which is
`/bin/bash` 3.2 unless a newer bash comes first. The plugin supports 3.2: it
lacks `$EPOCHREALTIME`, so a hook's reply limit is 4 s after the interpreter
started rather than 4.5 s after `py.sh` launched
([token-optimization.md](token-optimization.md#hook-runtime-hookslibhookpy-hookslibrunpy)),
and `tests/private-python.test.sh` is written for 3.2's `set -e`. GitHub's
macOS runners put a newer bash first, so CI does not exercise 3.2 in hooks.
The hooks need Python 3.10+; the Command Line Tools' `/usr/bin/python3` is
3.9, so on a Mac with only that the trim hooks stay off and the session
start names the Python found. Capture does not need Python.

**Desktop app.** The Code tab runs its own copy of Claude Code, updated by the
app and versioned separately from the terminal's `claude`. Plugins install in
local and SSH sessions, not in cloud or WSL sessions. Connect's version check
runs `claude --version` from the session's `PATH`, not the app's copy, so it
needs a `claude` 2.1.280+ on that `PATH`; the walkthrough checks this.

**Not covered anywhere yet:** a released runtime installed from the GitHub
marketplace on Linux or Windows, and production (`orkmeter.com`).

## Release checklist

Before the human who merges the marketplace pointer for a release
([release.md](release.md), "Cutting a release", step 13) merges it:

1. Confirm the last nightly `ci` run on `main` passed every row above (it
   runs every `native-host` leg, `wsl` and `unit (ubuntu-24.04-arm)`).
2. Walk [platform-walkthrough.md](platform-walkthrough.md) on each manual row
   (macOS x64, Linux arm64, WSL2 connect, desktop Code tab on macOS and
   Windows) with the release tag installed from the GitHub marketplace, and
   at least one platform on Claude Code 2.1.280.
3. Record each walkthrough ("Recording a walkthrough") and update the
   matrix's links and dates.
4. If Claude Code's latest release changed a feature in the table above, or
   the floor moved in the runtime, update this page, the README and `ci.yml`
   together.
