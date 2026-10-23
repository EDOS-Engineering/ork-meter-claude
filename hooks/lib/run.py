"""Entry point for hooks/py.sh: ``python3 -I [...] hooks/lib/run.py <hook script>``.

Puts hooks/lib on ``sys.path``, loads the hook script by path (so the
``py.sh <name>.py`` names in hooks/hooks.json stay valid) and hands its
``EVENTS``, ``handle`` and optional ``FEATURE``, ``BUDGET``, ``REDUCER`` and ``wanted`` to
``hook.run``. Whatever happens, it prints nothing of its own and exits 0.

When a hook's lever is certainly off from the environment alone
(``skips``), run.py ends before it imports the hook library, about half of
the process's time.
"""

import time

STARTED = time.monotonic()   # before the imports below: hook.EMIT_LIMIT counts from here
STARTED_WALL = time.time()   # only to age hooks/py.sh's launch time (hook.launch_age)

import os  # noqa: E402

# hooks/py.sh's launch time (hook.LAUNCH_ENV), taken out of the environment
# at once: no handler, ctx.env or child process ever sees it.
LAUNCHED_AT = os.environ.pop("ORK_HOOK_LAUNCHED_AT", None)

import importlib.util  # noqa: E402
import sys  # noqa: E402

# Hook script name -> the variable of the opt-in lever without which it does
# nothing (features.OPT_INS; tests/stale-context.test.sh checks they agree).
# Unset, such a lever is off, so anything but an on spelling is certainly off.
OFF_UNLESS_ON = {"stale-context": "ORK_METER_STALE_GUARD"}
# Hook script name -> the variable of an opt-in lever with a per-token default
# (features.PER_TOKEN_DEFAULTS): unset, it may be on (billing is decided
# when the session is pinned), so only an explicit off spelling is certainly
# off (tests/context-nudge.test.sh checks).
OFF_WHEN_OFF = {"context-nudge": "ORK_METER_CONTEXT_HYGIENE"}
ON_VALUES = ("on", "1", "true", "yes")   # features.ON_VALUES
OFF_VALUES = ("off", "0", "false", "no")   # features.OFF_VALUES
MODE_ENV = "ORK_METER_MODE"   # features.MODE_ENV
MODES = ("reduce", "shadow")   # features.mode: any other value reads as off


def _spelling(variable, env):
    return str(env.get(variable, "")).strip().lower()


def certainly_off(variable, env):
    """True when the opt-in ``variable`` isn't on in ``env``. Claude Code
    merges the user's and managed settings' ``env`` into the hook's
    environment, and hooks/lib/project_env.py only ever turns a value that
    is on into something else, never the reverse, so a lever that isn't on
    here can't be on for the hook. Only for opt-ins without a per-token
    default (features.PER_TOKEN_DEFAULTS), which are off when unset."""
    return _spelling(variable, env) not in ON_VALUES


def explicitly_off(variable, env):
    """True when ``variable`` is set to an off spelling in ``env``. That holds
    for any opt-in, including one with a per-token default: project_env
    keeps an off value (it only replaces values that turn something on),
    a new pin records it as set (features.with_billing_defaults leaves an
    explicit setting alone), and an off spelling overrides an existing pin
    at once (features.apply_policy_settings)."""
    return _spelling(variable, env) in OFF_VALUES


def mode_off(env):
    """True when ``ORK_METER_MODE`` reads as off in ``env`` (features.mode).
    project_env keeps a mode that reads off, and features.apply_policy_settings
    gives "off" whenever the live mode differs from the pinned one, so the
    effective mode is off too and no automatic lever runs. Shadow is not
    certain: a project's ``shadow`` is replaced by the user's mode."""
    return str(env.get(MODE_ENV, "reduce")).strip().lower() not in MODES


def skips(name, env):
    """True when hook script ``name`` (no ``.py``) would certainly do nothing
    with ``env``. Only for the UserPromptSubmit hooks above: their levers are
    automatic (off unless the mode is reduce) and that event has no shadow
    capture (hooks/lib/trace.py's EVENTS)."""
    if name in OFF_UNLESS_ON:
        return mode_off(env) or certainly_off(OFF_UNLESS_ON[name], env)
    if name in OFF_WHEN_OFF:
        return mode_off(env) or explicitly_off(OFF_WHEN_OFF[name], env)
    return False


def main(argv):
    script = argv[1]
    try:
        off = skips(os.path.splitext(os.path.basename(script))[0], os.environ)
    except Exception:
        off = False
    if off:
        sys.stdin.buffer.read()   # take the payload, as a hook that ran would
        return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hook  # noqa: E402

    name = os.path.splitext(os.path.basename(script))[0].replace("-", "_")
    spec = importlib.util.spec_from_file_location("ork_meter_hook_" + name, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hook.run(module.EVENTS, module.handle,
             feature=getattr(module, "FEATURE", None),
             budget=getattr(module, "BUDGET", hook.DEFAULT_BUDGET), reducer=getattr(module, "REDUCER", None),
             wanted=getattr(module, "wanted", None), started=STARTED, every_mode=getattr(module, "EVERY_MODE", False),
             launch_age=hook.launch_age(LAUNCHED_AT, STARTED_WALL))


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception:
        pass
    sys.exit(0)
