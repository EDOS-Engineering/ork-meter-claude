"""Refuse publication unless GitHub's production environment enforces review."""
import json
import sys


def validate(environment, branches):
    """Raise ValueError unless production is protected."""
    if environment.get("name") != "production":
        raise ValueError("production environment is missing")
    rules = environment.get("protection_rules", [])
    review = next((r for r in rules if r.get("type") == "required_reviewers"), {})
    if not review.get("reviewers"):
        raise ValueError("production must require reviewers")
    # Self-review is allowed: the release engineer who dispatches a run may
    # also approve it (the same posture as ork-meter-codex). Reviewers,
    # no admin bypass and main-only deployment are still required.
    if environment.get("can_admins_bypass") is not False:
        raise ValueError("production must disable administrator bypass")
    policy = environment.get("deployment_branch_policy") or {}
    if policy.get("custom_branch_policies") is not True or policy.get("protected_branches") is not False:
        raise ValueError("production must use selected deployment branches")
    allowed = branches.get("branch_policies", [])
    if (branches.get("total_count") != 1 or len(allowed) != 1
            or allowed[0].get("name") != "main" or allowed[0].get("type") != "branch"):
        raise ValueError("production must allow only the main branch, with no tags")


def main(argv):
    try:
        with open(argv[1], encoding="utf-8") as fh:
            environment = json.load(fh)
        with open(argv[2], encoding="utf-8") as fh:
            branches = json.load(fh)
        validate(environment, branches)
    except (OSError, ValueError, TypeError, AttributeError, IndexError) as exc:
        print("Release gate refused publication: %s" % exc, file=sys.stderr)
        return 1
    print("Production review and main-only deployment restrictions verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
