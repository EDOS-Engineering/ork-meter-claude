"""Installed-host cold runtime install (#140).

Every other suite that runs the shipped runtime installs it first, untimed
(``Host.warm``), so its SessionStart is warm. This one keeps the cold path
visible: on a fresh CLAUDE_PLUGIN_DATA, hooks/session-start.sh as Claude Code
runs it, timed, then the same hook warm.

A user meets the cold path on the first session after an install or an
update (each runtime SHA gets its own directory). Installing means copying
the 12 MB runtime into the data dir, hashing it and running it for the first
time; on a machine whose antivirus scans a freshly written, unsigned binary
(Windows Defender) that first exec can take seconds. So hooks/session-start.sh
does not install: it starts scripts/runtime.sh --install detached and returns
at once, silently (no "setting up" notice: the hook prints only its failure
warning). The cold hook must finish under
COLD_BOUND, inside the hook timeout on every platform; the install must then
complete on its own, after the hook (and, for a real session, after the CLI)
has exited; and the next session is warm.
"""
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import host_retry  # noqa: E402
from native_canary_qualification import (SESSION_START_BOUND, run_session_start, runtime_sha256,  # noqa: E402
                                         runtime_target, session_start_responses, session_start_timeout)
from native_host import Host, assert_hooks_fail_open, hook_names, wait_for  # noqa: E402

RUNTIME = "ork-meter-plugin-runtime" + (".exe" if os.name == "nt" else "")
# The cold hook starts the install and returns: inside the hook timeout with
# the same margin as Windows' warm bound, on every platform.
COLD_BOUND = session_start_timeout() - 0.5
INSTALL_WAIT = 90
SLOW_INSTALL = 15
# hooks/session-start.sh prints only this, when the runtime cannot run at all;
# a cold session (install started detached) and a warm one print nothing.
UNAVAILABLE = "Ork Meter: automatic capture is unavailable."
TIMINGS = {}


class NativeRuntimeInstall(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        if TIMINGS:
            print("\nRuntime install timings (seconds): " + json.dumps(TIMINGS, sort_keys=True), flush=True)

    def setUp(self):
        manifest, actual = runtime_sha256()
        self.assertEqual(manifest, actual, "runtime/%s/SHA256 does not match the shipped binary" % runtime_target())

    def installed(self, host):
        return list((host.plugin_data / "runtime").glob("*/" + RUNTIME))

    def install_finished(self, host):
        """Binary and stamp in place, lock released, no staging file left."""
        runtime = host.plugin_data / "runtime"
        return (bool(self.installed(host)) and bool(list(runtime.glob("*/.verified")))
                and not (runtime / ".install-lock").exists() and not list(runtime.glob("*/.runtime-*")))

    def wait_installed(self, host):
        started = time.monotonic()
        self.assertTrue(wait_for(lambda: self.install_finished(host), timeout=INSTALL_WAIT, step=0.1),
                        "the background install did not finish: %s" % sorted(
                            str(path.relative_to(host.plugin_data)) for path in host.plugin_data.rglob("*")))
        return time.monotonic() - started

    def test_cold_session_start_installs_then_runs_warm(self):
        """The one timed SessionStart on a fresh data dir, then a warm one."""
        with Host(self, "ork-native-cold-", "Bash", runtime=True) as host:
            host.assert_credential_store_isolated()
            self.assertEqual(self.installed(host), [], "the data dir starts empty")
            cold, message = run_session_start(self, host, bound=COLD_BOUND)
            # Silent (hooks/session-start.sh: a 75 from --if-installed starts the
            # detached install and prints nothing); the install is the evidence.
            self.assertEqual(message, "", "a cold SessionStart prints nothing")
            background = self.wait_installed(host)
            self.assertTrue(self.installed(host), "scripts/runtime.sh installed the shipped runtime")
            warm, message = run_session_start(self, host, bound=SESSION_START_BOUND)
            # The installed runtime's own notice (its /ork-meter:connect hint)
            # goes to /dev/null; the warm hook is silent too, not the warning.
            self.assertEqual(message, "", "a warm SessionStart prints nothing")
            TIMINGS["session_start"] = {"cold": round(cold, 2), "warm": round(warm, 2), "cold_bound": COLD_BOUND,
                                        "background_install_after_hook": round(background, 2),
                                        "hook_timeout": session_start_timeout()}
            print("\n  cold SessionStart %.2f s (bound %.1f s), install done %.2f s later, warm %.2f s"
                  % (cold, COLD_BOUND, background, warm), flush=True)

    def test_cold_claude_session_is_not_cancelled(self):
        """A real first session: no hook is cancelled, and the install the
        hook started finishes after the CLI has exited."""
        with Host(self, "ork-native-cold-cli-", "Bash", runtime=True) as host:
            host.assert_credential_store_isolated()
            # Cold runtime only: the hooks' bytecode is compiled first, as
            # it is for any session after the plugin's first.
            host.warm_hooks()
            # An install slower than the whole session, so it must outlive
            # the CLI: this host's copy of runtime.sh (not the repository's)
            # sleeps first in --install mode. A PATH shim would not do: Git
            # Bash's launcher puts /usr/bin ahead of it on Windows.
            script = host.plugin / "scripts/runtime.sh"
            text = script.read_text(encoding="utf-8")
            anchor = '[ "$mode" != install ] || exec >/dev/null 2>&1\n'
            self.assertIn(anchor, text)
            script.write_text(text.replace(anchor, anchor + '[ "$mode" != install ] || sleep %d\n' % SLOW_INSTALL),
                              encoding="utf-8", newline="\n")
            started = time.monotonic()
            completed = host.run("Say the word.")
            elapsed = time.monotonic() - started
            pending = not self.install_finished(host)
            assert_hooks_fail_open(self, completed.stdout)
            # The cold SessionStart ran and printed nothing: no "setting up"
            # notice any more, and not the failure warning either.
            self.assertIn("SessionStart:startup", hook_names(completed.stdout))
            self.assertEqual(session_start_responses(completed.stdout), [],
                             "a cold session's SessionStart must be silent")
            self.assertNotIn(UNAVAILABLE, completed.stdout)
            self.assertIn("QUALIFIED", completed.stdout)
            self.assertLess(elapsed, SLOW_INSTALL, "the session did not wait for the install")
            self.assertTrue(pending, "the install was still running when the CLI exited")
            background = self.wait_installed(host)
            host.assert_no_external_traffic()
            TIMINGS["claude_cold_session"] = {"print": round(elapsed, 2), "install_delay": SLOW_INSTALL,
                                              "install_done_after_exit": round(background, 2)}


if __name__ == "__main__":
    unittest.main()
