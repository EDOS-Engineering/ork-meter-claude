#!/usr/bin/env python3
"""The public release is driven by events, and nothing polls for it.

deliver-release.py in the private repository starts public-sync; the sync
merge starts package-runtime; the signed tag's push starts publish-release,
which starts ork-meter's plugin-releases. Every pull request in that chain is
opened with a GitHub App token, because GitHub starts no workflow for events
made with GITHUB_TOKEN. The new step scripts run here by bash against a fake
`gh`, not against GitHub."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402  (scrubs ORK_METER_*)

WORKFLOWS = ROOT / ".github/workflows"
APP_TOKEN = "actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349 # v2.2.2"


def read(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def on_block(text):
    return text[text.index("\non:\n"):text.index("\njobs:\n")]


def job(text, name):
    match = re.search(r"^  %s:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)" % re.escape(name), text, re.M | re.S)
    assert match, name
    return match.group(1)


def step(text, name):
    """The text of step ``name``, up to the next step or job."""
    start = text.index(f"- name: {name}\n")
    following = re.search(r"^ {6}- (name|uses):|^  [a-z][a-z0-9-]*:\n", text[start + 1:], re.M)
    return text[start:start + 1 + following.start()] if following else text[start:]


def script(text, name):
    """The dedented `run: |` script of step ``name``."""
    body = step(text, name)
    lines = body[body.index("run: |\n") + len("run: |\n"):].splitlines()
    run = textwrap.dedent("\n".join(lines)) + "\n"
    assert "${{" not in run, name  # values reach scripts only through env
    return run


# A fake gh: each call is logged; the reply is the first rule whose words all
# appear in the call, as (exit status, stdout).
FAKE_GH = r'''import json, os, sys
call = " ".join(sys.argv[1:])
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
for words, status, out in json.load(open(os.environ["FAKE_GH_RULES"])):
    if all(word in call for word in words):
        sys.stdout.write(out)
        sys.exit(status)
sys.stderr.write("fake gh: no rule for " + call + "\n")
sys.exit(1)
'''


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class StepScripts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.bin = base / "bin"
        self.bin.mkdir()
        (base / "fake_gh.py").write_text(FAKE_GH)
        gh = self.bin / "gh"
        gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I -B "{base / "fake_gh.py"}" "$@"\n')
        gh.chmod(0o755)
        self.log = base / "gh.log"
        self.rules = base / "rules.json"
        self.output = base / "output"

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, text, rules, **env):
        self.log.write_text("")
        self.output.write_text("")
        self.rules.write_text(json.dumps(rules))
        full = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", FAKE_GH_LOG=str(self.log),
                    FAKE_GH_RULES=str(self.rules), GITHUB_OUTPUT=str(self.output), GITHUB_REPOSITORY="o/r",
                    GH_TOKEN="unused", **env)
        done = subprocess.run(["bash", "-c", text], env=full, capture_output=True, text=True, cwd=self.temp.name)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines() if line)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return outputs, calls, done.stdout

    # publish-release's start job, run on a tag push.
    def start(self, tag, published=False):
        rules = [[["releases/tags/"], 0 if published else 1, ""], [["workflow", "run"], 0, ""]]
        return self.run_script(script(read("publish-release.yml"), "Dispatch publish-release on main unless the Release is published"),
                               rules, TAG=tag, PUSHER="releaser")

    def test_a_release_tag_dispatches_the_publication_on_main(self):
        _, calls, _ = self.start("v1.2.3")
        self.assertEqual(calls[-1], ["workflow", "run", "publish-release.yml", "-R", "o/r", "--ref", "main",
                                     "-f", "plugin_version=1.2.3", "-f", "requested_by=releaser"])

    def test_a_published_release_or_another_tag_dispatches_nothing(self):
        for tag, published in (("v1.2.3", True), ("v1.2.3-rc.1", False), ("v01.2.3", False), ("nightly", False)):
            with self.subTest(tag=tag):
                _, calls, _ = self.start(tag, published)
                self.assertFalse([c for c in calls if c[:2] == ["workflow", "run"]])

    # package-runtime's resolve job.
    def resolve(self, event, before, after, tagged=False, proposed=False, ref="abc1234"):
        def manifest(version):
            return json.dumps({"name": "ork-meter", "version": version})
        rules = [
            [["contents/.claude-plugin/plugin.json?ref=BEFORE"], 0 if before else 1, manifest(before) if before else ""],
            [["contents/.claude-plugin/plugin.json?ref=AFTER"], 0, manifest(after)],
            [["matching-refs/tags/"], 0, "1" if tagged else "0"],
            [["matching-refs/heads/package-runtime/"], 0, "1" if proposed else "0"],
            [["release", "download", "-R", "EDOS-Engineering/claude-plugin"], 0, json.dumps({"ork_meter_ref": ref})],
        ]
        return self.run_script(script(read("package-runtime.yml"), "Resolve the version and the ork-meter ref"), rules,
                               EVENT=event, INPUT_VERSION="9.9.9" if event == "workflow_dispatch" else "",
                               INPUT_REF="v9" if event == "workflow_dispatch" else "",
                               BEFORE="BEFORE", AFTER="AFTER", PRIVATE_TOKEN="read-only")

    def test_the_sync_merge_packages_the_private_releases_runtime(self):
        outputs, calls, _ = self.resolve("push", "1.0.0", "1.1.0-dev")
        self.assertEqual(outputs, {"run": "true", "plugin_version": "1.1.0", "ork_meter_ref": "abc1234"})
        self.assertIn(["release", "download", "v1.1.0", "-R", "EDOS-Engineering/claude-plugin", "-p", "manifest.json", "-O", "-"], calls)

    def test_any_other_push_packages_nothing(self):
        for before, after, tagged, proposed in (("1.1.0-dev", "1.1.0-dev", False, False),   # an ordinary merge
                                                ("1.1.0-dev", "1.1.0", False, False),       # the release merge
                                                ("1.1.0", "1.1.0", False, False),           # the marketplace merge
                                                ("1.0.0", "1.1.0-dev", True, False),        # already tagged
                                                ("1.0.0", "1.1.0-dev", False, True)):       # already proposed
            with self.subTest(before=before, after=after, tagged=tagged, proposed=proposed):
                outputs, calls, _ = self.resolve("push", before, after, tagged, proposed)
                self.assertEqual(outputs, {"run": "false"})
                self.assertFalse([c for c in calls if c[:2] == ["release", "download"]])

    def test_a_dispatch_takes_its_inputs(self):
        outputs, calls, _ = self.resolve("workflow_dispatch", None, "unused")
        self.assertEqual(outputs, {"run": "true", "plugin_version": "9.9.9", "ork_meter_ref": "v9"})
        self.assertEqual(calls, [])

    # public-sync's guard.
    def sync(self, main, proposed=False):
        rules = [[["contents/.claude-plugin/plugin.json?ref=main"], 0, json.dumps({"version": main})],
                 [["matching-refs/heads/sync/"], 0, "1" if proposed else "0"]]
        outputs, _, _ = self.run_script(script(read("public-sync.yml"), "Stop if main has this version or a later one, or it is proposed"),
                                        rules, VERSION="1.1.0")
        return outputs["run"]

    def test_public_sync_proposes_only_a_newer_version_once(self):
        self.assertEqual(self.sync("1.0.0"), "true")
        self.assertEqual(self.sync("1.0.0", proposed=True), "false")
        # Merged already, released already, or a rollback delivery of an older release.
        for main in ("1.1.0-dev", "1.1.0", "1.2.0"):
            with self.subTest(main=main):
                self.assertEqual(self.sync(main), "false")


class Wiring(unittest.TestCase):
    def test_only_ci_runs_on_a_schedule(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            if path.name != "ci.yml":
                with self.subTest(workflow=path.name):
                    self.assertNotIn("schedule:", on_block(path.read_text(encoding="utf-8")))

    def test_the_follower_runs_only_by_hand(self):
        self.assertEqual(on_block(read("follow-private-release.yml")).split("\n")[2].split("#")[0].strip(), "workflow_dispatch:")

    def test_every_pull_request_is_opened_with_a_token_limited_to_this_repository(self):
        for workflow, opening in (("public-sync.yml", "Open the sync pull request"),
                                  ("package-runtime.yml", "Open the release pull request"),
                                  ("publish-release.yml", "Open the pull request")):
            text = read(workflow)
            with self.subTest(workflow=workflow):
                self.assertIn("GH_TOKEN: ${{ steps.app.outputs.token }}", step(text, opening))
                self.assertNotIn("github.token", step(text, opening))
                self.assertIn("gh pr create", step(text, opening))
                mint = text[text.index("id: app\n") - 200:text.index("id: app\n") + 600]
                self.assertIn(APP_TOKEN, mint)
                self.assertIn("repositories: ${{ github.event.repository.name }}", mint)
                self.assertIn("owner: ${{ github.repository_owner }}", mint)

    def test_public_sync_checks_out_with_the_app_token_and_may_push_workflows(self):
        text = read("public-sync.yml")
        self.assertIn("token: ${{ steps.app.outputs.token }}", step(text, "Check out main"))
        self.assertIn("permission-workflows: write", text)
        self.assertLess(text.index("id: needed"), text.index("id: app"))

    def test_package_runtime_pushes_the_release_branch_with_the_app_token_after_the_build(self):
        text = read("package-runtime.yml")
        self.assertIn("push:\n    branches: [main]\n  workflow_dispatch:", on_block(text))
        opening = step(text, "Open the release pull request")
        self.assertIn('git -c http.https://github.com/.extraheader= push "https://x-access-token:${GH_TOKEN}@github.com/', opening)
        self.assertLess(text.index("Smoke-test the linux-amd64 runtime"), text.index("id: app"))
        self.assertNotIn("permission-workflows", text)
        # The release commit's author is the one publish-release requires.
        commit = load(".github/scripts/check-release-commit.py")
        self.assertIn(f'git config user.name "{commit.BOT_NAME}"', opening)
        self.assertIn(f'git config user.email "{commit.BOT_EMAIL}"', opening)
        for later in ("verify", "package"):
            self.assertIn("needs.resolve.outputs.run == 'true'", job(text, later))
        self.assertIn("concurrency:", job(text, "package"))
        self.assertNotIn("\nconcurrency:", text)

    def test_the_tag_push_dispatches_the_gated_publication(self):
        text = read("publish-release.yml")
        self.assertIn('push:\n    tags: ["v*"]', on_block(text))
        self.assertIn("if: github.event_name == 'push'", job(text, "start"))
        self.assertNotIn("environment:", job(text, "start"))
        self.assertIn("actions: write", job(text, "start"))
        for gated in ("verify", "publish", "point-marketplace", "release-pages"):
            with self.subTest(job=gated):
                self.assertIn("if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", job(text, gated))
        self.assertIn("environment: production", job(text, "publish"))

    def test_publish_release_starts_the_site_release_sync_with_an_actions_only_token(self):
        pages = job(read("publish-release.yml"), "release-pages")
        self.assertIn("needs: publish", pages)
        self.assertIn("repositories: ork-meter\n", pages)
        self.assertEqual(re.findall(r"permission-[a-z-]+: \w+", pages), ["permission-actions: write"])
        self.assertIn('gh workflow run plugin-releases.yml -R "$GITHUB_REPOSITORY_OWNER/ork-meter" --ref main', pages)


if __name__ == "__main__":
    unittest.main()
