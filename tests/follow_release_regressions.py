#!/usr/bin/env python3
"""Every state of the automatic public release, and the sync's two rewrites."""
import ast
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402,F401  (scrubs ORK_METER_*)

follow = load(".github/scripts/follow_release.py")
prepare = load(".github/scripts/prepare_sync.py")



def pr(checks="green", in_queue=False, ejected=False):
    return {"id": "ID", "number": 7, "branch": "sync/1.0.0", "in_queue": in_queue, "ejected": ejected, "checks": checks}


def state(**changes):
    base = {"version": "1.0.0", "released": False, "tag": False, "main_version": "0.9.0", "prs": {}, "runs": {}}
    base.update(changes)
    return base


class Decide(unittest.TestCase):
    def action(self, **changes):
        actions = follow.decide(state(**changes))
        self.assertEqual(len(actions), 1)
        return actions[0]

    def test_nothing_to_follow(self):
        self.assertEqual(follow.decide({"version": None})[0][0], "done")

    def test_released_is_done_and_changes_nothing(self):
        self.assertEqual(self.action(released=True, main_version="1.0.0")[0], "done")

    def test_released_with_a_marketplace_pull_request_asks_for_it(self):
        self.assertIn("queue the", self.action(released=True, prs={"channel": pr()})[1])

    def test_main_past_the_private_release_is_left_alone(self):
        self.assertEqual(self.action(main_version="1.1.0-dev")[0], "done")

    def test_a_new_private_release_starts_the_sync(self):
        self.assertEqual(self.action(), ("dispatch", "public-sync"))

    def test_a_running_sync_is_not_started_twice(self):
        self.assertEqual(self.action(runs={"public-sync": "active"})[0], "wait")

    def test_a_failed_step_notifies_and_is_never_restarted(self):
        for changes, workflow in (({}, "public-sync"), ({"main_version": "1.0.0-dev"}, "package-runtime"), ({"main_version": "1.0.0", "tag": True}, "publish-release")):
            action = self.action(runs={workflow: "failed"}, **changes)
            self.assertEqual(action[0], "notify")
            self.assertIn(workflow, action[1])

    def test_a_sync_that_succeeded_without_a_pull_request_notifies(self):
        self.assertEqual(self.action(runs={"public-sync": "success"})[0], "notify")

    def test_a_pull_request_with_no_checks_gets_them_started(self):
        self.assertEqual(self.action(prs={"sync": pr("none")}), ("start-ci", "sync/1.0.0"))

    def test_a_pull_request_being_checked_waits(self):
        self.assertEqual(self.action(prs={"sync": pr("pending")})[0], "wait")

    def test_a_green_pull_request_asks_a_person_to_queue_it(self):
        self.assertIn("queue the", self.action(prs={"sync": pr()})[1])
        self.assertEqual(self.action(prs={"sync": pr(in_queue=True)})[0], "wait")

    def test_a_red_or_ejected_pull_request_notifies_and_is_not_requeued(self):
        self.assertEqual(self.action(prs={"sync": pr("failed")})[0], "notify")
        self.assertEqual(self.action(prs={"sync": pr(ejected=True)})[0], "notify")

    def test_a_merged_sync_builds_the_release(self):
        self.assertEqual(self.action(main_version="1.0.0-dev"), ("dispatch", "package-runtime"))

    def test_an_open_release_pull_request_advances(self):
        self.assertIn("queue the", self.action(main_version="1.0.0-dev", prs={"release": pr()})[1])

    def test_a_merged_release_waits_for_a_person_to_sign(self):
        action = self.action(main_version="1.0.0")
        self.assertEqual(action[0], "notify")
        self.assertIn("sign the tag v1.0.0", action[1])

    def test_a_signed_tag_publishes(self):
        self.assertEqual(self.action(main_version="1.0.0", tag=True), ("dispatch", "publish-release"))


class Checks(unittest.TestCase):
    def test_no_check_runs_is_none(self):
        self.assertEqual(follow.checks_of([]), "none")
        self.assertEqual(follow.checks_of([{}]), "none")

    def test_a_started_workflow_without_its_gate_yet_is_pending(self):
        self.assertEqual(follow.checks_of([{"name": "changes", "status": "COMPLETED", "conclusion": "SUCCESS"}]), "pending")

    def test_any_failed_job_is_failed(self):
        self.assertEqual(follow.checks_of([{"name": "unit", "status": "COMPLETED", "conclusion": "FAILURE"}]), "failed")

    def test_a_green_gate_is_green(self):
        self.assertEqual(follow.checks_of([{"name": "ci-gate", "status": "COMPLETED", "conclusion": "SUCCESS"}]), "green")


class OpenPullRequest(unittest.TestCase):
    def read(self, removed):
        answer = {"data": {"repository": {"pullRequests": {"nodes": [{
            "id": "ID", "number": 7, "headRefName": "sync/1.0.0", "isInMergeQueue": False,
            "timelineItems": {"totalCount": 9, "nodes": removed},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
        }]}}}}
        original = follow.gh
        follow.gh = lambda *args, **kw: json.dumps(answer)
        self.addCleanup(setattr, follow, "gh", original)
        return follow.open_pr("o/n", "sync/1.0.0")

    def test_the_timeline_total_does_not_count_as_an_ejection(self):
        # GitHub's totalCount ignores itemTypes, so only the nodes are evidence.
        self.assertFalse(self.read([])["ejected"])
        self.assertTrue(self.read([{"__typename": "RemovedFromMergeQueueEvent"}])["ejected"])

    def test_no_rollup_means_no_checks(self):
        self.assertEqual(self.read([])["checks"], "none")


class Prepare(unittest.TestCase):
    PUBLIC = 'RUNTIME_CANDIDATES = {\n    "0.9.0": ("a", {"linux-amd64": "1"}),\n}\n_PLUGIN_VERSION = json.loads(x)\n'
    PRIVATE = ('RUNTIME_CANDIDATES = {\n    "0.9.0": ("a", {"linux-amd64": "1"}),\n}\n'
               'RUNTIME_CANDIDATES["1.0.0-dev"] = RUNTIME_CANDIDATES["1.0.0"] = ("b", {"linux-amd64": "2"})\n')

    def pins(self, source):
        return prepare.candidates(source)

    def test_the_release_takes_the_private_pin_and_dev_takes_the_one_on_main(self):
        out = prepare.add_pins(self.PUBLIC, self.PRIVATE, "1.0.0", "0.9.0")
        ast.parse(out)
        pins = self.pins(out)
        self.assertEqual(pins["1.0.0"], ("b", {"linux-amd64": "2"}))
        self.assertEqual(pins["1.0.0-dev"], ("a", {"linux-amd64": "1"}))
        self.assertLess(out.index('RUNTIME_CANDIDATES["1.0.0"]'), out.index(prepare.ANCHOR))

    def test_an_aliased_private_pin_is_followed(self):
        private = self.PRIVATE.replace('("b", {"linux-amd64": "2"})', 'RUNTIME_CANDIDATES["0.9.0"]')
        self.assertEqual(self.pins(prepare.add_pins(self.PUBLIC, private, "1.0.0", "0.9.0"))["1.0.0"][0], "a")

    def test_a_missing_pin_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no runtime pin for 2.0.0"):
            prepare.add_pins(self.PUBLIC, self.PRIVATE, "2.0.0", "0.9.0")
        with self.assertRaisesRegex(ValueError, "no runtime pin for 0.8.0"):
            prepare.add_pins(self.PUBLIC, self.PRIVATE, "1.0.0", "0.8.0")

    def test_the_real_pin_file_parses(self):
        source = (ROOT / prepare.PINS).read_text(encoding="utf-8")
        self.assertTrue(self.pins(source))
        self.assertEqual(source.count(prepare.ANCHOR), 1)

    def test_the_manifest_becomes_dev_and_keeps_its_formatting(self):
        self.assertEqual(prepare.set_dev_version('{\n  "version": "1.0.0",\n  "x": 1\n}\n', "1.0.0"), '{\n  "version": "1.0.0-dev",\n  "x": 1\n}\n')
        with self.assertRaises(ValueError):
            prepare.set_dev_version('{"version": "0.9.0"}', "1.0.0")


if __name__ == "__main__":
    unittest.main()
