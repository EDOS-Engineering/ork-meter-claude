---
name: lean
description: Install, remove or check the opt-in lean main agent (fewer turns, targeted reads) for this project. Use when the person asks for the lean agent.
argument-hint: "install|remove|status"
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/lean_agent.py --cwd "${CLAUDE_PROJECT_DIR}" --install), Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/lean_agent.py --cwd "${CLAUDE_PROJECT_DIR}" --remove), Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/lean_agent.py --cwd "${CLAUDE_PROJECT_DIR}" --status)
---

# Ork Meter lean agent

The person explicitly asked for this. If `$ARGUMENTS` is exactly `install`, copy the agent into this project:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/lean_agent.py --cwd "${CLAUDE_PROJECT_DIR}" --install
```

If `$ARGUMENTS` is exactly `remove`, delete it:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/lean_agent.py --cwd "${CLAUDE_PROJECT_DIR}" --remove
```

If `$ARGUMENTS` is exactly `status`, or empty, report what is installed without changing anything:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/lean_agent.py --cwd "${CLAUDE_PROJECT_DIR}" --status
```

For other arguments, explain that only `install`, `remove` and `status` are supported. Never interpolate arbitrary arguments into a shell command.

`hooks/py.sh --script` runs the script with `python3 -I -B`; errors stay on stderr. If it says `python3` is missing, tell the person the plugin needs Python 3.10 or later on `PATH`.

What it does:

- `install` writes exactly two files: `.claude/agents/ork-meter-lean.md` (the agent, a copy of the plugin's `docs/optional-agents/lean.md`) and, unless one exists, `.claude/agents/.gitignore`, which ignores only those two files. It refuses to overwrite an `ork-meter-lean.md` it did not write and refuses to write through a symlinked `.claude` or `agents` directory. Running it again updates an older copy and is otherwise a no-op.
- It never reads or edits any settings file and turns nothing on. The agent is used only in a session the person starts with `claude --agent ork-meter-lean`, or by default in this project if they add `"agent": "ork-meter-lean"` to `.claude/settings.local.json` themselves.
- It runs no model and sends nothing anywhere.

Report the script's output in a few short lines, including how to start a session with the agent. Mention that `--agent` replaces Claude Code's default system prompt with the agent's (which restates the essential ground rules), and that `claude --append-system-prompt-file .claude/agents/ork-meter-lean.md` keeps the default prompt and adds the lean rules instead. Mention that while installed, the agent's one-line description is listed in every session in this project. Don't promise a token saving: it has not been measured yet. Don't open or summarise the agent file unless the person asks.
