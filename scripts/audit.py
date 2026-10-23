"""Static setup audit: what loads into every Claude Code session here.

Usage: audit.py [--cwd <dir>] [--json]

Local only. Reports, with byte and line counts and a 4-bytes-per-token
estimate:

* every CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md and .claude/rules/*.md
  from the working directory up to the filesystem root, plus
  ~/.claude/CLAUDE.md and ~/.claude/rules/*.md (``$CLAUDE_CONFIG_DIR``
  instead of ~/.claude when set, as everywhere below);
* the MCP servers configured in the known files: ~/.claude.json (in
  ``$CLAUDE_CONFIG_DIR`` when set; global
  and for this project), .mcp.json in the working directory and its
  parents, and any mcpServers in the settings files. Only the server
  names are printed, never their commands, arguments or environment.

Then a ranked list of the largest always-loaded items. MCP tool schemas also
load into every session, but their size is only known to the server, so the
audit counts the servers and says so.

Two recommendations from metadata only, each with the counts it is based on
(issue #110):

* **Code-intelligence (LSP) plugins** for the typed languages in the project,
  from file names alone: source-file extensions (a bounded walk that skips
  dependency and build directories) and the presence of config files such as
  ``tsconfig.json``, ``go.mod`` or ``Cargo.toml``. No file is opened. A
  plugin already enabled in the settings is not recommended again.
* **A 1-hour prompt cache**, only when this project's transcripts show the
  main conversation writing the 5-minute cache and never the 1-hour one
  (a subscription within its limits already gets 1 hour), with at least
  ``MIN_IDLE_REWRITES`` requests that came after a 5-60 minute pause and
  re-wrote most of their context, and when, at API list prices, those
  re-writes cost more than the 1-hour cache's write premium would have.
  Usage counters and timestamps only (scripts/cache_report.py).

It also says whether the context-size nudge is on by default for a new
session here and why (hooks/lib/billing.py). Nothing leaves the machine.
"""

import argparse
import glob
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks/lib"))

import billing       # noqa: E402
import features      # noqa: E402
import ledger        # noqa: E402
import project_env   # noqa: E402
import session       # noqa: E402


def _sibling(name):
    """A module from this directory, loaded by path: putting scripts/ on
    sys.path would shadow hooks/lib's ``usage`` with scripts/usage.py."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ork_" + name, os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cache_report = _sibling("cache_report")

CLAUDE_MD_LINE_ADVICE = 200
SETTINGS_FILES = ("settings.json", "settings.local.json")

# Typed languages and the code-intelligence plugin for each, from Anthropic's
# official marketplace (claude-plugins-official): (language, source-file
# extensions, config file names, plugin). Python counts only with a type
# checker's own config file or stub files, since a .py file says nothing
# about types and pyproject.toml would have to be read.
LSP_PLUGINS = (
    ("TypeScript", (".ts", ".tsx", ".mts", ".cts"), ("tsconfig.json",), "typescript-lsp"),
    ("Python (type-checked)", (".pyi",), ("mypy.ini", ".mypy.ini", "pyrightconfig.json", "py.typed"), "pyright-lsp"),
    ("Go", (".go",), ("go.mod",), "gopls-lsp"),
    ("Rust", (".rs",), ("Cargo.toml",), "rust-analyzer-lsp"),
    ("Java", (".java",), ("pom.xml", "build.gradle", "build.gradle.kts"), "jdtls-lsp"),
    ("Kotlin", (".kt",), (), "kotlin-lsp"),
    ("C#", (".cs",), (".csproj", ".sln"), "csharp-lsp"),
    ("C/C++", (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"), ("compile_commands.json", "CMakeLists.txt"), "clangd-lsp"),
    ("Swift", (".swift",), ("Package.swift",), "swift-lsp"),
)
LSP_MARKETPLACE = "claude-plugins-official"
MIN_SOURCE_FILES = 5        # without a config file, this many source files make a language count
WALK_LIMIT = 20000          # directory entries looked at, at most
SKIP_DIRS = {"node_modules", "vendor", "target", "dist", "build", "out", "venv", "__pycache__", "Pods", "third_party"}
MIN_IDLE_REWRITES = 3       # idle re-writes needed before a 1-hour cache is recommended


def file_item(path, scope):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return {"path": path, "scope": scope, "bytes": len(data), "lines": lines, "est_tokens": ledger.est_tokens(len(data))}


def parents(start):
    path = os.path.abspath(start)
    seen = []
    while True:
        seen.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            return seen
        path = parent


def instruction_files(cwd, config):
    items = []
    user_md = os.path.join(config, "CLAUDE.md")
    user_rules = sorted(glob.glob(os.path.join(config, "rules", "*.md")))
    # A project under $HOME walks up through ~: its ~/.claude files are listed
    # once, as the user's.
    user = {os.path.realpath(p) for p in [user_md] + user_rules}
    for d in parents(cwd):
        for name in ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md")):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.path.realpath(p) not in user:
                items.append(file_item(p, "project" if d == os.path.abspath(cwd) else "parent"))
        for p in sorted(glob.glob(os.path.join(d, ".claude", "rules", "*.md"))):
            if os.path.realpath(p) not in user:
                items.append(file_item(p, "project rules" if d == os.path.abspath(cwd) else "parent rules"))
    if os.path.isfile(user_md):
        items.append(file_item(user_md, "user"))
    for p in user_rules:
        items.append(file_item(p, "user rules"))
    return [i for i in items if i]


def server_names(obj):
    servers = obj.get("mcpServers") if isinstance(obj, dict) else None
    return sorted(servers.keys()) if isinstance(servers, dict) else []


def mcp_servers(cwd, global_file, config):
    found = []
    cfg = session.read_json(global_file)
    label = "~/.claude.json" if os.path.dirname(global_file) != config else global_file
    if isinstance(cfg, dict):
        names = server_names(cfg)
        if names:
            found.append({"source": label + " (global)", "servers": names})
        projects = cfg.get("projects")
        if isinstance(projects, dict):
            for key in (os.path.abspath(cwd), cwd):
                names = server_names(projects.get(key))
                if names:
                    found.append({"source": label + " (this project)", "servers": names})
                    break
    for d in parents(cwd):
        p = os.path.join(d, ".mcp.json")
        names = server_names(session.read_json(p))
        if names:
            found.append({"source": p, "servers": names})
        for name in SETTINGS_FILES:
            sp = os.path.join(d, ".claude", name)
            if os.path.realpath(sp) == os.path.realpath(os.path.join(config, name)):
                continue   # ~/.claude settings: listed once, as the user's, below
            names = server_names(session.read_json(sp))
            if names:
                found.append({"source": sp, "servers": names})
    for name in SETTINGS_FILES:
        sp = os.path.join(config, name)
        names = server_names(session.read_json(sp))
        if names:
            found.append({"source": sp, "servers": names})
    return found


def project_languages(cwd, limit=WALK_LIMIT):
    """{language: {"files": n, "config": [names]}} from file names only,
    for the languages in ``LSP_PLUGINS``; ``truncated`` is True when the walk
    stopped at ``limit`` entries. Hidden and dependency directories are skipped."""
    by_ext = {ext: spec[0] for spec in LSP_PLUGINS for ext in spec[1]}
    configs = {name: spec[0] for spec in LSP_PLUGINS for name in spec[2]}
    found, seen, truncated = {}, 0, False
    for directory, dirs, names in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        for name in names:
            seen += 1
            if seen > limit:
                truncated = True
                break
            ext = os.path.splitext(name)[1]
            config = configs.get(name) or (configs.get(ext) if ext in (".csproj", ".sln") else None)
            if config:
                entry = found.setdefault(config, {"files": 0, "config": []})
                if name not in entry["config"]:
                    entry["config"].append(name)
            elif ext in by_ext:
                found.setdefault(by_ext[ext], {"files": 0, "config": []})["files"] += 1
        if truncated:
            break
    return found, truncated


def enabled_plugins(cwd, config):
    """Plugin ids enabled in the user's or this project's settings (names only)."""
    enabled = set()
    for path in [os.path.join(config, n) for n in SETTINGS_FILES] + [os.path.join(cwd, ".claude", n) for n in SETTINGS_FILES]:
        plugins = session.read_json(path)
        plugins = plugins.get("enabledPlugins") if isinstance(plugins, dict) else None
        if isinstance(plugins, dict):
            enabled.update(str(k).split("@", 1)[0] for k, v in plugins.items() if v is True)
    return enabled


def lsp_recommendations(cwd, config):
    found, truncated = project_languages(cwd)
    enabled = enabled_plugins(cwd, config)
    result = []
    for language, extensions, _configs, plugin in LSP_PLUGINS:
        entry = found.get(language)
        if not entry or plugin in enabled or not (entry["config"] or entry["files"] >= MIN_SOURCE_FILES):
            continue
        basis = []
        if entry["files"]:
            basis.append("%d %s file%s" % (entry["files"], "/".join(extensions), "" if entry["files"] == 1 else "s"))
        if entry["config"]:
            basis.append(", ".join(sorted(entry["config"])))
        result.append({"kind": "lsp_plugin", "language": language, "plugin": plugin,
                       "basis": {"source_files": entry["files"], "config_files": sorted(entry["config"]),
                                 "walk_truncated": truncated},
                       "text": "%s detected (%s%s). The %s plugin gives Claude code intelligence (definitions, "
                               "references, type errors) instead of exploratory reads: /plugin install %s@%s." % (
                                   language, "; ".join(basis), "; file walk stopped early" if truncated else "",
                                   plugin, plugin, LSP_MARKETPLACE)})
    return result


def cache_lifetime(config, cwd):
    """The 5-minute vs 1-hour prompt-cache counts for this project's main
    conversations and, when warranted, a recommendation. Usage counters
    and timestamps only (scripts/cache_report.py)."""
    root, _label = cache_report.find_project(config, cwd)
    paths = sorted(glob.glob(os.path.join(glob.escape(root), "**", "*.jsonl"), recursive=True))
    requests, transcripts = cache_report.requests_in_order(paths)
    basis = {"transcripts": transcripts, "requests": 0, "requests_5m": 0, "requests_1h": 0,
             "idle_rewrites": 0, "idle_rewrite_tokens": 0}
    premium = kept = 0.0
    for row, previous, compactions in requests:
        if row["side"]:
            continue   # subagents have their own cache-lifetime setting
        usage = row["usage"]
        split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        hour, five = cache_report.count(split, "ephemeral_1h_input_tokens"), cache_report.count(split, "ephemeral_5m_input_tokens")
        read, write = cache_report.count(usage, "cache_read_input_tokens"), cache_report.count(usage, "cache_creation_input_tokens")
        base, read_price, _out = cache_report.prices(cache_report.priced(row["model"]) or cache_report.FALLBACK_MODEL,
                                                     row["context"])
        basis["requests"] += 1
        basis["requests_1h"] += bool(hour)
        basis["requests_5m"] += bool(five)
        premium += five * 0.75 * base / 1e6   # a 1-hour write costs 2x base input, a 5-minute one 1.25x
        if previous is None or not five:
            continue
        gap = (row["t"] - previous["t"]).total_seconds() / 60
        if (5 < gap <= 60 and write >= cache_report.MISS_SHARE * row["context"]
                and read < cache_report.MISS_SHARE * previous["context"]
                and min(row["context"], previous["context"]) >= cache_report.MIN_CONTEXT
                and not any(previous["t"] < c <= row["t"] for c in compactions)):
            basis["idle_rewrites"] += 1
            basis["idle_rewrite_tokens"] += write
            kept += write * (1.25 * base - read_price) / 1e6   # a write the 1-hour cache would have read instead
    basis["est_1h_write_premium_usd"], basis["est_idle_rewrite_usd"] = round(premium, 2), round(kept, 2)
    if not (basis["requests_5m"] and not basis["requests_1h"] and basis["idle_rewrites"] >= MIN_IDLE_REWRITES
            and kept > premium):
        return basis, None
    text = ("Prompt cache: %d of %d main-conversation requests in %d transcript(s) wrote the 5-minute cache and none "
            "the 1-hour one; %d came after a 5-60 minute pause and re-wrote %s tokens. At API list prices (estimates) "
            "those re-writes cost about $%.2f, more than the $%.2f a 1-hour cache would have added in write premium. "
            "Set \"promptCacheTtl\": \"1h\" in ~/.claude/settings.json (or CLAUDE_CODE_PROMPT_CACHE_TTL=1h)." % (
                basis["requests_5m"], basis["requests"], basis["transcripts"], basis["idle_rewrites"],
                "{:,}".format(basis["idle_rewrite_tokens"]), kept, premium))
    return basis, {"kind": "cache_1h", "basis": basis, "text": text}


def nudge_default(env, config, cwd):
    """Whether the context-size nudge is on for a new session here, and why."""
    name = features.PER_TOKEN_DEFAULTS["context_hygiene"]
    payload = {"cwd": os.path.abspath(cwd)}
    env = project_env.trusted(env, payload)[0]   # as the hooks see it: a project may only turn it off
    if features.explicit(env, name):
        return {"on": features.is_on(env, name), "basis": "set explicitly"}
    root, _label = cache_report.find_project(config, cwd)
    def mtime(path):
        try:
            return os.stat(path).st_mtime
        except OSError:
            return 0
    newest = sorted(glob.glob(os.path.join(glob.escape(root), "*.jsonl")), key=mtime, reverse=True)
    if newest:
        payload["transcript_path"] = newest[0]
    mode, basis = billing.detect(env, payload)
    return {"on": mode == billing.PER_TOKEN, "basis": basis}


def audit(cwd, home, env=None):
    env = os.environ if env is None else env
    config = project_env.config_dir(env, home)
    files = instruction_files(cwd, config)
    servers = mcp_servers(cwd, project_env.global_config_file(env, home), config)
    ranked = sorted(files, key=lambda i: i["bytes"], reverse=True)
    total_tokens = sum(i["est_tokens"] for i in files)
    notes = []
    for i in files:
        if i["lines"] > CLAUDE_MD_LINE_ADVICE and i["path"].endswith("CLAUDE.md"):
            notes.append("%s has %d lines; Anthropic advises keeping CLAUDE.md under %d. Move reference material "
                         "into a skill that loads on demand." % (i["path"], i["lines"], CLAUDE_MD_LINE_ADVICE))
    server_count = sum(len(s["servers"]) for s in servers)
    if server_count:
        notes.append("%d MCP server(s) configured; each server's tool list loads into every session, and its size "
                     "is only known to the server. Disable servers this project doesn't use." % server_count)
    recommendations = lsp_recommendations(cwd, config)
    cache, advice = cache_lifetime(config, cwd)
    if advice:
        recommendations.append(advice)
    notes.extend(r["text"] for r in recommendations)
    return {"cwd": os.path.abspath(cwd), "instruction_files": files, "mcp_servers": servers,
            "ranked": ranked, "est_tokens_per_session": total_tokens,
            "heuristic": "Token figures are estimates (bytes/4), not tokenizer counts.", "notes": notes,
            "recommendations": recommendations, "cache_lifetime": cache,
            "context_nudge_default": nudge_default(env, config, cwd)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--home", default=os.path.expanduser("~"), help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = audit(args.cwd, args.home)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print("Ork Meter setup audit for %s" % result["cwd"])
    print("")
    print("Instruction files loaded into every session here:")
    if not result["instruction_files"]:
        print("  none found")
    for i in result["instruction_files"]:
        print("  %-14s %8s bytes %6s lines %8s est. tokens  %s" % (i["scope"], "{:,}".format(i["bytes"]), i["lines"],
                                                                   "{:,}".format(i["est_tokens"]), i["path"]))
    print("  estimated total: %s tokens per session" % "{:,}".format(result["est_tokens_per_session"]))
    print("")
    print("MCP servers configured (names only):")
    if not result["mcp_servers"]:
        print("  none found in the known files")
    for s in result["mcp_servers"]:
        print("  %s: %s" % (s["source"], ", ".join(s["servers"])))
    print("")
    print("Largest always-loaded items:")
    for n, i in enumerate(result["ranked"][:10], 1):
        print("  %d. %s (%s est. tokens, %d lines)" % (n, i["path"], "{:,}".format(i["est_tokens"]), i["lines"]))
    nudge = result["context_nudge_default"]
    print("")
    print("Context-size nudge for a new session here (with Ork Meter Plus): %s (%s)." % ("on" if nudge["on"] else "off", nudge["basis"]))
    if result["notes"]:
        print("")
        print("Suggestions:")
        for note in result["notes"]:
            print("  - " + note)
    print("")
    print(result["heuristic"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
