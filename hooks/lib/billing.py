"""Does this session pay per token? From metadata only.

The context-size nudge (``context_hygiene``) is on by default when the
session is billed per token (an API key, a cloud provider, or a subscription
past its usage limits on usage credits) and off on a subscription. Only the
session pin (hooks/lib/policy.py) asks, once, when it is first written, so
the default never changes mid-session. ``detect`` returns ``(mode, basis)``:
``mode`` is ``PER_TOKEN``, ``SUBSCRIPTION`` or ``UNKNOWN`` and ``basis``
one line saying what it saw. The first signal that answers wins:

1. **The prompt-cache lifetime Claude Code chose** in this project's recent
   transcripts. With no override, Claude Code writes the main conversation's
   cache with a 1-hour lifetime on a subscription within its usage limits
   and a 5-minute one on an API key, Bedrock, Vertex or Foundry (Claude
   Code 2.1.302's ``promptCacheTtl`` setting description), so the newest
   main-conversation write's ``cache_creation`` split
   (``ephemeral_1h_input_tokens`` or ``ephemeral_5m_input_tokens``) is
   Claude Code's own answer. Usage counters only: the current transcript,
   then the newest ``RECENT_FILES`` others in its folder modified within
   ``RECENT_SECONDS``, at most ``TAIL_BYTES`` from the end of each.
   Skipped when the lifetime was chosen by hand (``TTL_ENV`` set, or
   ``promptCacheTtl`` in user or managed settings): then it says nothing
   about billing. Also skipped when a project's settings set an auth,
   cloud-provider or cache-lifetime variable, ``apiKeyHelper`` or
   ``promptCacheTtl`` (or can't be read): a project could otherwise make
   its sessions bill per token and so turn the nudge on.
2. **Claude Code's auth configuration**, by name: ``apiKeyHelper`` in user
   or managed settings, ``CLAUDE_CODE_USE_BEDROCK``/``_VERTEX``/``_FOUNDRY``
   on, or ``ANTHROPIC_API_KEY``/``ANTHROPIC_AUTH_TOKEN`` present. A key's
   value is never read, logged or compared; only whether the name is set.
   Names a project's settings files set are not counted (a project may only
   turn levers off), nor is any name when a project file can't be read.
3. Otherwise ``UNKNOWN``, which keeps the lever off.

The plan the person declared in ``/ork-meter:connect`` would come first, but
the plugin runtime keeps it with the pairing record in the OS credential
store, which hooks never read; no plugin-data file carries it today.

Never raises: any error gives ``UNKNOWN``.
"""

import glob
import json
import os
import time

import project_env
import session

PER_TOKEN = "per_token"
SUBSCRIPTION = "subscription"
UNKNOWN = "unknown"

TTL_ENV = ("CLAUDE_CODE_PROMPT_CACHE_TTL", "ENABLE_PROMPT_CACHING_1H", "FORCE_PROMPT_CACHING_5M")
TTL_SETTING = "promptCacheTtl"
HELPER_SETTING = "apiKeyHelper"
PROVIDER_ENV = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
KEY_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
ON = ("1", "true", "yes", "on")
RECENT_FILES = 3
RECENT_SECONDS = 7 * 24 * 3600
TAIL_BYTES = 256 * 1024
SETTINGS_BYTES = 64 * 1024
BUDGET_SECONDS = 0.25


def _settings_keys(path):
    """The top-level keys of a settings file; empty when missing or bad."""
    try:
        data = json.loads(session.read_bounded(path, SETTINGS_BYTES).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return set()
    return set(data) if isinstance(data, dict) else set()


def _trusted_settings(env):
    """User then managed settings files (never a project's)."""
    paths = [project_env.user_settings_path(env)] + project_env.managed_files()
    return [p for p in paths if p]


def ttl_chosen(env):
    """True when the person's or managed settings chose the cache lifetime
    by hand, so it isn't a billing signal."""
    if any(name in env for name in TTL_ENV):
        return True
    return any(TTL_SETTING in _settings_keys(p) for p in _trusted_settings(env))


def project_steers_billing(env, payload):
    """True when a project's settings could set how this project's sessions
    are billed or cached (an auth or cloud-provider variable, a cache-lifetime
    override, ``apiKeyHelper`` or ``promptCacheTtl``), or can't be read. Its
    sessions' cache lifetime then reflects the project's choice, so reading it
    as billing would let a project turn the nudge on."""
    files = project_env.project_files(env, payload)
    names = project_env.project_names(env, payload)
    if files is None or names is None:
        return True
    watched = {project_env._key(n) for n in KEY_ENV + PROVIDER_ENV + TTL_ENV}
    if watched & names:
        return True
    return any({HELPER_SETTING, TTL_SETTING} & _settings_keys(p) for p in files)


def cache_ttl(path, deadline):
    """``"1h"`` or ``"5m"``: the lifetime of the newest main-conversation
    cache write in the last ``TAIL_BYTES`` of the transcript at ``path``;
    None when there is none. Usage counters only, never content."""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - TAIL_BYTES))
            tail = stream.read(TAIL_BYTES)
    except (OSError, TypeError, ValueError):
        return None
    lines = tail.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]   # the first line may be cut
    for raw in reversed(lines):
        if time.monotonic() > deadline:
            return None
        if b'"ephemeral_' not in raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "assistant" or row.get("isSidechain"):
            continue
        message = row.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        split = usage.get("cache_creation") if isinstance(usage, dict) else None
        if not isinstance(split, dict):
            continue
        hour, five = split.get("ephemeral_1h_input_tokens"), split.get("ephemeral_5m_input_tokens")
        if type(hour) is int and hour > 0:
            return "1h"
        if type(five) is int and five > 0:
            return "5m"
    return None


def transcripts(payload, now=None):
    """The session's transcript, then the newest others in its folder."""
    current = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not isinstance(current, str) or not os.path.isabs(current):
        return []
    now = time.time() if now is None else now
    others = []
    for path in glob.glob(os.path.join(glob.escape(os.path.dirname(current)), "*.jsonl")):
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if path != current and now - mtime <= RECENT_SECONDS:
            others.append((mtime, path))
    return [current] + [p for _, p in sorted(others, reverse=True)[:RECENT_FILES]]


def observed(env, payload, deadline):
    if ttl_chosen(env) or project_steers_billing(env, payload):
        return None
    for path in transcripts(payload):
        ttl = cache_ttl(path, deadline)
        name = "this session's transcript" if path == payload.get("transcript_path") else "a recent transcript in this project"
        if ttl == "5m":
            return PER_TOKEN, "Claude Code used the 5-minute prompt cache in %s (API key, cloud provider or usage credits)" % name
        if ttl == "1h":
            return SUBSCRIPTION, "Claude Code used the 1-hour prompt cache in %s (subscription within its limits)" % name
    return None


def configured(env, payload):
    for path in _trusted_settings(env):
        if HELPER_SETTING in _settings_keys(path):
            return PER_TOKEN, "apiKeyHelper is set in %s" % path
    planted = project_env.project_names(env, payload)
    if planted is None:
        return None

    def own(name):
        return name in env and project_env._key(name) not in planted
    for name in PROVIDER_ENV:
        if own(name) and str(env.get(name, "")).strip().lower() in ON:
            return PER_TOKEN, "%s is on" % name
    for name in KEY_ENV:
        if own(name):   # presence only; the value is never read
            return PER_TOKEN, "%s is set" % name
    return None


def detect(env, payload=None, deadline=None):
    """``(mode, basis)`` for this session; see the module docstring."""
    try:
        payload = payload if isinstance(payload, dict) else {}
        deadline = time.monotonic() + BUDGET_SECONDS if deadline is None else deadline
        return (observed(env, payload, deadline) or configured(env, payload)
                or (UNKNOWN, "no billing signal found"))
    except Exception:
        return UNKNOWN, "billing could not be determined"
