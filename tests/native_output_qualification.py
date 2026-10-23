"""Native Bash/Grep/Glob output qualification; localhost inference, no billing."""
import json
import os
from pathlib import Path
import shlex
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import host_retry  # noqa: E402
from native_host import Fixture, Host, hook_evidence, latest_tool_results  # noqa: E402
sys.path.insert(0, str(ROOT / "hooks/lib"))
import session  # noqa: E402


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "opt in to installed CLI and localhost fixture")
@host_retry.retry_once
class NativeOutputQualification(unittest.TestCase):
    def test_deep_windows_style_storage_retains_native_circuit_observations(self):
        self.qualify("reduce", circuit_failure=True, deep_storage=True)

    def test_repeated_bash_reducer_failure_preserves_native_results_and_keeps_other_tools_active(self):
        self.qualify("reduce", circuit_failure=True)

    def test_native_baseline_reduction_and_shadow_capture(self):
        sizes = {}
        for mode in ("stock", "off", "reduce", "shadow"):
            with self.subTest(mode=mode):
                sizes[mode] = self.qualify(mode)
        if set(sizes) != {"stock", "off", "reduce", "shadow"}:
            return
        for tool in ("Bash", "Grep", "Glob"):
            self.assertLess(sizes["reduce"][tool], sizes["off"][tool], tool)
            self.assertEqual(sizes["shadow"][tool], sizes["off"][tool], tool)
            self.assertEqual(sizes["stock"][tool], sizes["off"][tool], tool)

    def qualify(self, mode, circuit_failure=False, deep_storage=False):
        scratch = Path("profile-" + "x" * 60) / ("workspace-" + "y" * 35) / "tmp" if deep_storage else "tmp"
        with Host(self, "ork-native-output-", "Bash,Grep,Glob", inference=Fixture, scratch=scratch) as host:
            plugin, work, scratch = host.plugin, host.work, host.scratch
            attempts = host.base / "reducer-attempts.txt"
            if circuit_failure:
                target = plugin / "hooks/trim-tool-output.py"
                original = target.read_text(encoding="utf-8")
                needle = "def handle_bash(ctx, response, budget, full):\n"
                self.assertIn(needle, original)
                injected = (needle + "    from pathlib import Path\n    marker = Path(%r)\n" % str(attempts)
                            + "    marker.write_text((marker.read_text() if marker.exists() else '') + 'x')\n"
                            + "    raise RuntimeError('controlled reducer failure')\n")
                target.write_text(original.replace(needle, injected), encoding="utf-8")
            script = work / "output.py"
            script.write_text('import sys\nfor i in range(450):\n    print("line_%04d " % i + "context " * 5)\n    if i == 220: print("ERROR: middle-first")\n    if i == 230: print("ERROR: middle-second")\nprint("fatal: separate stderr", file=sys.stderr)\n')
            noisy = work / "noisy.txt"
            # Circuit isolation needs observable rewriting by another reducer.
            # Keep that Grep over its 8 KiB budget but below native spilling;
            # the stock/off/reduce/shadow comparison still uses the 450-line
            # spilled result. Native previews must not masquerade as a global
            # circuit disable in the unrelated Bash-fault case.
            grep_lines = 250 if circuit_failure else 450
            noisy.write_text("".join("line_%04d " % i + "context " * 5 + "\n" for i in range(grep_lines)))
            listings = work / "listings"
            listings.mkdir()
            for i in range(150):
                (listings / ("finding_%04d_" % i + "x" * 75 + ".txt")).write_text("fixture")
            Fixture.calls = [
                ("Bash", {"command": shlex.quote(sys.executable.replace("\\", "/")) + " " + shlex.quote(str(script).replace("\\", "/"))}),
                ("Grep", {"pattern": "line_", "path": str(noisy), "output_mode": "content", "head_limit": 1000}),
                ("Glob", {"pattern": "*.txt", "path": str(listings)})]
            if circuit_failure:
                Fixture.calls = [Fixture.calls[0]] * 3 + Fixture.calls[1:]
            Fixture.requests = []
            Fixture.arrivals = []
            completed = host.run("Inspect the fixture outputs.", env=host.env(mode), plugin=mode != "stock")
            host.assert_no_external_traffic()
            self.assertEqual(len(Fixture.requests), len(Fixture.calls) + 1)
            sizes = {}
            for i, (tool, _) in enumerate(Fixture.calls):
                results = latest_tool_results(Fixture.requests[i + 1])
                self.assertEqual(len(results), 1, completed.stdout[-4000:] + completed.stderr[-4000:])
                output = json.dumps(results[0]["content"])
                sizes[tool] = len(output)
                self.assertFalse(results[0].get("is_error", False), output[:500])
                if tool == "Bash":
                    for evidence in ("ERROR: middle-first", "ERROR: middle-second", "fatal: separate stderr"):
                        self.assertIn(evidence, output)
                if mode == "reduce" and not (circuit_failure and tool == "Bash"):
                    self.assertIn("Full output:", output, tool + "; " + hook_evidence(completed.stdout, Fixture.arrivals))
                else:
                    self.assertNotIn("Full output:", output, tool)
            if circuit_failure:
                sys.path.insert(0, str(ROOT / "hooks/lib"))
                import circuit
                pins = list(Path(session.fs_path(scratch)).rglob(session.POLICY_NAME))
                self.assertEqual(len(pins), 1)
                health = circuit.state(session.Store(str(pins[0].parent)))
                self.assertEqual(attempts.read_text(), "xx", "third call must skip the disabled Bash reducer")
                self.assertEqual([(entry["scope"], entry["status"], entry["faults"]) for entry in health["entries"]],
                                 [("trim-tool-output:Bash", "disabled", 2)])
                self.assertNotIn("trim", health["disabled_features"], "other trim paths remain available")
                if deep_storage:
                    faults = list(pins[0].parent.rglob("reducer-health/*/" + "?" * 64 + ".json"))
                    self.assertEqual(len(faults), 2)
                    self.assertTrue(all(len(session.display_path(str(path))) > 300 for path in faults))
            if mode == "shadow":
                traces = [json.loads(path.read_text()) for path in scratch.rglob("shadow-traces/*.json")]
                post = [row for row in traces if row["event"] == "PostToolUse"]
                self.assertEqual({row["tool_name"] for row in post}, {"Bash", "Grep", "Glob"})
                for row in post:
                    if row["tool_name"] == "Grep":
                        response = row["tool_response"]
                        print("Grep native response metadata:", json.dumps({key: value for key, value in response.items()
                              if key not in ("content", "filenames")}), "content bytes:", len(response.get("content", "").encode()))
            print("Model-visible result bytes (%s): %s" % (mode, json.dumps(sizes, sort_keys=True)))
            return sizes


if __name__ == "__main__":
    unittest.main()
