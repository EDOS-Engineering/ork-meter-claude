---
name: audit
description: Audit what loads into every Claude Code session in this project - CLAUDE.md and rules files with line counts and estimated tokens, configured MCP servers (names only), and a ranked list of the largest always-loaded items. Use when the person asks to audit their setup, why sessions start large, or what CLAUDE.md costs.
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/audit.py --cwd "${CLAUDE_PROJECT_DIR}")
---

# Ork Meter setup audit

Run this from the project directory and show the person its output as it is, in a code block:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/audit.py --cwd "${CLAUDE_PROJECT_DIR}"
```

`hooks/py.sh --script` runs the script with `python3 -I -B` (nothing from the environment or the current directory is imported); errors stay on stderr. If it says `python3` is missing, tell the person the plugin needs Python 3.10 or later on `PATH`.

It reads local files only (CLAUDE.md files up the directory tree, `~/.claude/CLAUDE.md`, rules files, `~/.claude.json`, `.mcp.json`, settings files, the project's file names, and usage counters and timestamps from Claude Code's transcripts), prints MCP server **names only**, never their commands or environment, never reads source files or message content, and sends nothing anywhere.

Then, in a few lines:

- Point out any CLAUDE.md over 200 lines and what in it could move to a skill that loads on demand.
- Point out MCP servers this project doesn't seem to use.
- Pass on each recommendation (LSP plugins, 1-hour prompt cache) with the basis it prints; don't add recommendations the audit didn't make.
- Call the token figures estimates (bytes/4). Don't promise a saving; say what would stop loading.
- Don't change any file. The person decides.
