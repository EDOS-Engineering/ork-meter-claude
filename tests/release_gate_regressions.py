"""Ensure missing or weakened production protections refuse publication."""
import copy
import datetime
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402
path = ROOT / ".github/scripts/check-release-gate.py"
gate = load(".github/scripts/check-release-gate.py")


class ReleaseGate(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "name": "production", "can_admins_bypass": False,
            "protection_rules": [{"type": "required_reviewers", "prevent_self_review": True,
                                  "reviewers": [{"type": "User", "reviewer": {"id": 1}}]}],
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        self.branches = {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]}

    def test_protected_environment_is_accepted(self):
        gate.validate(self.environment, self.branches)
        relaxed = copy.deepcopy(self.environment)
        relaxed["protection_rules"][0]["prevent_self_review"] = False
        gate.validate(relaxed, self.branches)

    def test_missing_or_weakened_review_is_rejected(self):
        variants = [{}, dict(self.environment, protection_rules=[]),
                    dict(self.environment, can_admins_bypass=True)]
        env = copy.deepcopy(self.environment)
        env["protection_rules"][0]["reviewers"] = []
        variants.append(env)
        for env in variants:
            with self.subTest(environment=env), self.assertRaises(ValueError):
                gate.validate(env, self.branches)

    def test_unrestricted_or_extra_branch_and_tag_policies_are_rejected(self):
        for policy in (None, {}, {"protected_branches": True, "custom_branch_policies": False}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                gate.validate(dict(self.environment, deployment_branch_policy=policy), self.branches)
        for policies in ([], [{"name": "*", "type": "branch"}], [{"name": "main", "type": "tag"}],
                         self.branches["branch_policies"] + [{"name": "dev", "type": "branch"}]):
            with self.subTest(policies=policies), self.assertRaises(ValueError):
                gate.validate(self.environment, {"total_count": len(policies), "branch_policies": policies})


if __name__ == "__main__":
    unittest.main()
