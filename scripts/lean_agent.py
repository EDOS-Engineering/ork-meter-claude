"""Install, remove or report the opt-in lean main agent for one project.

Usage: lean_agent.py --cwd <project-dir> (--install | --remove | --status)

--install copy docs/optional-agents/lean.md to
          <project>/.claude/agents/ork-meter-lean.md, plus a .gitignore in that
          directory (unless one exists) that ignores only the files written here;
--remove  delete those files (only ones this script wrote);
--status  say what is installed, changing nothing.

Nothing is enabled by this: the agent runs only in a session started with
`claude --agent ork-meter-lean`, or as the project default if the person sets
that themselves. This script never reads or writes a settings file, refuses to
write through a symlinked `.claude` or `agents` directory, and never overwrites
or removes a file it did not write (the definition carries MARKER). It reuses
scripts/project_map.py's safe-write helpers. Nothing is sent anywhere.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "optional-agents" / "lean.md"
AGENT_NAME = "ork-meter-lean"
AGENT_DIR = (".claude", "agents")
FILENAME = AGENT_NAME + ".md"
MARKER = "<!-- ork-meter:lean-agent v1 -->"
GITIGNORE_TEXT = ("# Written by /ork-meter:lean. Ignores only the agent it installed.\n"
                  "/%s\n/.gitignore\n" % FILENAME)
REL = "/".join(AGENT_DIR + (FILENAME,))


def _load_project_map():
    spec = importlib.util.spec_from_file_location("ork_lean_project_map", ROOT / "scripts" / "project_map.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pm = _load_project_map()
LeanError = pm.MapError


def definition():
    """The shipped definition, checked before it is copied anywhere."""
    text = SOURCE.read_text(encoding="utf-8")
    front = text.split("---", 2)[1] if text.startswith("---\n") else ""
    if "\nname: %s\n" % AGENT_NAME not in front or MARKER not in text[:4096]:
        raise LeanError("the shipped agent definition %s is malformed" % SOURCE)
    return text


def _read(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def _gitignore_state(directory):
    path = os.path.join(directory, ".gitignore")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISREG(info.st_mode) and _read(path) == GITIGNORE_TEXT:
        return "ours"
    return "foreign"


def status(root):
    root = os.path.realpath(root)
    directory = pm.safe_dir(root, AGENT_DIR, create=False)
    if directory is None:
        return {"agent": None, "gitignore": None}
    target = os.path.join(directory, FILENAME)
    state = pm._ours(target, MARKER)
    if state is None:
        agent = None
    elif state is False:
        agent = "foreign"
    else:
        agent = "current" if _read(target) == definition() else "outdated"
    return {"agent": agent, "gitignore": _gitignore_state(directory)}


def install(root):
    text = definition()
    root = os.path.realpath(root)
    directory = pm.safe_dir(root, AGENT_DIR, create=True)
    target = os.path.join(directory, FILENAME)
    state = pm._ours(target, MARKER)
    if state is False:
        raise LeanError("%s exists and was not written by /ork-meter:lean; not overwriting it" % REL)
    if _gitignore_state(directory) is None:
        pm._write_atomic(directory, ".gitignore", GITIGNORE_TEXT)
    previous = _read(target) if state else None
    if previous != text:
        pm._write_atomic(directory, FILENAME, text)
    return {"changed": previous != text, "updated": previous is not None and previous != text,
            "gitignore": _gitignore_state(directory)}


def remove(root):
    root = os.path.realpath(root)
    directory = pm.safe_dir(root, AGENT_DIR, create=False)
    if directory is None:
        return False
    target = os.path.join(directory, FILENAME)
    state = pm._ours(target, MARKER)
    if state is False:
        raise LeanError("%s was not written by /ork-meter:lean; not removing it" % REL)
    deleted = []
    if state:
        pm._unlink(target)
        deleted.append(FILENAME)
    if _gitignore_state(directory) == "ours":
        pm._unlink(os.path.join(directory, ".gitignore"))
        deleted.append(".gitignore")
    if deleted:
        pm._rmdir_if_empty(directory, deleted)
    return bool(state)


USAGE = ("Use it for one session: `claude --agent %s`. It replaces Claude Code's default system prompt with its own "
         "(which restates the essential ground rules); to keep the default prompt and add the lean rules instead, start "
         "`claude --append-system-prompt-file %s`. To make it this project's default, add `\"agent\": \"%s\"` to "
         ".claude/settings.local.json yourself; this command never edits settings." % (AGENT_NAME, REL, AGENT_NAME))
OVERHEAD = ("While installed, its one-line description is listed as an available agent in every session in this "
            "project. Savings are not measured yet.")
GITIGNORE_NOTE = {"ours": "A .gitignore in that directory keeps it out of git.",
                  "foreign": ".claude/agents/.gitignore is not ours and was left alone; add `%s` to it to keep the "
                             "agent out of git." % FILENAME,
                  None: "No .gitignore: git may pick the agent up."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cwd", required=True, help="project directory")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--remove", action="store_true")
    mode.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    root = os.path.realpath(args.cwd)
    try:
        if not os.path.isdir(root):
            raise LeanError("not a directory: %s" % args.cwd)
        if args.remove:
            print("Removed %s." % REL if remove(root) else "The lean agent is not installed here; nothing removed.")
            return 0
        if args.status:
            state = status(root)
            agent = state["agent"]
            if agent is None:
                print("Not installed: no %s. `/ork-meter:lean install` adds it." % REL)
            elif agent == "foreign":
                print("%s exists but was not written by /ork-meter:lean; it is left alone." % REL)
            else:
                print("Installed: %s (%s)." % (REL, "current" if agent == "current" else
                                                "older than this plugin's version; `/ork-meter:lean install` updates it"))
                print(GITIGNORE_NOTE[state["gitignore"]])
                print(USAGE)
            return 0
        result = install(root)
        print("%s %s as the agent %r." % ("Updated" if result["updated"] else "Installed" if result["changed"]
                                          else "Unchanged:", REL, AGENT_NAME))
        print(GITIGNORE_NOTE[result["gitignore"]])
        print(USAGE)
        print(OVERHEAD + " `/ork-meter:lean remove` deletes it.")
        return 0
    except (LeanError, OSError) as exc:
        print("ork-meter: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
