---
name: connect
description: Connect once with browser sign-in and consent, then capture token counters automatically in normal Claude Code sessions.
argument-hint: "[dev]"
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/runtime.sh" --plugin-data "${CLAUDE_PLUGIN_DATA}" connect --environment production), Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/runtime.sh" --plugin-data "${CLAUDE_PLUGIN_DATA}" connect --environment dev)
---

# Connect to Ork Meter

If `$ARGUMENTS` is exactly `dev`, run:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/runtime.sh" --plugin-data "${CLAUDE_PLUGIN_DATA}" connect --environment dev
```

With no arguments, run:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/runtime.sh" --plugin-data "${CLAUDE_PLUGIN_DATA}" connect --environment production
```

For other arguments, explain that only `dev` or no arguments are supported.
Never interpolate arbitrary arguments into a shell command.

The command opens a local setup page. The person selects their Anthropic plan,
confirms telemetry permission, signs in to Ork Meter and approves pairing in
their browser. Approval returns directly to the plugin. No helper download,
claim-code copying, reference ID or terminal launch command is needed.

Report only the public state/message. `setup_opened` means waiting for the
person, not that pairing or delivery succeeded. After browser setup completes,
they restart Claude once and then launch it normally. `/ork-meter:status`
checks actual receiving evidence.

Credentials go to the OS store. The runtime installs narrow metrics settings in
Claude's user configuration. Existing/managed telemetry conflicts stop setup;
never advise bypassing policy. Do not choose a plan or consent, read private
settings, accept secrets, or call internal `headers`/`serve` commands. `headers`
returns a local credential solely to Claude's exporter. Never run legacy
`ork-meter` or `ork-meter.exe`.
