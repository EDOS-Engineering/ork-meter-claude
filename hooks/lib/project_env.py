"""A project's settings may only turn Ork Meter levers off (#143 item 4).

Claude Code merges the ``env`` block of a project's ``.claude/settings.json``
(and ``.claude/settings.local.json``, which a repository can ship too) into
the hook process environment. A hook can't tell from that environment where
a value came from, so an untrusted repository could otherwise turn a lever on,
for example ``ORK_METER_DELTA=on`` or ``ORK_METER_RECORD_HANDLES=1``.

``trusted(env, payload)`` reads those two files directly (``env`` blocks
only, at most ``MAX_BYTES`` each, cached per process). They are read in
every directory from the payload's ``cwd`` (else the hook's own working
directory) up to its checkout's root, the first holding a ``.git``
directory (a submodule's ``.git`` file doesn't stop it; outside a checkout,
up to the account's home directory or the filesystem root, and never into a
shared directory such as ``/tmp``; at most ``MAX_DEPTH`` levels), so a
nested ``.claude/`` never hides the checkout's own, and in
``$CLAUDE_PROJECT_DIR``, since a project file can set
``$CLAUDE_PROJECT_DIR`` itself. When no working directory is known at all,
the project can't be found and every lever setting is treated as a
project's. For every
lever setting in the registry (``features.LEVERS`` plus ``ORK_METER_MODE``
and ``ORK_METER_RETENTION_DAYS``) a project value is honoured only when it
turns something off or makes it stricter (``honoured``):

* ``ORK_METER_MODE``: only a value that reads as ``off`` (``reduce`` and
  ``shadow`` are ignored);
* an opt-out (``ORK_METER_TRIM`` and the others): only an off spelling;
* an opt-in (``ORK_METER_DELTA`` and the others) and a ``switch`` tunable
  (``ORK_METER_RECORD_HANDLES``): anything but an on spelling;
* a tunable that takes off spellings as unset (``ORK_METER_LOOP_STOP``):
  only an off spelling (never stop);
* ``ORK_METER_RETENTION_DAYS``: never in the environment, which sets the
  age limit for every project's saved output. A project value from 0 up to
  the user's (or managed, or default 7) value applies to this project's own
  session stores only: ``project_retention`` returns it for the one
  project ``retention_project`` picks (``$CLAUDE_PROJECT_DIR``, else the
  cwd's own checkout, never a parent's ``.claude/``), and
  hooks/lib/retention.py pins it in the session's store at its first
  SessionStart.
  A higher or malformed value is ignored and reported;
* any other numeric tunable (budgets, thresholds): never. A number tunes a
  lever rather than turning it off, and every lever has a switch for that.

When a project sets anything else and the live value is not itself honoured,
the setting is resolved as if the project hadn't set it: from the ``env`` of
managed settings (``managed-settings.json`` then ``managed-settings.d/*.json``;
macOS MDM profiles and the Windows registry are not read), else the person's
own settings (``$CLAUDE_CONFIG_DIR/settings.json``, default
``~/.claude/settings.json``), else the built-in default. When a project file
sets ``CLAUDE_CONFIG_DIR`` or a home variable, those come from the account's
home directory instead (on Windows, user settings then count as absent). A
value the shell also exported is treated as the project's when the project
sets it too. A project file that exists but can't be read or parsed makes
every lever setting that isn't honoured untrusted, and the home variables
too; so does a value that is not a string, number or boolean (Claude Code
may still turn ``["on"]`` into ``on``), which is reported even when the
live value is unaffected. On Windows, names compare case-insensitively, as the environment does.
Nothing here raises; the settings whose effect changed are returned so the
session start can say so once.
"""

import glob
import json
import os
import stat
import sys

import features
import session

MAX_BYTES = 64 * 1024
MAX_DEPTH = 32
MAX_DROP_INS = 64
PROJECT_FILES = ("settings.json", "settings.local.json")
# hooks/lib/retention.py imports this module, so the shared values live in features.
RETENTION = features.RETENTION_ENV
RETENTION_DEFAULT, RETENTION_CEILING = features.RETENTION_DEFAULT_DAYS, features.RETENTION_MAX_DAYS
# A value Claude Code would turn into text, but this module can't read the
# same way (an array, an object, null): untrusted, and always reported.
NON_TEXT = None
UNKNOWN_PROJECT = "the project's settings (its directory could not be determined)"
HOME_VARS = ("CLAUDE_CONFIG_DIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")
PROJECT_VAR = "CLAUDE_PROJECT_DIR"
if sys.platform == "darwin":
    MANAGED_FILES = ("/Library/Application Support/ClaudeCode/managed-settings.json",)
elif os.name == "nt":
    MANAGED_FILES = (r"C:\Program Files\ClaudeCode\managed-settings.json",
                     r"C:\ProgramData\ClaudeCode\managed-settings.json")
else:
    MANAGED_FILES = ("/etc/claude-code/managed-settings.json",)

_cache = {}


def _case_insensitive():
    return os.name == "nt"


def _key(name):
    return name.upper() if _case_insensitive() else name


def _names():
    """Every lever setting: name -> (kind, (feature, key) of a tunable)."""
    return features.SETTING_KINDS


def _days(raw):
    return features.parse_int(raw, RETENTION_DEFAULT, 0, RETENTION_CEILING)


def effect(name, raw):
    """What ``raw`` does for ``name``, so equal effects compare equal."""
    kind, tunable = _names().get(name, (None, None))
    probe = {} if raw is None else {name: raw}
    if kind == "mode":
        return features.mode(probe)
    if kind == "retention":
        return _days(raw)
    if tunable is not None:
        return features.value(probe, *tunable)
    if kind == "opt_out":
        return not features.is_off(probe, name)
    return features.is_on(probe, name)


def honoured(name, raw):
    """True when ``raw`` for ``name`` only turns something off or makes it
    stricter. Never for ``ORK_METER_RETENTION_DAYS``: see ``project_retention``."""
    kind = _names().get(name, (None, None))[0]
    probe = {name: raw}
    if kind == "mode":
        return features.mode(probe) == "off"
    if kind == "opt_out":
        return features.is_off(probe, name)
    if kind == "opt_in":
        return not features.is_on(probe, name)
    return False


def _project_days(raw):
    """A project's retention value in days, or None when it isn't a number
    of days ``features.parse_int`` reads (negative, junk)."""
    return features.parse_int(raw, None, 0, RETENTION_CEILING)


def _text(raw):
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (str, int, float)):
        return str(raw)
    return None


def env_block(path):
    """The ``env`` block of a settings file as {name: text}: {} when the file
    or the block is missing; None when the file exists but can't be read, is
    too large, isn't JSON, or it or its ``env`` isn't an object. A value that
    isn't a string, number or boolean maps to ``NON_TEXT``."""
    if path in _cache:
        return _cache[path]
    try:
        raw = session.read_bounded(path, MAX_BYTES)
    except (FileNotFoundError, NotADirectoryError):
        block = {}
    except (OSError, ValueError):   # unreadable, or larger than MAX_BYTES
        block = None
    else:
        try:
            data = json.loads(raw.decode("utf-8"))
            found = data.get("env", {}) if isinstance(data, dict) else None
            if not isinstance(found, dict):
                raise ValueError("settings or its env is not an object")
            block = {_key(k): _text(v) for k, v in found.items() if isinstance(k, str)}
        except (ValueError, RecursionError):
            block = None
    _cache[path] = block
    return block


def _real_home():
    """The account's home directory from a source no settings file can set,
    or None (Windows: user settings then count as absent)."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir or None
    except (ImportError, KeyError, OSError, AttributeError):
        return None


def _configured(env):
    base = env.get("CLAUDE_CONFIG_DIR")
    return base if base and os.path.isabs(base) else None


def config_dir(env, home=None):
    """Claude Code's configuration directory: ``$CLAUDE_CONFIG_DIR`` when it
    is absolute, else ``<home>/.claude`` (``home`` defaults to ``$HOME``,
    else the account's). The one rule for hooks and scripts alike."""
    return _configured(env) or os.path.join(home or env.get("HOME") or os.path.expanduser("~"), ".claude")


def global_config_file(env, home=None):
    """Claude Code's global ``.claude.json``: in ``$CLAUDE_CONFIG_DIR`` when it
    is absolute (the rule of ``config_dir``), else in the home directory."""
    return os.path.join(_configured(env) or home or env.get("HOME") or os.path.expanduser("~"), ".claude.json")


def user_settings_path(env, distrust_home=False):
    if distrust_home:
        home = _real_home()
        return os.path.join(home, ".claude", "settings.json") if home else None
    return os.path.join(config_dir(env), "settings.json")


def managed_files():
    paths = list(MANAGED_FILES)
    for path in MANAGED_FILES:   # drop-ins merge after the file, in name order
        try:
            paths.extend(sorted(glob.glob(os.path.join(glob.escape(os.path.dirname(path)), "managed-settings.d", "*.json")))[:MAX_DROP_INS])
        except (OSError, ValueError):
            pass
    return paths


def _shared(directory):
    """True for a world-writable directory with the sticky bit (``/tmp``):
    anyone can plant a ``.claude/`` there."""
    if os.name == "nt":
        return False
    try:
        mode = os.stat(directory).st_mode
    except (OSError, ValueError):
        return False
    return bool(mode & stat.S_ISVTX and mode & stat.S_IWOTH)


def _lever_directories(directory):
    """``directory`` and every directory above it up to its checkout's root
    (the first holding a ``.git`` directory, included), at most
    ``MAX_DEPTH`` levels; those above ``directory`` only when they hold
    ``.claude/``. Every one of them is a place a repository can plant
    settings, so a nested ``.claude/`` never hides its checkout's own.

    A ``.git`` file (a submodule's or a linked worktree's gitfile) doesn't
    end the walk: the outer checkout's ``.git`` directory does. Outside a
    checkout the walk ends at the account's home directory (``_real_home``,
    included) or the filesystem root, whichever comes first. A shared
    directory above ``directory`` (world-writable and sticky, like
    ``/tmp``) ends it too, unread, unless it is the checkout's root."""
    found, current = [directory], directory
    home = _real_home()
    for _ in range(MAX_DEPTH):
        if os.path.isdir(os.path.join(current, ".git")):
            break
        if home and _same(current, home):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        if _shared(current) and not os.path.isdir(os.path.join(current, ".git")):
            break
        if os.path.isdir(os.path.join(current, ".claude")):
            found.append(current)
    return found


def _cwd(payload):
    """The payload's ``cwd``, else this process's (Claude Code starts hooks
    in it), else None."""
    cwd = _absolute(payload.get("cwd")) if isinstance(payload, dict) else None
    if cwd is None:
        try:
            cwd = _absolute(os.getcwd())
        except (OSError, ValueError):
            cwd = None
    return cwd


def _absolute(value):
    return value if isinstance(value, str) and value and os.path.isabs(value) else None


_real = {}


def _realpath(path):
    """``os.path.realpath``, memoized for this process (a hook is one short process)."""
    if path not in _real:
        _real[path] = os.path.realpath(path)
    return _real[path]


def _same(a, b):
    try:
        return _realpath(a) == _realpath(b)
    except (OSError, ValueError):
        return False


def _blocks(directory, user):
    """[(path, env block or None)] for the project files under ``directory``."""
    found = []
    for name in PROJECT_FILES:
        path = os.path.join(directory, ".claude", name)
        if user is None or not _same(path, user):   # else the project is the person's own config directory
            found.append((path, env_block(path)))
    return found


def trusted(env, payload=None):
    """``(env, ignored)``: ``env`` with every lever setting a project tried to
    turn on resolved from managed or user settings or the default, and the
    ``(name, value, path)`` settings whose effect that changed (``value``
    None when the file couldn't be read). ``env`` itself when nothing did."""
    try:
        names = _names()
        found = _discover(env, payload)
        if found is None:
            return env, ()
        blocks, fallback = found
        lookup = {_key(n): n for n in names}
        untrusted, non_text = {}, set()
        for path, block in blocks:
            for key, raw in (block.items() if block is not None else ((k, None) for k in lookup)):
                name = lookup.get(key)
                if name and (raw is None or not honoured(name, raw)):
                    untrusted.setdefault(name, (raw, path))
                    if block is not None and raw is NON_TEXT:
                        non_text.add(name)
        if not untrusted:
            return env, ()
        result, ignored = dict(env), []
        for name, (raw, path) in sorted(untrusted.items()):
            key = _key(name)
            live = env.get(key)
            if live is None or honoured(name, str(live)):
                if name in non_text:   # malformed: say so even where nothing changed
                    ignored.append((name, raw, path))
                continue   # a stricter value from elsewhere stands
            if key in fallback:
                result[key] = fallback[key]
            else:
                result.pop(key, None)
            if name == RETENTION:
                if project_retention(env, payload) is None:   # not applied to this project's stores either
                    ignored.append((name, raw, path))
                continue
            if name in non_text or effect(name, result.get(key)) != effect(name, live):
                ignored.append((name, raw, path))
        return result, tuple(ignored)
    except Exception:
        # Fail safe: drop every lever setting that isn't an off value.
        try:
            names = {_key(n): n for n in _names()}
            return {k: v for k, v in env.items() if k not in names or honoured(names[k], str(v))}, ()
        except Exception:
            return env, ()


def _lowered(blocks, fallback):
    """The project's ``ORK_METER_RETENTION_DAYS`` when it is at most the
    trusted value (user or managed settings, else the default), else None.
    ``settings.local.json`` overrides ``settings.json``, as in Claude Code."""
    key = _key(RETENTION)
    raw = None
    for _path, block in blocks:   # PROJECT_FILES order: shared, then local
        if block and key in block:
            raw = block[key]
    days = _project_days(raw)
    return days if days is not None and days <= _days(fallback.get(key)) else None


def retention_project(env, payload=None):
    """The one project directory whose settings may lower retention for this
    session's store: ``$CLAUDE_PROJECT_DIR`` when set, else the payload
    ``cwd`` if it holds ``.claude/`` or ``.git``, else its nearest parent
    holding ``.git``. A parent's ``.claude/`` alone is not inherited, so a
    directory in no project of its own gets None. A ``.git`` file counts:
    a submodule (or linked worktree) is its own project here, as it is for
    ``git rev-parse --show-toplevel``, so the outer checkout's value never
    reaches its stores (the lever walk, which only turns things off, still
    reads the outer checkout's settings)."""
    declared = _absolute(env.get(_key(PROJECT_VAR)))
    if declared:
        return declared
    current = _absolute(payload.get("cwd")) if isinstance(payload, dict) else None
    if current is None:
        return None
    if os.path.isdir(os.path.join(current, ".claude")):
        return current
    for _ in range(MAX_DEPTH):
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def project_retention(env, payload=None):
    """Days this session's project (``retention_project``) keeps its own
    stores' saved tool output, when its settings lower
    ``ORK_METER_RETENTION_DAYS`` (0 included) to at most the trusted value;
    None otherwise. Never raises."""
    try:
        directory = retention_project(env, payload)
        found = _discover(env, payload, directory) if directory else None
        if found is None:
            return None
        blocks, fallback = found
        own = [(p, b) for p, b in blocks if _same(os.path.dirname(os.path.dirname(p)), directory)]
        return _lowered(own, fallback)
    except Exception:
        return None


def _discover(env, payload, extra=None):
    """``(blocks, fallback)``: the project settings files' ``env`` blocks as
    ``[(path, block or None)]`` and the trusted (user, then managed) ``env``
    values; None when there are no project files to read. The projects are
    every one from the payload ``cwd`` (else this process's) up to its
    checkout's root (``_lever_directories``), ``$CLAUDE_PROJECT_DIR`` and
    ``extra``, if given. When no working directory is known at all, the
    project can't be found, so one unreadable pseudo-file stands for it:
    every lever setting that isn't honoured is then untrusted."""
    cwd = _cwd(payload)
    directories = _lever_directories(cwd) if cwd else []
    # $CLAUDE_PROJECT_DIR may come from a project file itself, so it never
    # replaces the cwd's project; reading it as well only adds files.
    for candidate in (_absolute(env.get(_key(PROJECT_VAR))), extra):
        if candidate and not any(_same(candidate, d) for d in directories):
            directories.append(candidate)
    # The account's own settings file is never project-level.
    real = _real_home()
    own = os.path.join(real, ".claude", "settings.json") if real else None
    blocks = [b for d in directories for b in _blocks(d, own)]
    if cwd is None:
        blocks.append((UNKNOWN_PROJECT, None))
    if not blocks:
        return None
    home_vars = {_key(v) for v in HOME_VARS}
    distrust_home = any(block is None or home_vars & set(block) for _, block in blocks)
    user = user_settings_path(env, distrust_home)
    blocks = [(p, b) for p, b in blocks if p == UNKNOWN_PROJECT or user is None or not _same(p, user)]
    fallback = {}
    for path in ([user] if user else []) + managed_files():   # managed settings win over the person's
        fallback.update(env_block(path) or {})
    return blocks, fallback


def project_names(env, payload=None):
    """The environment names any of this session's project settings files
    sets (the files ``trusted`` reads), as compared by the environment, so a
    caller can tell a value the project may have planted. None when a
    project file can't be read (every name may be the project's). Never
    raises; an error also gives None."""
    try:
        found = _discover(env, payload)
        if found is None:
            return set()
        names = set()
        for _path, block in found[0]:
            if block is None:
                return None
            names.update(block)
        return names
    except Exception:
        return None


def project_files(env, payload=None):
    """The project settings files ``trusted`` reads for this session (those
    that exist or not), or None when the project can't be determined or on
    any error. Never raises."""
    try:
        found = _discover(env, payload)
        if found is None:
            return []
        paths = [path for path, _block in found[0]]
        return None if UNKNOWN_PROJECT in paths else paths
    except Exception:
        return None


def notice(ignored):
    """One line for the person naming each ignored setting and its file, or
    None. The project's value itself is not echoed."""
    if not ignored:
        return None
    files = {}
    for name, _raw, path in ignored:
        files.setdefault(path, []).append(name)
    parts = ["%s in %s" % (", ".join(names), path) for path, names in files.items()]
    return ("Ork Meter: ignored %s: a project's settings can only turn levers off. "
            "To turn one on, set it in your user settings (~/.claude/settings.json) instead." % "; ".join(parts))
