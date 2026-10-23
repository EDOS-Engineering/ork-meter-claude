---
name: savings
description: Show what the Ork Meter token-trim hooks did this session - interventions, bytes avoided, estimated tokens avoided, and the session's measured token usage from its transcript, each labelled. Use when the person asks about savings, trimmed output, tokens avoided, or what the trim hooks did.
argument-hint: "[--all]"
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/savings.py --session *)
---

# Ork Meter savings

Run this and show the person its output as it is, in a code block:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/savings.py --session "${CLAUDE_SESSION_ID}"
```

If `$ARGUMENTS` is exactly `--all`, run this instead:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/savings.py --session "${CLAUDE_SESSION_ID}" --all
```

For other arguments, explain that only `--all` or no arguments are supported. Never interpolate arbitrary arguments into a shell command.

`hooks/py.sh --script` runs the script with `python3 -I -B` (nothing from the environment or the current directory is imported); errors stay on stderr. If it says `python3` is missing, tell the person the plugin needs Python 3.10 or later on `PATH`.

With `--all`, the script aggregates every session it finds under Claude Code's temp directory on this machine.

It reads only local files under the session scratchpad (`ork-meter/savings.jsonl`, `session.json`, `session-summary.json`) and the session transcript, and sends nothing anywhere.

## How to describe the numbers

- The table is an **estimate**: 4 bytes per token over avoided tool-output bytes. Say so. Don't call it a saving that was measured, a percentage of the bill, or a cost figure.
- "Measured from transcript" sums the `usage` fields of the assistant messages in the session transcript. That is a measurement of what the session used, not of what the hooks saved.
- The script prints no ratio between the estimate and the measurement. Don't compute one or express the estimate as a percentage of usage.
- If a kind is missing from the table, no intervention of that kind happened; that isn't an error.
- The table's total counts only interventions that removed bytes. Haiku agent runs, cache nudges and loop-guard warnings and stops are events: for one session they are listed after the table, and they are never in its total.
- If the script prints a "Duplicate rows" line, some ledger rows repeated a tool call already counted; they are not in the table or its total. Say so; don't add them back.
- If measurement is "unavailable", say why (the script prints the reason) and don't guess.
