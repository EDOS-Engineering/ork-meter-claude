"""The lever registry: which levers exist, what switches them off, what
tunes them, and the file that tells the plugin runtime which are on.

``LEVERS`` is the only place a lever's env opt-outs, tunables, defaults
and floors are declared. The hooks ask ``enabled(env, name)`` and
``value(env, name, key)``; nothing else reads an ``ORK_METER_*``
variable (tests/features.test.sh checks). A tunable is a ``Tunable(env,
default, floor, unit, off_means_unset, ceiling)`` and every one is parsed by
``parse_int`` with one grammar: a non-negative integer, optionally followed
by ``k`` or ``kib`` (case-insensitive, times 1024); empty or anything else
gives the default; a value below the floor gives the floor, and one above a
declared ceiling gives the ceiling. A tunable that
declares ``off_means_unset`` (only ``loop_guard.stop``) also takes the
opt-out spellings in ``OFF_VALUES`` (``off``, ``0``, ``false``, ``no``) as
"unset", so ``ORK_METER_LOOP_STOP=0`` means never stop. A tunable with unit
``"switch"`` is an on/off control: ``ON_VALUES`` read 1, ``OFF_VALUES`` read
0, anything else its default.

With Ork Meter Plus the platform's **signed lever policy** (hooks/lib/
entitlement.py ``policy``; the contract is ork-meter docs/plus-policy.md)
replaces the built-in defaults: ``gate`` verifies it and threads it through
``RuntimeEnvironment.policy``, and the pure functions consult it when the
environment carries one. A lever is available only where
``policy["levers"][name]`` is true (missing means off); a tunable's default
is ``policy["tunables"]["lever.key"]`` when that is an integer, still
clamped by the floor and ceiling above. Environment and project settings
keep their meaning on top: an opt-out still turns a lever off, an opt-in
lever needs the policy *and* its switch, an explicit tunable value still
wins over the policy's default. Nothing in the environment turns on a lever
the policy leaves off. Unknown levers or tunables in a policy are ignored.
Without a policy (the legacy entitlement window) the built-in defaults apply.

The plugin runtime (the connect-once runtime; ork-meter
``internal/pluginstate`` is its reader) tags usage snapshots with the
plugin's active levers so a customer's own ledger can compare periods with
and without a lever (ork-meter #209). The plugin publishes the list at
``$XDG_STATE_HOME/ork-meter/plugin-features.json`` (``~/.local/state`` when
XDG_STATE_HOME is unset), owner-only, with exactly three keys::

    {"plugin_version": "0.2.0", "features": ["dedupe", …], "updated_at": "2026-10-13T12:00:00Z"}

Nothing else goes in the file; the reader's rules are listed in
docs/token-optimization.md under "Active-lever file" and checked by
tests/features-contract.test.sh. Each session also publishes its own
levers in ``session-features/<sha256(session_id)>.json`` (same format);
the global file lists the levers that are on in *every* live session, so
parallel sessions with different levers never make it describe the wrong
one (#131).
"""

import collections
import hashlib
import json
import os
import re
import time

import entitlement
import session

KIB = 1024
TRIM_TOOLS = ("Bash", "Read", "Grep", "Glob", "WebFetch")

# env: the variable; default: its value when unset or invalid (None: no value);
# floor: the smallest value it takes; unit: what the number counts;
# off_means_unset: an OFF_VALUES spelling reads as unset (the default);
# ceiling: the largest value it takes (None: no ceiling).
Tunable = collections.namedtuple("Tunable", "env default floor unit off_means_unset ceiling", defaults=(False, None))

# lever -> opt_outs: env variables that switch it off (any of them set to an OFF_VALUES entry);
#          tunables: key -> Tunable.
LEVERS = {
    "trim": {
        "opt_outs": ("ORK_METER_TRIM",),
        "tunables": {
            "record_handles": Tunable("ORK_METER_RECORD_HANDLES", 0, 0, "switch"),
            "budget_bash": Tunable("ORK_METER_TRIM_BUDGET_BASH", 12 * KIB, KIB, "bytes"),
            "budget_read": Tunable("ORK_METER_TRIM_BUDGET_READ", 24 * KIB, KIB, "bytes"),
            "budget_grep": Tunable("ORK_METER_TRIM_BUDGET_GREP", 8 * KIB, KIB, "bytes"),
            "budget_glob": Tunable("ORK_METER_TRIM_BUDGET_GLOB", 4 * KIB, KIB, "bytes"),
            "budget_webfetch": Tunable("ORK_METER_TRIM_BUDGET_WEBFETCH", 16 * KIB, KIB, "bytes"),
        },
    },
    "test_filter": {"opt_outs": ("ORK_METER_TRIM", "ORK_METER_TEST_FILTER"), "tunables": {}},
    "dedupe": {"opt_outs": ("ORK_METER_TRIM", "ORK_METER_DEDUPE"), "tunables": {}},
    "read_guard": {
        "opt_outs": ("ORK_METER_TRIM", "ORK_METER_READ_GUARD"),
        "tunables": {"bytes": Tunable("ORK_METER_READ_GUARD_BYTES", 96 * KIB, 4 * KIB, "bytes"),
                     # Lines kept by a plain page; a file with no more lines is left alone.
                     # The ceiling keeps the page at least 500 lines shorter than Read's
                     # native 2000-line page, so the token gate can still admit it (#145).
                     "lines": Tunable("ORK_METER_READ_GUARD_LINES", 300, 50, "lines", ceiling=1500),
                     # Read compaction (hooks/lib/read_compact.py): a whole-file Read over
                     # ``tokens`` (counted as Claude Code renders it) is compacted first and
                     # paged only when it still doesn't fit. Opt-in (``compact`` defaults to
                     # 0, #316) until the Write guard is hardened against the bypasses
                     # found after #307; the guard runs whenever a compacted record exists.
                     # The ceiling is the largest Read the guard lets through whole for it
                     # (read_compact.DEFER_MAX_TOKENS).
                     "tokens": Tunable("ORK_METER_READ_GUARD_TOKENS", 6000, 1000, "tokens", ceiling=20000),
                     "compact": Tunable("ORK_METER_READ_COMPACT", 0, 0, "switch"),
                     # The Write guard's deliberate override (#319): on lets every Write
                     # through, from the next session. Off (the default) when a pin lacks it.
                     "write_guard_override": Tunable("ORK_METER_WRITE_GUARD_OVERRIDE", 0, 0, "switch")},
    },
    "haiku_agents": {"opt_outs": (), "tunables": {}},   # agent files load with the plugin; no env switch exists
    "cache_nudge": {
        "opt_outs": ("ORK_METER_NUDGES",),
        "tunables": {
            "tokens": Tunable("ORK_METER_CACHE_NUDGE_TOKENS", 40000, 1000, "tokens"),
            "accept_seconds": Tunable("ORK_METER_CACHE_NUDGE_ACCEPT_SECONDS", 300, 1, "seconds"),
        },
    },
    "loop_guard": {
        "opt_outs": ("ORK_METER_LOOP_GUARD",),
        "tunables": {
            "warn": Tunable("ORK_METER_LOOP_WARN", 3, 1, "failures"),
            "stop": Tunable("ORK_METER_LOOP_STOP", None, 1, "failures", off_means_unset=True),
        },
    },
    "audit": {"opt_outs": (), "tunables": {}},          # a command the person runs; nothing automatic
    # Opt-in (off unless ORK_METER_CONTEXT_HYGIENE is on): /clear and /compact
    # suggestions when a conversation's context grows large (issue #60).
    # per_token_default: unset, it is on in a session billed per token
    # (hooks/lib/billing.py, decided once when the session is pinned, #110).
    "context_hygiene": {
        "opt_outs": (),
        "opt_in": "ORK_METER_CONTEXT_HYGIENE",
        "per_token_default": True,
        "tunables": {
            "tokens": Tunable("ORK_METER_CONTEXT_NUDGE_TOKENS", 120000, 20000, "tokens"),
            "step": Tunable("ORK_METER_CONTEXT_NUDGE_STEP", 60000, 10000, "tokens"),
            "accept_seconds": Tunable("ORK_METER_CONTEXT_NUDGE_ACCEPT_SECONDS", 600, 1, "seconds"),
        },
    },
    # Opt-in (off unless ORK_METER_STALE_GUARD is on): hold a prompt that would
    # re-send a large context whose prompt cache has expired (issue #101).
    "stale_guard": {
        "opt_outs": (),
        "opt_in": "ORK_METER_STALE_GUARD",
        "tunables": {
            "tokens": Tunable("ORK_METER_STALE_GUARD_TOKENS", 100000, 20000, "tokens"),
        },
    },
    # Opt-in (off unless ORK_METER_DELTA is on): a re-Read of a changed file or a
    # re-run of the same Bash command is delivered as a diff against the earlier
    # result still in context (hooks/lib/delta.py).
    "delta": {"opt_outs": ("ORK_METER_TRIM",), "opt_in": "ORK_METER_DELTA", "tunables": {}},
}
ALL_FEATURES = tuple(LEVERS)
OFF_VALUES = ("off", "0", "false", "no")
ON_VALUES = ("on", "1", "true", "yes")
MODE_ENV = "ORK_METER_MODE"
# Saved tool-output retention (hooks/lib/retention.py), shared with
# hooks/lib/project_env.py here so neither imports the other for them.
RETENTION_ENV = "ORK_METER_RETENTION_DAYS"


def _setting_kinds():
    """Every lever setting: env name -> (kind, (feature, key) of a tunable or None).

    ``opt_out``: an off spelling turns something off (lever opt-outs, and a
    tunable whose off spelling means unset: never stop); ``opt_in``:
    anything but an on spelling keeps it off (lever opt-ins, and ``switch``
    tunables); ``tunable``: a number that tunes a lever; ``mode`` and
    ``retention``. A project may only turn things off (hooks/lib/
    project_env.py), and a pin missing a switch reads it off
    (hooks/lib/policy.py ``adapt``): both follow this one table."""
    kinds = {MODE_ENV: ("mode", None), RETENTION_ENV: ("retention", None)}
    for feature, lever in LEVERS.items():
        for name in lever["opt_outs"]:
            kinds[name] = ("opt_out", None)
        if lever.get("opt_in"):
            kinds[lever["opt_in"]] = ("opt_in", None)
        for key, tunable in lever["tunables"].items():
            kind = "opt_in" if tunable.unit == "switch" else "opt_out" if tunable.off_means_unset else "tunable"
            kinds[tunable.env] = (kind, (feature, key))
    return kinds


SETTING_KINDS = _setting_kinds()
# Per lever, in declaration order (tests/shared_helpers_regressions.py checks
# each name here has that kind in SETTING_KINDS).
OPT_OUTS = {name: lever["opt_outs"] for name, lever in LEVERS.items()}
OPT_INS = {name: lever["opt_in"] for name, lever in LEVERS.items() if lever.get("opt_in")}
PER_TOKEN_DEFAULTS = {name: lever["opt_in"] for name, lever in LEVERS.items() if lever.get("per_token_default")}
# Levers whose replies replace a tool's input or output, so they need the
# token gate's tokenizer (hooks/lib/tokenizer_check.py).
REPLACING_FEATURES = ("trim", "test_filter", "dedupe", "read_guard", "delta")
RETENTION_DEFAULT_DAYS = 7
RETENTION_MAX_DAYS = 365
AUTOMATIC_FEATURES = ("trim", "test_filter", "dedupe", "read_guard", "cache_nudge", "loop_guard", "context_hygiene", "stale_guard", "delta")
INT_RE = re.compile(r"^(\d+)(k|kib)?$")
UPDATED_AT_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")   # as features_payload writes it
SESSION_FEATURES_DIR = "session-features"
ENDED_DIR = "session-features-ended"   # markers of sessions that sent SessionEnd
LIVE_SECONDS = 2 * 3600    # a live session refreshes its file at least hourly
PRUNE_SECONDS = 48 * 3600  # the reader drops files older than 24 hours anyway
STATE_PRUNE_SECONDS = 30 * 24 * 3600   # a hook's per-session state (session_state_path) left this long
STALE_GUARD_DIR = "stale-guard"   # hooks/stale-context.py's state, pruned by hooks/trim-session.py
FEATURES_FILE_MAX_BYTES = 4096   # a lever file larger than this is malformed: rewritten, never trusted
FALLBACK_VERSION = "0.0.0"   # the reader's version rule is ^\d{1,4}\.\d{1,4}\.\d{1,4}(-…)?$; "unknown" would be dropped


def is_off(env, name):
    return str(env.get(name, "")).strip().lower() in OFF_VALUES


def is_on(env, name):
    return str(env.get(name, "")).strip().lower() in ON_VALUES


# Prompts Claude Code writes itself, not a person: a background task's completion and another session's message.
HARNESS_PROMPTS = ("<task-notification>", "<cross-session-message")


def unattended(env, payload):
    """True when nobody is there to answer a held prompt: Claude Code runs
    non-interactively (``-p``, stream-json or an SDK: ``CLAUDE_CODE_ENTRYPOINT``
    starts with ``sdk``), or the prompt is one Claude Code wrote itself
    (``HARNESS_PROMPTS``). A hook that would block such a prompt lets it through:
    a held prompt there is lost, not resubmitted. Only the prompt's opening
    marker is looked at, never its content."""
    if str(env.get("CLAUDE_CODE_ENTRYPOINT", "")).strip().lower().startswith("sdk"):
        return True
    prompt = (payload or {}).get("prompt")
    return isinstance(prompt, str) and prompt.lstrip().startswith(HARNESS_PROMPTS)


_agents = []   # memo: whether this plugin ships agent files (agents/*.md)


def _has_agents():
    if not _agents:
        directory = os.path.join(os.path.dirname(__file__), "..", "..", "agents")
        _agents.append(os.path.isdir(directory) and any(name.endswith(".md") for name in os.listdir(directory)))
    return _agents[0]


def policy_allows(policy, feature):
    """True when the signed policy ``policy`` (a dict) lists ``feature``
    as available: ``levers[feature]`` is true. Missing means off."""
    levers = policy.get("levers") if isinstance(policy, dict) else None
    return isinstance(levers, dict) and levers.get(feature) is True


def policy_default(policy, feature, key, tunable):
    """``tunable``'s default under ``policy``: ``tunables["feature.key"]``
    when it is an integer (0 or 1 for a ``"switch"``), clamped by the floor
    and ceiling; else the built-in ``tunable.default``."""
    tunables = policy.get("tunables") if isinstance(policy, dict) else None
    raw = tunables.get("%s.%s" % (feature, key)) if isinstance(tunables, dict) else None
    if type(raw) is not int:   # bool is not a count
        return tunable.default
    if tunable.unit == "switch":
        return raw if raw in (0, 1) else tunable.default
    if raw < 0:
        return tunable.default
    clamped = max(raw, tunable.floor)
    return clamped if tunable.ceiling is None else min(clamped, tunable.ceiling)


def policy_id(env):
    """The active signed policy's id for a message (printable, at most 64
    characters), or None when ``env`` carries no policy."""
    policy = getattr(env, "policy", None)
    name = policy.get("id") if isinstance(policy, dict) else None
    if not isinstance(name, str):
        return None
    return "".join(c for c in name if c.isprintable() and not c.isspace())[:64] or None


def enabled(env, feature):
    if feature == "haiku_agents" and not _has_agents():
        return False
    if feature in getattr(env, "disabled_features", ()):
        return False
    policy = getattr(env, "policy", None)
    if policy is not None and not policy_allows(policy, feature):
        return False   # the signed policy leaves it off; nothing in the environment turns it on
    if mode(env) != "reduce" and feature in AUTOMATIC_FEATURES:
        return False
    if feature in OPT_INS and not is_on(env, OPT_INS[feature]):
        return False   # an opt-in lever is off until the person turns it on
    return not any(is_off(env, name) for name in OPT_OUTS.get(feature, ()))


class RuntimeEnvironment(dict):
    """Pinned configuration plus locally unavailable reducer families, the
    entitlement status and the signed lever policy. Wrapping keeps what an
    inner environment already disabled, its entitlement status and its
    policy, so the reducer circuit cannot re-enable gated levers."""
    def __init__(self, env, disabled_features=(), entitlement_status=None, policy=None):
        super().__init__(env)
        self.disabled_features = frozenset(disabled_features) | frozenset(getattr(env, "disabled_features", ()))
        self.entitlement = entitlement_status or getattr(env, "entitlement", None)
        # The verified policy document (hooks/lib/entitlement.py ``policy``)
        # while entitled; None applies the built-in defaults.
        self.policy = policy if policy is not None else getattr(env, "policy", None)


def gate(env, now=None):
    """The live environment with every lever off unless the account has Ork
    Meter Plus, and with the verified signed policy applied while it has
    (hooks/lib/entitlement.py). Env opt-outs still apply on top. Apply only
    to the live environment of a hook or script: this is the one place the
    entitlement and policy files are read; the pure functions here never
    read them."""
    state, policy = entitlement.resolve(env, now)
    if state != entitlement.ENTITLED:
        gated = RuntimeEnvironment(env, ALL_FEATURES, state)
        gated.policy = None
        return gated
    return RuntimeEnvironment(env, (), state, policy)


def carried(env, source):
    """``env`` carrying ``source``'s gate verdict (its disabled levers,
    entitlement and policy) when ``source`` is a ``RuntimeEnvironment`` and
    ``env``, a plain dict derived from it, is not; else ``env`` itself.
    hooks/lib/policy.py keeps the verdict through the project rule and the
    session pin with it, so the pin records the policy's defaults."""
    if isinstance(source, RuntimeEnvironment) and not isinstance(env, RuntimeEnvironment):
        return RuntimeEnvironment(env, source.disabled_features, source.entitlement, source.policy)
    return env


def mode(env):
    """Explicit shadow capture or pass-through; unknown modes fail open."""
    value = str(env.get(MODE_ENV, "reduce")).strip().lower()
    return value if value in ("reduce", "shadow", "off") else "off"


def enabled_features(env=None):
    env = os.environ if env is None else env
    return sorted(f for f in ALL_FEATURES if enabled(env, f))


def parse_int(raw, default, floor, ceiling=None):
    """The one grammar for a tunable: ``<digits>`` or ``<digits>k``/``kib``
    (case-insensitive, times 1024). Empty or invalid gives ``default``;
    below ``floor`` gives ``floor``; above ``ceiling`` (if any) gives ``ceiling``."""
    m = INT_RE.match(str(raw if raw is not None else "").strip().lower())
    if not m:
        return default
    value = max(int(m.group(1)) * (KIB if m.group(2) else 1), floor)
    return value if ceiling is None else min(value, ceiling)


def value(env, feature, key):
    """The tunable ``key`` of ``feature`` from ``env``, parsed by ``parse_int``.
    A tunable with ``off_means_unset`` set to an ``OFF_VALUES`` spelling is
    unset (its default). A ``"switch"`` tunable reads 1 for an ``ON_VALUES``
    spelling, 0 for an ``OFF_VALUES`` one and its default otherwise. The
    default is the signed policy's (``policy_default``) when ``env`` carries
    one, else the built-in; an explicit value in ``env`` wins either way."""
    t = LEVERS[feature]["tunables"][key]
    policy = getattr(env, "policy", None)
    default = t.default if policy is None else policy_default(policy, feature, key, t)
    if t.unit == "switch":
        return 1 if is_on(env, t.env) else 0 if is_off(env, t.env) else default
    if t.off_means_unset and is_off(env, t.env):
        return default
    return parse_int(env.get(t.env), default, t.floor, t.ceiling)


def read_compaction(env):
    """Whether whole-file Reads are compacted (``read_guard``'s ``compact``
    switch): the read guard and the Read result hook (``trim``) both on."""
    return enabled(env, "read_guard") and enabled(env, "trim") and value(env, "read_guard", "compact") == 1


def trim_budget(env, tool):
    """The byte budget for one of ``TRIM_TOOLS``."""
    return value(env, "trim", "budget_" + tool.lower())


def policy_settings(env):
    """Canonical metadata-only settings, including parsed defaults and floors
    (the signed policy's defaults when ``env`` carries one, so a session pin
    taken from a gated environment records them)."""
    settings = {MODE_ENV: mode(env)}
    for feature, lever in LEVERS.items():
        for name in lever["opt_outs"]:
            settings[name] = "off" if is_off(env, name) else "on"
        if lever.get("opt_in"):
            settings[lever["opt_in"]] = "on" if is_on(env, lever["opt_in"]) else "off"
        for key, tunable in lever["tunables"].items():
            parsed = value(env, feature, key)
            settings[tunable.env] = "off" if parsed is None else str(parsed)
    return settings


def explicit(env, name):
    """True when ``name`` is set to an on or off spelling."""
    return is_on(env, name) or is_off(env, name)


def with_billing_defaults(env, per_token):
    """``env`` with each ``per_token_default`` opt-in that isn't set to an
    on or off spelling turned on when the session is billed per token
    (``per_token``). Used once, for the values a new session pin records
    (hooks/lib/policy.py), so the default never changes mid-session; an
    explicit setting always wins."""
    effective = dict(env)
    if per_token:
        for name in PER_TOKEN_DEFAULTS.values():
            if not explicit(env, name):
                effective[name] = "on"
    return effective


def needs_billing(env):
    """True when a ``per_token_default`` opt-in is left to its default."""
    return any(not explicit(env, name) for name in PER_TOKEN_DEFAULTS.values())


def apply_policy_settings(env, settings):
    """Keep pinned settings, while honoring immediate disables/privacy opt-outs."""
    effective = dict(env)
    effective.update(settings)
    # Changing mode cannot turn on reduction or content capture in this session.
    if mode(env) != settings[MODE_ENV]:
        effective[MODE_ENV] = "off"
    per_token = set(PER_TOKEN_DEFAULTS.values())
    for name, (kind, tunable) in SETTING_KINDS.items():
        if kind == "opt_in" and tunable is None and name not in per_token:
            # Turning an opt-in lever off applies at once; turning it on waits
            # for the next session, like every other pinned setting.
            off = not is_on(env, name)
        else:
            # An opt-out, an off switch or never-stop setting applies at once.
            # A lever with a per-token default keeps its pin while unset (the
            # pin holds that default); only an off spelling turns it off.
            off = kind in ("opt_out", "opt_in") and is_off(env, name)
        if off:
            effective[name] = "off"
    return effective


def pass_through_environment(env):
    effective = dict(env)
    effective[MODE_ENV] = "off"
    return effective


def status_line(env):
    """The SessionStart line for the person, from the values the hooks use.
    None when there is nothing to say (not connected: the runtime's own
    session-start line asks the person to connect)."""
    state = getattr(env, "entitlement", None)
    if state == entitlement.NOT_ENTITLED:
        return entitlement.MESSAGE
    if state == entitlement.NOT_CONNECTED:
        return None
    if mode(env) == "shadow":
        return "Ork Meter: shadow capture (local tool inputs/results only; no automatic reduction)."
    if mode(env) == "off":
        return "Ork Meter: automatic optimization off (ORK_METER_MODE)."
    policy = policy_id(env)
    if not enabled(env, "trim"):
        if policy and not policy_allows(env.policy, "trim"):
            return "Ork Meter trim: off (policy %s)." % policy
        return "Ork Meter trim: off (ORK_METER_TRIM=off)."
    budgets = ", ".join("%s %dK" % (tool, trim_budget(env, tool) // KIB) for tool in TRIM_TOOLS)
    guard = ("read guard %dK" % (value(env, "read_guard", "bytes") // KIB)
             if enabled(env, "read_guard") else "read guard off")
    return "Ork Meter trim: on (%s; %s).%s" % (budgets, guard, " Policy %s." % policy if policy else "")


def plugin_version(env=None, plugin_root=None):
    """The version in ``plugin.json`` under ``plugin_root``,
    ``$CLAUDE_PLUGIN_ROOT`` or this file's plugin; ``FALLBACK_VERSION``
    when it can't be read, so the reader still accepts the file."""
    env = os.environ if env is None else env
    root = (plugin_root or env.get("CLAUDE_PLUGIN_ROOT")
            or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), "r", encoding="utf-8") as fh:
            version = json.load(fh).get("version")
    except (OSError, ValueError, AttributeError):
        return FALLBACK_VERSION
    return version if isinstance(version, str) and version else FALLBACK_VERSION


def state_dir(env=None):
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return session.private_dir(os.path.join(base, "ork-meter"))


def _session_key(session_id):
    if isinstance(session_id, str) and 0 < len(session_id) <= 256:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    raise ValueError("invalid session identity")


def features_path(env=None, session_id=None):
    if session_id is not None:
        directory = session.private_dir(os.path.join(state_dir(env), SESSION_FEATURES_DIR))
        return os.path.join(directory, _session_key(session_id) + ".json")
    return os.path.join(state_dir(env), "plugin-features.json")


def session_state_path(env, directory, session_id, create=True):
    """``<state_dir>/<directory>/<session>.json``: one small per-session state
    file of a hook (nothing from the conversation), or None without a usable
    session id. ``create`` makes the directory (0700) first."""
    sid = session.safe_name(session_id)
    if sid is None:
        return None
    base = os.path.join(state_dir(env), directory)
    return os.path.join(session.private_dir(base) if create else base, sid + ".json")


def _is_state_file(name):
    """True for a name ``session_state_path`` gives: ``<safe_name>.json``."""
    return name.endswith(".json") and session.safe_name(name[:-5]) == name[:-5]


def prune_session_state(env, directories, now=None):
    """Delete per-session state files (``session_state_path`` names only, no
    other file) untouched for ``STATE_PRUNE_SECONDS``. A directory that is a
    link, isn't a directory or isn't this user's is never entered; where the
    platform allows, it is opened without following links and its files are
    removed relative to that handle. Never raises."""
    now = time.time() if now is None else now
    try:
        base = state_dir(env)   # private_dir: ours and not a link
    except (OSError, ValueError):
        return
    by_handle = (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
                 and os.unlink in os.supports_dir_fd and os.scandir in os.supports_fd)
    for directory in directories:
        path = session.fs_path(os.path.join(base, directory))
        handle = None
        try:
            if session.check_private(os.lstat(path), directory=True, owner_only=False):
                continue
            if by_handle:
                handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            with os.scandir(path if handle is None else handle) as entries:
                for entry in entries:
                    try:
                        if (_is_state_file(entry.name) and entry.is_file(follow_symlinks=False)
                                and now - entry.stat(follow_symlinks=False).st_mtime > STATE_PRUNE_SECONDS):
                            os.unlink(entry.name if handle is not None else entry.path, dir_fd=handle)
                    except OSError:
                        continue
        except OSError:
            continue
        finally:
            if handle is not None:
                os.close(handle)


def _scan(directory, now):
    """(name, age) of the regular files in ``directory``; files older than
    ``PRUNE_SECONDS`` are deleted on the way."""
    try:
        entries = list(os.scandir(session.fs_path(directory)))
    except OSError:
        return []
    found = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            if not entry.is_file(follow_symlinks=False):
                continue
            age = now - info.st_mtime
            if age > PRUNE_SECONDS:
                os.unlink(entry.path)
                continue
        except OSError:
            continue
        found.append((entry.name, age))
    return found


def live_feature_sets(env=None, now=None):
    """The lever sets of the live sessions: session files refreshed within
    ``LIVE_SECONDS`` whose session hasn't ended. Stale files are pruned."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    base = state_dir(env)
    ended = {name for name, _ in _scan(os.path.join(base, ENDED_DIR), now)}
    sets = []
    for name, age in _scan(os.path.join(base, SESSION_FEATURES_DIR), now):
        if not name.endswith(".json") or age > LIVE_SECONDS or name[:-5] in ended:
            continue
        try:
            listed = json.loads(session.read_bounded(session.fs_path(os.path.join(base, SESSION_FEATURES_DIR, name)),
                                                     FEATURES_FILE_MAX_BYTES))["features"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if isinstance(listed, list):
            sets.append({f for f in listed if f in LEVERS})
    return sets


def _timestamp(seconds):
    """``updated_at``'s form; whole seconds, so text order is time order."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def features_payload(env=None, plugin_root=None):
    env = os.environ if env is None else env
    return {
        "plugin_version": plugin_version(env, plugin_root),
        "features": enabled_features(env),
        "updated_at": _timestamp(time.time()),
    }


def write_features_file(env=None, plugin_root=None, session_id=None, ended=False):
    """Publish ``env``'s levers: the session's own file (with ``session_id``)
    as they are, and the global file (any existing content, well-formed or
    not, is replaced) as the levers on in every live session, this one
    included unless ``ended`` (SessionEnd). With no live session the global
    file lists ``env``'s levers. Returns the global file's path."""
    env = os.environ if env is None else env
    path = features_path(env)
    payload = features_payload(env, plugin_root)
    if session_id is not None:
        session.write_json(features_path(env, session_id), payload)
        marker = os.path.join(state_dir(env), ENDED_DIR, _session_key(session_id))
        if ended:
            session.private_dir(os.path.dirname(marker))
            session.write_private(marker, "")
        else:
            try:
                os.unlink(session.fs_path(marker))
            except OSError:
                pass
    # Serialize scan-and-write so a writer that scanned before another
    # session published can't overwrite that writer's newer result.
    with session._lock(path + ".lock"):
        sets = live_feature_sets(env)
        if session_id is None:
            sets.append(set(payload["features"]))
        listed = sorted(set.intersection(*sets)) if sets else payload["features"]
        session.write_json(path, dict(payload, features=listed))
    return path


def refresh_features_file(env, session_id):
    """Publish changed effective flags, or refresh a long-running session hourly.

    Fresh means written less than an hour ago and not in the future: the
    file's mtime, and ``updated_at`` (by at most 5 minutes of clock skew).
    ``updated_at`` has one fixed-width form, so it is compared as text
    with the window's ends rather than parsed."""
    path = features_path(env, session_id)
    wanted = features_payload(env)
    try:
        actual = json.loads(session.read_bounded(session.fs_path(path), FEATURES_FILE_MAX_BYTES))
        now = time.time()
        if (isinstance(actual, dict) and set(actual) == set(wanted)
                and actual["features"] == wanted["features"] and actual["plugin_version"] == wanted["plugin_version"]
                and UPDATED_AT_RE.match(actual["updated_at"])
                and _timestamp(now - 3600) < actual["updated_at"] <= _timestamp(now + 300)
                and 0 <= now - os.stat(session.fs_path(path)).st_mtime < 3600):
            return
    except (OSError, ValueError, KeyError, TypeError):
        pass
    write_features_file(env, session_id=session_id)
