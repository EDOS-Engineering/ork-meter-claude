"""The CI layout of #140 holds: what triggers ci, what ci-gate
waits for, which native-host combinations each event runs, and where the
slow pure_bpe sweep runs. The WSL job and the arm64 unit leg (#30) are
pinned too, and docs/platform-matrix.md must name every CI row.

The workflows are read as text with an indentation walk (no YAML library is
installed in CI), so this checks the shapes these files use, not YAML at
large."""
import itertools
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT  # noqa: E402
WORKFLOWS = ROOT / ".github/workflows"


def read(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def block(text, key, indent=0):
    """The lines under ``key:`` at ``indent``, up to the next key at that indent."""
    lines = text.splitlines()
    head = re.compile(r"^%s%s:" % (" " * indent, re.escape(key)))
    for i, line in enumerate(lines):
        if head.match(line):
            body = []
            for following in lines[i + 1:]:
                stripped = following.lstrip(" ")
                if stripped and not stripped.startswith("#") and len(following) - len(stripped) <= indent:
                    break
                body.append(following)
            return "\n".join(body)
    raise AssertionError("no %r at indent %d" % (key, indent))


def keys(text, indent):
    return re.findall(r"^%s([A-Za-z0-9_-]+):" % (" " * indent), text, re.M)


def jobs(text):
    section = block(text, "jobs")
    return {name: block(section, name, 2) for name in keys(section, 2)}


def evaluate(expression, event, reuse=""):
    """The one expression shape the native-host excludes use:
    ``<variable> == '<v>' && '<a>' || '<b>'``, where the variable is
    github.event_name or the changes job's reuse output (#289)."""
    match = re.fullmatch(r"\$\{\{ (github\.event_name|needs\.changes\.outputs\.reuse) == '(\w+)' "
                         r"&& '([\w.-]+)' \|\| '([\w.-]+)' \}\}", expression)
    if not match:
        raise AssertionError("unexpected exclude expression: " + expression)
    value = event if match.group(1) == "github.event_name" else reuse
    return match.group(3) if value == match.group(2) else match.group(4)


def flow_list(text, key):
    match = re.search(r"^\s*%s: \[(.*)\]$" % re.escape(key), text, re.M)
    if not match:
        raise AssertionError("no %s: [...] list" % key)
    return [item.strip().strip('"') for item in match.group(1).split(",")]


def native_host_legs(event, reuse=""):
    """The native-host legs for ``event``; ``reuse`` is the changes job's output."""
    matrix = block(jobs(read("ci.yml"))["native-host"], "matrix", 6)
    legs = set(itertools.product(flow_list(matrix, "os"), flow_list(matrix, "claude")))
    exclude = block(matrix, "exclude", 8)
    entries = re.findall(r"^          - os: (.+)\n(?:            claude: \"?([\w.]+)\"?\n)?", exclude + "\n", re.M)
    if len(entries) != exclude.count("- "):
        raise AssertionError("unexpected exclude entry in:\n" + exclude)
    for os_expr, claude in entries:
        os_expr = os_expr.strip()
        os_name = evaluate(os_expr, event, reuse) if os_expr.startswith("${{") else os_expr
        # An entry without claude is a partial match: it excludes every claude.
        legs -= {leg for leg in legs if leg[0] == os_name and claude in ("", leg[1])}
    return legs


class CiWorkflows(unittest.TestCase):
    def test_no_push_trigger(self):
        """Main changes only through the merge queue, which already ran them."""
        for name in ("ci.yml",):
            triggers = keys(block(read(name), "on"), 2)
            with self.subTest(name=name):
                self.assertNotIn("push", triggers)
                self.assertIn("pull_request", triggers)
                self.assertIn("merge_group", triggers)
                self.assertIn("workflow_dispatch", triggers)
        self.assertIn("schedule", keys(block(read("ci.yml"), "on"), 2), "ci runs nightly on main")

    def test_ci_gate_needs_and_checks_every_job(self):
        all_jobs = jobs(read("ci.yml"))
        gate = all_jobs.pop("ci-gate")
        self.assertEqual(re.search(r"^    name: (.+)$", gate, re.M).group(1), "ci-gate", "the ruleset requires this name")
        self.assertRegex(gate, r"(?m)^    if: always\(\)$")
        self.assertEqual(sorted(flow_list(gate, "needs")), sorted(all_jobs))
        checked = set(re.findall(r"needs\.([\w-]+)\.result", gate))
        self.assertEqual(checked, set(all_jobs), "every needed job's result reaches the script")
        self.assertIn('[ "$result" = "success" ] || exit 1', gate)


    def test_native_host_covers_every_combination_before_merge(self):
        full = {(os_name, claude) for os_name in ("ubuntu-latest", "macos-latest", "windows-latest")
                for claude in ("2.1.280", "latest")}
        for event in ("merge_group", "schedule", "workflow_dispatch"):
            with self.subTest(event=event):
                self.assertEqual(native_host_legs(event), full)
        self.assertEqual(native_host_legs("pull_request"), full - {("macos-latest", "2.1.280")})

    def test_reuse_runs_only_the_native_host_leg_pull_requests_skip(self):
        """#289: a merge group reusing its PR's run still qualifies every
        combination before merge; the PR run passed all legs but this one."""
        self.assertEqual(native_host_legs("merge_group", "true"),
                         native_host_legs("merge_group") - native_host_legs("pull_request"))
        self.assertEqual(native_host_legs("merge_group", "true"), {("macos-latest", "2.1.280")})
        for reuse in ("", "false"):
            self.assertEqual(native_host_legs("merge_group", reuse), native_host_legs("merge_group"))

    def test_unit_runs_once_per_os(self):
        matrix = block(jobs(read("ci.yml"))["unit"], "matrix", 6)
        self.assertEqual(sorted(flow_list(matrix, "os")),
                         ["macos-latest", "ubuntu-24.04-arm", "ubuntu-latest", "windows-latest"])
        self.assertNotIn("claude", matrix)

    def test_platform_matrix_names_every_ci_row(self):
        """#30: docs/platform-matrix.md cites every CI job that verifies a row, by the
        name ci.yml gives it, and states the floor native-host tests; the README and
        docs/release.md link it."""
        matrix = (ROOT / "docs/platform-matrix.md").read_text(encoding="utf-8")
        ci = jobs(read("ci.yml"))
        names = {"unit (%s)" % os_name for os_name in flow_list(block(ci["unit"], "matrix", 6), "os")}
        names |= {"native-host (%s, claude %s)" % leg for leg in native_host_legs("merge_group")}
        names.add(re.search(r"^    name: (.+)$", ci["wsl"], re.M).group(1))
        names.add(re.search(r"^    name: (.+)$", ci["connected-failure"], re.M).group(1))
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertIn("`%s`" % name, matrix)
        floor = flow_list(block(ci["native-host"], "matrix", 6), "claude")[0]
        self.assertRegex(floor, r"^\d+\.\d+\.\d+$", "the first claude entry is the floor")
        self.assertIn("**Minimum: Claude Code %s.**" % floor, matrix)
        self.assertIn("**Out of scope: the VS Code and Cursor extensions.**", matrix)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Requires Claude Code %s or later" % floor, readme)
        self.assertIn("(docs/platform-matrix.md)", readme)
        release = (ROOT / "docs/release.md").read_text(encoding="utf-8")
        self.assertIn("(platform-matrix.md)", release)
        self.assertIn("(platform-walkthrough.md)", release)
        self.assertTrue((ROOT / "docs/platform-walkthrough.md").is_file())

    def slow_sweep_settings(self, *names):
        found = []
        for name in names:
            for job, text in jobs(read(name)).items():
                found += [(name, job, value.strip()) for value in re.findall(r"ORK_SLOW_TESTS: (.+)", text)]
        return found

    def test_slow_sweep_runs_in_exactly_one_ci_job(self):
        sweep = (ROOT / "tests/pure_bpe_regressions.py").read_text(encoding="utf-8")
        self.assertNotIn('"CI"', sweep, "CI=true is set in every job; only ORK_SLOW_TESTS turns the sweep on")
        self.assertEqual(self.slow_sweep_settings("ci.yml"),
                         [("ci.yml", "unit", "${{ matrix.os == 'ubuntu-latest' && '1' || '0' }}")])

    def test_release_verify_jobs_keep_the_full_sweep(self):
        """Releases are rare, so their verify jobs run the full sweep."""
        self.assertEqual(self.slow_sweep_settings("package-runtime.yml", "publish-release.yml"),
                         [("package-runtime.yml", "verify", '"1"'), ("publish-release.yml", "verify", '"1"')])

    def test_install_retries_and_caches(self):
        action = (ROOT / ".github/actions/install-claude/action.yml").read_text(encoding="utf-8")
        self.assertIn("for attempt in 1 2 3", action)
        self.assertIn("claude-code-${{ steps.resolve.outputs.version }}", action)
        ci = read("ci.yml")
        self.assertNotIn("npm install", ci, "every install goes through the retrying action")
        self.assertEqual(ci.count("uses: ./.github/actions/install-claude"), 2)
        # The wsl job installs inside WSL, where a composite action's bash is
        # Git Bash on the Windows host; its script carries the same retries.
        wsl = (ROOT / ".github/scripts/wsl-setup.sh").read_text(encoding="utf-8")
        self.assertEqual(wsl.count("for attempt in 1 2 3"), 2, "resolve and install both retry")
        self.assertIn('grep -qF "$version"', wsl, "the CLI must report the resolved version")

    def test_wsl_runs_unit_and_native_host_inside_ubuntu(self):
        """#30: WSL2 is a CI row, gated like unit, with both suites run in the distribution."""
        job = jobs(read("ci.yml"))["wsl"]
        self.assertIn("runs-on: windows-latest", job)
        self.assertIn("shell: wsl-bash {0}", job)
        self.assertRegex(job, r"uses: Vampire/setup-wsl@[0-9a-f]{40} # v\d")
        self.assertIn("distribution: Ubuntu-24.04", job)
        self.assertIn("wsl-shell-user: ork", job, "tests run unprivileged, as WSL users do")
        self.assertIn("bash .github/scripts/wsl-setup.sh", job)
        self.assertIn("bash tests/run.sh", job)
        self.assertIn("claude plugin validate --strict .claude-plugin/plugin.json", job)
        self.assertIn("RUN_NATIVE_CLAUDE_TESTS=1 python3 -I -B .github/scripts/check-native-host.py", job)
        self.assertNotIn("ORK_SLOW_TESTS", job)
        self.assertLessEqual(int(re.search(r"timeout-minutes: (\d+)", job).group(1)), 20)
        setup = (ROOT / ".github/scripts/wsl-setup.sh").read_text(encoding="utf-8")
        self.assertIn('git clone --quiet --no-hardlinks "$checkout" "$HOME/plugin"', setup,
                      "tests run in the distribution's filesystem, not under /mnt")
        self.assertIn("sha256sum -c", setup, "Node is checked against nodejs.org's SHASUMS256.txt")

if __name__ == "__main__":
    unittest.main(verbosity=2)
