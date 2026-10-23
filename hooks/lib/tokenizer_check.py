"""Session-start check that the offline token gate can count tokens.

Run by hooks/python-check.sh with the hooks' own interpreter:
``<hook python> -I -B hooks/lib/tokenizer_check.py``. Below the plugin's
3.10 floor it prints that interpreter's version and exits ``FLOOR_STATUS``
before importing anything else, and the shell says so; this file must
therefore still parse on older Python 3.

Every replacement a trim lever proposes must pass ``token_gate.permits``,
which counts tokens exactly with the pinned ``tiktoken`` (requirements-hooks.txt, hash-locked)
in that interpreter or, without it, with the built-in pure-Python encoder
(hooks/lib/pure_bpe.py). The two give the same counts, so a missing tiktoken
alone costs nothing but some speed and is not reported. Only when neither
loads does the gate fall back to a conservative byte-count gate: still safe,
but it admits only very large savings, so most trims are left out. This says
so, once per startup or resume, with the command that restores full savings.

Silent when an exact encoder works, and when the account has no Ork Meter Plus
(hooks/lib/entitlement.py): every lever is off then anyway and the session
start already says why. Prints at most one JSON ``systemMessage``, never
installs anything and always exits 0.
"""

import sys

FLOOR_STATUS = 3
if __name__ == "__main__" and sys.version_info < (3, 10):
    print("%d.%d.%d" % tuple(sys.version_info[:3]))
    sys.exit(FLOOR_STATUS)

import json  # noqa: E402
import os  # noqa: E402
import string  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# requirements-hooks.txt locks every package with its hashes (#265): pip
# installs only those files, and only wheels, so no package code runs to build.
LOCKED_INSTALL = "--require-hashes --only-binary=:all:"


def problem():
    """Why the token gate can't count exactly, or None when it can."""
    import importlib.metadata
    import token_gate
    if token_gate.available():  # tiktoken, or the pure-Python encoder
        return None
    try:
        version = importlib.metadata.version("tiktoken")
    except importlib.metadata.PackageNotFoundError:
        return "has no tiktoken"
    except Exception:
        version = None
    if version is not None and version != token_gate.VERSION:
        return "has tiktoken %s, not %s" % (version, token_gate.VERSION)
    try:
        import regex  # noqa: F401 (token_gate.count needs it)
    except ImportError:
        return "has no regex"
    return "cannot load the offline tokenizer"


def private_python(data, platform=sys.platform):
    venv = os.path.join(data, "hook-python")
    if platform == "win32":
        return venv, os.path.join(venv, "Scripts", "python.exe")
    return venv, os.path.join(venv, "bin", "python3")


def is_absolute(data, platform=sys.platform):
    """The paths hooks/hook-python.sh accepts: /..., and on Windows only
    C:\\... or C:/... (elsewhere a drive-like name is relative)."""
    windows = platform.startswith(("win", "cygwin", "msys"))
    return data.startswith("/") or (windows and len(data) > 2 and data[0] in string.ascii_letters
                                    and data[1] == ":" and data[2] in "\\/")


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def message(reason, executable, env, platform=sys.platform, prefix=sys.prefix):
    requirements = os.path.join(ROOT, "requirements-hooks.txt")
    data = env.get("CLAUDE_PLUGIN_DATA") or ""
    if is_absolute(data, platform):
        venv, python = private_python(data, platform)
        install = '"%s" -m pip install %s -r "%s"' % (python, LOCKED_INSTALL, requirements)
        if not same_path(prefix, venv):
            install = '"%s" -m venv --clear "%s", then %s' % (executable, venv, install)
    else:
        install = ('"%s" -m pip install %s -r "%s" (not --user: the hooks run Python with -I, '
                   "which ignores user site-packages)" % (executable, LOCKED_INSTALL, requirements))
    version = "%d.%d.%d" % tuple(sys.version_info[:3])
    return ("Ork Meter: tokenizer missing: the hooks' Python (%s, %s) %s, so trimming uses a "
            "conservative byte-count gate that admits only very large savings. For full savings, "
            "install the tokenizer: run %s, then restart Claude Code."
            % (executable, version, reason, install))


def main(env=None, out=sys.stdout):
    env = os.environ if env is None else env
    sys.path.insert(0, HERE)
    import entitlement
    if entitlement.status(env) != entitlement.ENTITLED:
        return
    import features
    # Silent when the person has turned every replacing lever off: the missing
    # tokenizer then costs them nothing.
    if features.mode(env) == "off" or not any(features.enabled(env, lever) for lever in features.REPLACING_FEATURES):
        return
    reason = problem()
    if reason:
        out.write(json.dumps({"systemMessage": message(reason, sys.executable or "python3", env)}) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
