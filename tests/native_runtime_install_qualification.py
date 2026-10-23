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
does not install: it starts scripts/runtime.sh --install detached and answers
at once that the runtime is being set up. The cold hook must finish under
COLD_BOUND, inside the hook timeout on every platform; the install must then
complete on its own, after the hook (and, for a real session, after the CLI)
has exited; and the next session is warm.

On Windows, ``test_windows_cold_exec_timeline`` prints, without asserting
beyond sanity, a timestamp for each step of a cold install done by hand
(the package digest, the copy to a staging file, its digest, the move, the
first and second exec of the runtime) and whether Defender real-time
protection is on. On GitHub's Windows runners it is off, and the whole cold
install takes about 0.5-1 s; a user's machine with Defender on may differ.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import host_retry  # noqa: E402
from native_canary_qualification import (SESSION_START_BOUND, run_session_start, runtime_sha256,  # noqa: E402
                                         runtime_target, session_start_timeout)
from native_host import Host, assert_hooks_fail_open, bash, wait_for  # noqa: E402

RUNTIME = "ork-meter-plugin-runtime" + (".exe" if os.name == "nt" else "")
# The cold hook starts the install and returns: inside the hook timeout with
# the same margin as Windows' warm bound, on every platform.
COLD_BOUND = session_start_timeout() - 0.5
INSTALL_WAIT = 90
SLOW_INSTALL = 15
SETTING_UP = "Ork Meter: setting up its runtime in the background"
TIMINGS = {}

# One cold install by hand, as scripts/runtime.sh does it, with a timestamp
# ($EPOCHREALTIME, bash 5: Git Bash has it) after each step. Arguments: the
# package dir, the binary name, the data dir.
TIMELINE = r'''
set -u
mark() { printf '%s %s\n' "$1" "$EPOCHREALTIME"; }
mark bash_started
package=$1 name=$2 data=$3
expected=$(cat "$package/SHA256")
destination="$data/runtime/$expected"
mkdir -p "$destination"
mark manifest_read
actual=$(sha256sum < "$package/$name" | cut -d ' ' -f 1)
mark package_digest
staging=$(mktemp "$destination/.runtime-XXXXXX")
cp "$package/$name" "$staging"
chmod 700 "$staging"
mark copied_to_staging
staged=$(sha256sum < "$staging" | cut -d ' ' -f 1)
mark staging_digest
mv -f "$staging" "$destination/$name"
mark moved
"$destination/$name" session-start --data-dir "$data" > "$data/first.out" 2>/dev/null
printf 'first_exit %s\n' "$?"
mark first_exec
"$destination/$name" session-start --data-dir "$data" > "$data/second.out" 2>/dev/null
printf 'second_exit %s\n' "$?"
mark second_exec
printf 'digests_match %s\n' "$([ "$actual" = "$expected" ] && [ "$staged" = "$expected" ] && echo yes || echo no)"
'''

DEFENDER = ("$s = Get-MpComputerStatus -ErrorAction Stop; "
            "'{0} {1} {2}' -f $s.RealTimeProtectionEnabled, $s.OnAccessProtectionEnabled, $s.AMRunningMode")


def defender_status():
    """Defender real-time protection as a non-admin can read it, or why not."""
    try:
        completed = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", DEFENDER],
                                   capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return {"available": False, "error": repr(error)[:200]}
    fields = completed.stdout.split()
    if completed.returncode != 0 or len(fields) < 3:
        return {"available": False, "error": (completed.stderr or completed.stdout).strip()[:300]}
    return {"available": True, "real_time_protection": fields[0], "on_access_protection": fields[1],
            "running_mode": " ".join(fields[2:])}


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
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
            self.assertIn(SETTING_UP, message)
            background = self.wait_installed(host)
            self.assertTrue(self.installed(host), "scripts/runtime.sh installed the shipped runtime")
            warm, message = run_session_start(self, host, bound=SESSION_START_BOUND)
            self.assertIn("/ork-meter:connect", message)
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
            self.assertIn(SETTING_UP, completed.stdout)
            self.assertIn("QUALIFIED", completed.stdout)
            self.assertLess(elapsed, SLOW_INSTALL, "the session did not wait for the install")
            self.assertTrue(pending, "the install was still running when the CLI exited")
            background = self.wait_installed(host)
            host.assert_no_external_traffic()
            TIMINGS["claude_cold_session"] = {"print": round(elapsed, 2), "install_delay": SLOW_INSTALL,
                                              "install_done_after_exit": round(background, 2)}

    if os.name == "nt":
        def test_windows_cold_exec_timeline(self):
            """Diagnostic for #140: printed, asserted only for sanity."""
            with Host(self, "ork-native-timeline-", "Bash", runtime=True) as host:
                env = host.env()
                package = host.plugin / "runtime" / runtime_target()
                script = host.base / "timeline.sh"
                script.write_text(TIMELINE, encoding="utf-8", newline="\n")
                launched = time.time()
                completed = subprocess.run([bash(env), script.as_posix(), package.as_posix(), RUNTIME,
                                            host.plugin_data.as_posix()], cwd=host.work, env=env,
                                           capture_output=True, text=True, timeout=120)
                ended = time.time()
                self.assertEqual(completed.returncode, 0, completed.stderr[-800:])
                marks, facts = [], {}
                for line in completed.stdout.splitlines():
                    key, _, value = line.partition(" ")
                    if key in ("first_exit", "second_exit", "digests_match"):
                        facts[key] = value
                    else:
                        marks.append((key, float(value)))
                names = [key for key, _ in marks]
                self.assertEqual(names, ["bash_started", "manifest_read", "package_digest", "copied_to_staging",
                                         "staging_digest", "moved", "first_exec", "second_exec"], completed.stdout)
                times = [value for _, value in marks]
                self.assertEqual(times, sorted(times), "timestamps are in order")
                self.assertEqual(facts, {"first_exit": "0", "second_exit": "0", "digests_match": "yes"})
                for output in ("first.out", "second.out"):
                    lines = (host.plugin_data / output).read_text(encoding="utf-8").splitlines()
                    self.assertEqual(len(lines), 1)
                    self.assertIn("systemMessage", json.loads(lines[0]))
                steps = {"bash_start": round(times[0] - launched, 3)}
                for (_, earlier), (name, later) in zip(marks, marks[1:]):
                    steps[name] = round(later - earlier, 3)
                steps["bash_exit"] = round(ended - times[-1], 3)
                report = {"steps_seconds": steps, "total_seconds": round(ended - launched, 3),
                          "runtime_bytes": (package / RUNTIME).stat().st_size, "defender": defender_status()}
                TIMINGS["windows_cold_exec_timeline"] = report
                print("\n  WINDOWS COLD EXEC TIMELINE: " + json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    unittest.main()
