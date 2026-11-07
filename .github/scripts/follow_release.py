#!/usr/bin/env python3
"""Advance this repository's release of the newest private release by one step.

Usage: follow_release.py [--dry-run]

Run on a schedule by follow-private-release.yml. The private repository holds
only a read token, so this repository follows it rather than being pushed to.
decide() is the whole policy; it starts workflows and CI, and the production environment still gates every build and publication. A person
signs the tag, so a release stops there until the tag exists. A step that fails
is never started again by this script: it opens one issue and waits.

Environment: GH_TOKEN (this repository), PRIVATE_TOKEN (read-only, private
repository), GITHUB_REPOSITORY. Python 3.10+, standard library and gh only.
"""
import json
import os
import re
import subprocess
import sys

PRIVATE_REPOSITORY = "EDOS-Engineering/claude-plugin"
MANIFEST = ".claude-plugin/plugin.json"
FIRST_FOLLOWED = (0, 5, 8)
ISSUE_PREFIX = "Release follow:"
VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
FAILED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}


def numbers(version):
    return tuple(int(part) for part in version.split("-")[0].split("."))


def advance_pr(kind, pr):
    """The actions for one open pull request this process generated."""
    if pr["ejected"]:
        return [("notify", f"the {kind} pull request #{pr['number']} left the merge queue without merging")]
    if pr["in_queue"]:
        return [("wait", f"{kind} pull request #{pr['number']} is in the merge queue")]
    if pr["checks"] == "none":
        return [("start-ci", pr["branch"])]
    if pr["checks"] == "failed":
        return [("notify", f"the {kind} pull request #{pr['number']} failed its checks")]
    if pr["checks"] == "pending":
        return [("wait", f"{kind} pull request #{pr['number']} is being checked")]
    # A pull request queued with GITHUB_TOKEN gets no merge_group run and jams the
    # queue, so a person queues it.
    return [("notify", f"queue the {kind} pull request #{pr['number']}")]


def advance_run(workflow, version, run, pr_kind=None):
    """The action for a release step with no open pull request."""
    if run == "active":
        return [("wait", f"{workflow} {version} is running or waiting for approval")]
    if run == "failed":
        return [("notify", f"{workflow} {version} failed")]
    if run == "success":
        missing = f" and no {pr_kind} pull request is open" if pr_kind else ", but the release does not exist"
        return [("notify", f"{workflow} {version} succeeded{missing}")]
    return [("dispatch", workflow)]


def decide(state):
    """state -> [(action, detail)]. Pure, and every state is unit tested."""
    version = state["version"]
    if version is None:
        return [("done", "no private release to follow")]
    if state["released"]:
        if state["prs"].get("channel"):
            return advance_pr("marketplace", state["prs"]["channel"])
        return [("done", f"{version} is released")]
    main = state["main_version"]
    if numbers(main) > numbers(version):
        return [("done", f"main is already past {version}")]
    if main == version:
        if not state["tag"]:
            return [("notify", f"sign the tag v{version} on main's release commit")]
        return advance_run("publish-release", version, state["runs"].get("publish-release"))
    if main == f"{version}-dev":
        if state["prs"].get("release"):
            return advance_pr("release", state["prs"]["release"])
        return advance_run("package-runtime", version, state["runs"].get("package-runtime"), "release")
    if state["prs"].get("sync"):
        return advance_pr("sync", state["prs"]["sync"])
    return advance_run("public-sync", version, state["runs"].get("public-sync"), "sync")


def gh(*args, token=None):
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    done = subprocess.run(["gh", *args], check=True, capture_output=True, text=True, env=env)
    return done.stdout


def newest_private_release(token):
    listed = json.loads(gh("release", "list", "-R", PRIVATE_REPOSITORY, "--exclude-drafts", "--exclude-pre-releases",
                           "--limit", "30", "--json", "tagName", token=token))
    found = []
    for release in listed:
        match = VERSION.fullmatch(release["tagName"])
        if match:
            found.append(tuple(int(part) for part in match.groups()))
    newest = max(found, default=None)
    if newest is None or newest < FIRST_FOLLOWED:
        return None
    return ".".join(str(part) for part in newest)


def run_state(repository, workflow, version):
    """The newest run of a workflow for this version, by the run-name it sets."""
    runs = json.loads(gh("run", "list", "-R", repository, "--workflow", f"{workflow}.yml", "--limit", "30",
                         "--json", "status,conclusion,displayTitle"))
    for run in runs:
        if run["displayTitle"] == f"{workflow} {version}":
            if run["status"] != "completed":
                return "active"
            return "success" if run["conclusion"] == "success" else "failed"
    return None


PR_QUERY = """query($owner:String!,$name:String!,$head:String!){repository(owner:$owner,name:$name){
pullRequests(headRefName:$head,states:OPEN,first:1){nodes{id number headRefName isInMergeQueue
timelineItems(itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT],first:1){nodes{__typename}}
commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100){nodes{... on CheckRun{name status conclusion}}}}}}}}}}}"""


def checks_of(contexts):
    runs = [context for context in contexts if context.get("name")]
    if not runs:
        return "none"
    if any(run["conclusion"] in FAILED for run in runs):
        return "failed"
    gate = [run for run in runs if run["name"] == "ci-gate"]
    return "green" if gate and gate[0]["conclusion"] == "SUCCESS" else "pending"


def open_pr(repository, branch):
    owner, name = repository.split("/")
    data = json.loads(gh("api", "graphql", "-f", f"query={PR_QUERY}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"head={branch}"))
    nodes = data["data"]["repository"]["pullRequests"]["nodes"]
    if not nodes:
        return None
    pr = nodes[0]
    rollup = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
    return {"id": pr["id"], "number": pr["number"], "branch": pr["headRefName"], "in_queue": pr["isInMergeQueue"],
            "ejected": bool(pr["timelineItems"]["nodes"]), "checks": checks_of(rollup["contexts"]["nodes"] if rollup else [])}


def exists(*args):
    try:
        gh(*args)
    except subprocess.CalledProcessError:
        return False
    return True


def gather(repository, token):
    version = newest_private_release(token)
    if version is None:
        return {"version": None}
    raw = json.loads(gh("api", f"repos/{repository}/contents/{MANIFEST}?ref=main", "-H", "Accept: application/vnd.github.raw+json"))
    return {
        "version": version,
        "released": exists("release", "view", f"v{version}", "-R", repository, "--json", "tagName"),
        "tag": exists("api", f"repos/{repository}/git/ref/tags/v{version}"),
        "main_version": raw["version"],
        "prs": {"sync": open_pr(repository, f"sync/{version}"), "release": open_pr(repository, f"package-runtime/v{version}"),
                "channel": open_pr(repository, f"release-channel/v{version}")},
        "runs": {workflow: run_state(repository, workflow, version) for workflow in ("public-sync", "package-runtime", "publish-release")},
    }


def dispatch(repository, workflow, version, token):
    if workflow == "public-sync":
        fields = [f"version={version}"]
    else:
        fields = [f"plugin_version={version}"]
        if workflow == "package-runtime":
            manifest = json.loads(gh("release", "download", f"v{version}", "-R", PRIVATE_REPOSITORY, "-p", "manifest.json", "-O", "-", token=token))
            fields.append(f"ork_meter_ref={manifest['ork_meter_ref']}")
    flags = [flag for field in fields for flag in ("-f", field)]
    gh("workflow", "run", f"{workflow}.yml", "-R", repository, "--ref", "main", *flags)


def sync_issue(repository, message, version):
    """One open issue per message; every other follow issue is closed."""
    title = f"{ISSUE_PREFIX} {message}" if message else None
    issues = json.loads(gh("issue", "list", "-R", repository, "--state", "open", "--search", f'"{ISSUE_PREFIX}" in:title', "--json", "number,title"))
    for issue in issues:
        if issue["title"].startswith(ISSUE_PREFIX) and issue["title"] != title:
            gh("issue", "close", str(issue["number"]), "-R", repository, "--comment", "The release moved on.")
    if title and not any(issue["title"] == title for issue in issues):
        gh("issue", "create", "-R", repository, "--title", title, "--body",
           f"The automatic public release of {version} stopped here (docs/release.md, \"Syncing from development\"). "
           "It continues on the next scheduled run once this is resolved.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry_run = argv == ["--dry-run"]
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["PRIVATE_TOKEN"]
    state = gather(repository, token)
    actions = decide(state)
    message = None
    for action, detail in actions:
        print(f"{action}: {detail}")
        if action == "notify":
            message = detail
            print(f"::warning::{detail}")
        if dry_run:
            continue
        if action == "dispatch":
            dispatch(repository, detail, state["version"], token)
        elif action == "start-ci":
            gh("workflow", "run", "ci.yml", "-R", repository, "--ref", detail)
    if not dry_run:
        sync_issue(repository, message, state.get("version"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
