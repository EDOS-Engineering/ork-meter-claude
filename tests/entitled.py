"""Test fixture: an Ork Meter Plus entitlement, so lever tests run entitled.

Savings levers are off without Ork Meter Plus (hooks/lib/entitlement.py).
``install()`` points ``CLAUDE_PLUGIN_DATA`` at a private temporary directory
holding what the plugin runtime writes for a connected Plus account, and
returns that directory: a policy envelope signed with the contract's test
key (tests/plus_policy_sign.py), its ``connection.json`` and the legacy
``entitlement.json`` for older readers. tests/entitlement.test.sh and
tests/plus-policy.test.sh cover the gate itself.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plus_policy_sign  # noqa: E402


def write(directory, **kw):
    """``plus_policy_sign.install`` into ``directory`` (created 0700)."""
    return plus_policy_sign.install(directory, **kw)


def install(environ=None):
    environ = os.environ if environ is None else environ
    directory = tempfile.mkdtemp(prefix="ork-meter-plugin-data-")
    atexit.register(shutil.rmtree, directory, True)
    environ["CLAUDE_PLUGIN_DATA"] = write(directory)
    return directory


def claude_config(config):
    """Seed an entitlement where Claude Code puts this plugin's data for
    ``--plugin-dir`` runs (``$CLAUDE_CONFIG_DIR/plugins/data/<name>-inline``,
    observed with Claude Code 2.1.295) and return ``config`` as a string, for
    the native qualification tests' ``CLAUDE_CONFIG_DIR``."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as stream:
        name = json.load(stream)["name"]
    write(os.path.join(str(config), "plugins", "data", name + "-inline"))
    return str(config)
