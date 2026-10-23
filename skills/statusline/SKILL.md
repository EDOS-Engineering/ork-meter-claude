---
name: statusline
description: Print the settings.json snippet that turns on the Ork Meter status line - context size, estimated cost of the current turn and of the session at API list prices, and whether the prompt cache has likely expired. Use when the person asks for a cost or context status line.
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/statusline.py --snippet)
---

# Ork Meter status line

Run this and show the person its output as it is, in a `json` code block:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/statusline.py --snippet
```

If it says `python3` is missing, tell the person the plugin needs Python 3.10 or later on `PATH`.

Then explain, briefly:

- A plugin can't turn on a status line itself, so this is opt-in: add the `statusLine` entry to `~/.claude/settings.json` (every project) or `.claude/settings.local.json` (this project only). If they already have a `statusLine`, it replaces it; there is only one.
- It shows, for example, `ctx 84.2k | turn ~$0.41 | session ~$12.30 | cache 1h: 37m left (est.)`:
  - `ctx`: the context size of the latest request, in tokens, measured from the transcript.
  - `turn` and `session`: estimated cost at API list prices of the requests since the last prompt and of the whole session, subagents included. On a subscription these are relative weights, not a bill. A trailing `+` means part of a long transcript is still being read (a lower bound); `?` means an unrecognised model was priced as Opus 5.5.
  - `cache`: the prompt-cache lifetime the usage records show (1 hour or 5 minutes) and the time left. `expired` means the next prompt will likely re-send the whole context at the cache-write price; `/clear` with a short restatement, or a focused `/compact`, avoids most of it.
- The path in the snippet names this plugin version. After a plugin update the line ends with `plugin updated: rerun /ork-meter:statusline`; run this command again and replace the entry.
- It reads only usage counters, model ids, timestamps and record types from Claude Code's local transcripts, never message content, and sends nothing anywhere.
- `refreshInterval` (seconds) keeps the cache countdown current while the session is idle; remove it to update only on new messages.

Don't edit any settings file yourself unless the person asks you to in this conversation. Don't promise a saving.
