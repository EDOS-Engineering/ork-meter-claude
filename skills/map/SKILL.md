---
name: map
description: Generate or refresh a compact local map of this project (layout with sizes, entry points, build and test commands, manifests, conventions, top-level Python symbols) and store it as an on-demand project skill that Claude loads only when it needs orientation. Use when the person asks for a project map or to refresh it.
argument-hint: "[preview|remove]"
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/project_map.py --cwd "${CLAUDE_PROJECT_DIR}" --write), Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/project_map.py --cwd "${CLAUDE_PROJECT_DIR}" --preview), Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/project_map.py --cwd "${CLAUDE_PROJECT_DIR}" --remove)
---

# Ork Meter project map

The person explicitly asked for this. With no arguments, generate or refresh the map:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/project_map.py --cwd "${CLAUDE_PROJECT_DIR}" --write
```

If `$ARGUMENTS` is exactly `preview`, print the map without writing anything:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/project_map.py --cwd "${CLAUDE_PROJECT_DIR}" --preview
```

If `$ARGUMENTS` is exactly `remove`, delete the generated map:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/project_map.py --cwd "${CLAUDE_PROJECT_DIR}" --remove
```

For other arguments, explain that only `preview`, `remove` or no arguments are supported. Never interpolate arbitrary arguments into a shell command.

`hooks/py.sh --script` runs the script with `python3 -I -B`; errors stay on stderr. If it says `python3` is missing, tell the person the plugin needs Python 3.10 or later on `PATH`.

What it does:

- Writes exactly two files: `.claude/skills/ork-meter-map/SKILL.md` (the map, at most 6 KB) and `.claude/skills/ork-meter-map/.gitignore` (so git ignores the generated skill). Nothing else in the project changes. It refuses to overwrite a `SKILL.md` it did not write, and refuses to write through a symlinked `.claude` or `skills` directory.
- Reads only files inside the project directory: no symlinks followed, `.gitignore` and `.git/info/exclude` honoured, vendored and generated directories skipped, and secret-looking files (`.env`, keys, certificates, credentials) never opened.
- Is deterministic and local. It runs no model and sends nothing anywhere; the map never leaves the machine and is never sent to Ork Meter.

Report the script's output in a short line or two. If it says it created `.claude/skills/`, tell the person to run `/reload-skills` (or restart Claude Code) so this session sees the map. Explain that Claude now loads the map only when it needs orientation, that its one-line description is listed in every session in this project, and that `/ork-meter:map` refreshes it after larger changes. To share it with the team, they can delete that `.gitignore` and commit the map. Don't promise a token saving: it has not been measured yet. Don't open or summarise the generated map unless the person asks.
