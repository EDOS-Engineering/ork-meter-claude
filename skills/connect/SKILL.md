---
name: connect
description: Connect once with browser sign-in and consent, then capture token counters automatically in normal Claude Code sessions. Works when the browser is on another computer too.
argument-hint: "[dev]"
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py connect --environment production), Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py connect --environment dev), Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py prepare), Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py prepare --plan pro-max --permitted yes), Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py prepare --plan team-enterprise --permitted yes), Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py prepare --plan api --permitted yes)
---

# Connect to Ork Meter

If `$ARGUMENTS` is exactly `dev`, run:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py connect --environment dev
```

With no arguments, run:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py connect --environment production
```

For other arguments, explain that only `dev` or no arguments are supported.
Never interpolate arbitrary arguments into a shell command.

## In a browser on this computer

`setup_opened`: the command opened a local setup page. The person selects
their Anthropic plan, confirms telemetry permission, signs in to Ork Meter and
approves pairing in their browser. Approval returns directly to the plugin.
No helper download, claim-code copying, reference ID or terminal launch
command is needed. `setup_opened` means waiting for the person, not that
pairing or delivery succeeded. After browser setup completes, they restart
Claude once and then launch it normally. `/ork-meter:status` checks actual
receiving evidence. The first session after an install may already have opened
this page by itself (first-run connect in hooks/session-start.sh); connect
then reuses that setup while it is still waiting.

## Browser on another computer

`setup_pending` means no browser could be opened on this machine (SSH,
devcontainer, cloud). Quote its `message`: it names the loopback setup URL and
the SSH port forward that reaches it, which finishes setup in the person's
browser as above. When the answer also has `"terminal_approval":
"available"`, offer the terminal route instead, which needs no forward. Offer
it too, after quoting the message, if the person says their browser is on
another computer.

The terminal route, only when `terminal_approval` is `available`:

1. Run:

   ```sh
   bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py prepare
   ```

   It answers `consent_required` with the setup page's `plans`,
   `permission`, `destination` and `notes`. Show the person the destination,
   the notes and the permission statement word for word, and ask them to
   choose their plan (`pro-max`: Pro or Max; `team-enterprise`: Team or
   Enterprise; `api`: API key or Console) and to confirm the permission
   statement themselves. Never choose a plan or confirm for them, and stop if
   they don't confirm.
2. With their plan and confirmation, run exactly one of:

   ```sh
   bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py prepare --plan pro-max --permitted yes
   bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py prepare --plan team-enterprise --permitted yes
   bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py prepare --plan api --permitted yes
   ```

3. It answers `approval_pending` with `approval_url`, `fingerprint` and
   `next_step`. Print the approval link in full on its own line and the
   fingerprint in groups of eight characters, then tell the person: open the
   link in a browser on any computer, sign in to Ork Meter, check that the
   security code matches this fingerprint, tick **This browser is not on the
   computer running Claude Code**, approve, then run `/ork-meter:claim <code>`
   here with the approval code the page shows (`XXXX-XXXX`). The setup waits
   30 minutes from `/ork-meter:connect`.

Any other `state` (`connected`, `setup_expired`, `not_completed`,
`setup_unavailable`, `no_terminal_setup`) comes with a `message`: report it
as given. The approval link and fingerprint are public; the approval code is
not: never ask for it here, only through `/ork-meter:claim`.

## Always

Report only the public state and message. Credentials go to the OS store. The
runtime installs narrow metrics settings in Claude's user configuration.
Existing/managed telemetry conflicts stop setup; never advise bypassing
policy. Do not choose a plan or consent, read private settings, accept
secrets, or call internal `headers`/`serve` commands. `headers` returns a
local credential solely to Claude's exporter. Never run legacy `ork-meter` or
`ork-meter.exe`.
