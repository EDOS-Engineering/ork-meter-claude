# Platform walkthrough

A manual check of the plugin on one platform and surface, for the manual rows
of the [platform matrix](platform-matrix.md). It takes about 20 minutes and
needs a browser and an Ork Meter test account with Plus or a trial (the trim
steps need Plus). Use the dev platform unless the release says otherwise.

Note what you see at each step. "Expected" is what a pass looks like; record
anything else as a failure with the exact text. Record the result as the
matrix's "Recording a walkthrough" says.

## 0. Prerequisites

| Check | Expected |
|---|---|
| `claude --version` (desktop app: `/status` in a Code tab session, the **Claude Code** row) | `2.1.280 (Claude Code)` or later. Note the version. |
| `python3 --version` (Windows: `python --version`) | 3.10 or later, for the trim hooks. An older one is not a failure: step 2 then shows the Python notice instead. |
| Windows native only: `where bash` in PowerShell, or Git for Windows installed | Git's `bash.exe` (for example `C:\Program Files\Git\bin\bash.exe`), not `C:\Windows\System32\bash.exe` |
| WSL only: run every command inside the distribution, in a folder under `~`, not a Windows drive mount (`/mnt/<drive>`) | `uname -r` mentions `microsoft-standard-WSL2` |
| Desktop app only: the terminal's `claude --version` | 2.1.280 or later (connect checks the `claude` on `PATH`, not the app's copy) |

Start from a clean state: no `ork-meter@edos` in `claude plugin list`. To
keep your own setup untouched on the CLI, run the whole walkthrough with
`CLAUDE_CONFIG_DIR` set to a new empty directory and sign in there.

## 1. Install from the marketplace

In a Claude Code session (desktop app: **+** next to the prompt box, then
**Add plugins**, and add the marketplace and plugin there):

```text
/plugin marketplace add EDOS-Engineering/ork-meter-claude
/plugin install ork-meter@edos
```

Then, from a shell:

```sh
claude plugin list
```

Expected: `ork-meter@edos` listed, enabled, with the version of the release
tag the marketplace names (README, "Connect"). No error mentions hooks or
skills.

## 2. SessionStart

Quit Claude Code and start a new session in a project folder.

Expected, first session after the install: one notice, `Ork Meter: setting up
its runtime in the background; it is ready from your next session.` Then quit
and start another session. Expected from then on: one short Ork Meter line
asking you to run `/ork-meter:connect` once (not connected yet), and nothing
else from the plugin, except one line naming the Python found if step 0's
Python was older than 3.10.

On the CLI, the same check without the interface:

```sh
claude -p --output-format stream-json --verbose "Say ok." | grep '"subtype":"hook_started"' | grep -c '"hook_name":"SessionStart:startup"'
```

Expected: `3`, the plugin's three `startup` hooks (with a clean
`CLAUDE_CONFIG_DIR`, no other hooks are configured).

## 3. Status, then connect

```text
/ork-meter:status
```

Expected before connecting: not connected, with connecting as the next step.
It asks for no reference or code.

```text
/ork-meter:connect dev
```

Expected: a local setup page opens in the browser. Choose the plan, confirm
telemetry permission, sign in to Ork Meter and approve the connection; the
browser returns to the plugin by itself. Restart Claude Code once, run a short
prompt, wait a minute and run `/ork-meter:status` again.

Expected: `state` is `connected_waiting_for_usage` or `receiving_usage`,
`authorization` is `verified` and `receiver` is `ready`; with
`receiving_usage`, `latest_export` shows a recent acknowledgement.

Known WSL failures to record rather than work around: the browser does not
open (no `xdg-open` that reaches Windows), or the credential cannot be saved
(no Secret Service); see [platform-matrix.md](platform-matrix.md), "WSL2".

## 4. A trim and its recovery

Needs Plus on the connected account (step 3). In a session, ask:

```text
Run `seq 1 5000` with the Bash tool and tell me the last line it printed.
```

Expected: Claude answers `5000`. The tool result it saw is shorter than the
23,893 bytes the command printed: a head and a tail with an omission marker,
ending in `Full output: <path> (23893 bytes)`. The path's file name is a
64-character hex ID. (Expand the tool result in the interface, or use
`--output-format stream-json` on the CLI, to see it.)

Then recover lines from it without rerunning the command:

```text
/ork-meter:recover <the 64-character ID> 2000-2005
```

Expected: exactly the lines `2000` to `2005`, and a note of the range and
the total. Then:

```text
/ork-meter:savings
```

Expected: at least one trim in this session, labelled as an estimate, next to
the session's measured usage, with no percentage saved.

## 5. Disconnect and uninstall

```text
/ork-meter:disconnect
```

Expected: capture stops, the plugin's Claude settings and local credentials
are removed, and a page opens to review the remote revocation.
`/ork-meter:status` then reports not connected.

```sh
claude plugin uninstall ork-meter@edos
claude plugin marketplace remove edos
claude plugin list
```

Expected: `ork-meter@edos` is gone. Claude Code asks before deleting the
plugin's data directory; either answer is fine. Start a new session: no Ork
Meter notice. Your `~/.claude/settings.json` holds no Ork Meter keys.

If you used a throwaway `CLAUDE_CONFIG_DIR`, delete it.
