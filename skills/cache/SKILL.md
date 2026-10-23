---
name: cache
description: Report where this project's Claude Code tokens went - measured cache writes, cache reads and output from the local transcripts - and which prompt-cache misses re-sent whole conversations, by cause (idle past the cache lifetime, model or effort switch, compaction, tool or MCP changes). Use when the person asks why usage is high, about cache misses, or what resuming old sessions costs.
argument-hint: "[--all]"
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/cache_report.py --all), Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/cache_report.py --cwd .)
---

# Ork Meter cache report

Show the person the output as it is, in a code block. If `$ARGUMENTS` is exactly `--all`, cover every project:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/cache_report.py --all
```

With no arguments, cover this project (the script walks up from the current directory to the project's transcript folder, so a subdirectory works):

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/cache_report.py --cwd .
```

For other arguments, explain that only `--all` or no arguments are supported. Never interpolate arbitrary arguments into a shell command.

`hooks/py.sh --script` runs the script with `python3 -I -B`; errors stay on stderr. If it says `python3` is missing, tell the person the plugin needs Python 3.10 or later on `PATH`.

It reads Claude Code's own transcripts on this machine and uses only usage counters, timestamps, model and effort names, request-shape hashes and compaction markers. It never reads message content and sends nothing anywhere.

Then, in a few lines:

- Name the largest cause of cache writes and its share.
- If idle misses lead, explain that a conversation resumed after its prompt cache expired re-sends its whole context at the cache-write price, and that `/clear` with a short restatement, or a focused `/compact` before stepping away, avoids most of it. Mention the opt-in stale-context guard (`ORK_METER_STALE_GUARD=on`) only if the person's plugin version has it.
- Token counts are measured; dollar figures are list-price estimates, relative weights on a subscription. Don't promise a saving.
- Don't change any file or setting. The person decides.
