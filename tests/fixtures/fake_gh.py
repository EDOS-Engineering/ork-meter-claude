"""A fake `gh` for publish-release's Release steps (tests/tag_signature_regressions.py).

It answers only the calls those steps make, from a JSON state file named by
FAKE_GH_STATE, and appends each call's argv to the state's "calls":

* gh api -X POST repos/<repo>/releases -f tag_name=... -f target_commitish=...
  -F draft=true ... --jq .id: a new Release, printing its id;
* gh api -X POST https://uploads.github.com/repos/<repo>/releases/<id>/assets?name=<n>
  --input <file> --jq .state: an asset on that Release;
* gh api [--paginate] repos/<repo>/releases?per_page=100 --jq <filter>: the
  filters the workflow uses, matched by shape, not evaluated as jq;
* gh api repos/<repo>/releases/<id> [-X PATCH -F draft=... -f ...] [--jq ...];
* gh api -X DELETE repos/<repo>/releases/<id>;
* gh api repos/<repo>/git/ref/tags/<tag> and git/tags/<sha> with --jq
  '.object.type + " " + .object.sha'.

State: {"releases": [{"id", "tag_name", "draft", "target"}], "refs": {tag:
[type, sha]}, "tags": {sha: [type, sha]}, "move_on_publish": [type, sha] or
null, "fail_revert": bool, "fail_upload": bool, "calls": []}; a Release also
gets "assets" and "name". Anything else exits 2, so an unexpected call fails
the step under test.
"""
import json
import os
import re
import sys


def main(argv):
    path = os.environ["FAKE_GH_STATE"]
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    state["calls"].append(argv)
    try:
        status, out = dispatch(state, argv)
    finally:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    if out:
        print(out)
    return status


def option(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def fields(argv):
    pairs = {}
    for flag, value in zip(argv, argv[1:]):
        if flag in ("-F", "-f"):
            key, _, raw = value.partition("=")
            if flag == "-F" and raw.startswith("@"):
                with open(raw[1:], encoding="utf-8") as handle:
                    raw = handle.read()
                pairs[key] = raw
            else:
                pairs[key] = {"true": True, "false": False}.get(raw, raw) if flag == "-F" else raw
    return pairs


def dispatch(state, argv):
    if argv[:1] != ["api"]:
        return 2, f"fake gh: unsupported {argv}"
    method = option(argv, "-X") or "GET"
    jq = option(argv, "--jq") or ""
    upload = next((a for a in argv[1:] if a.startswith("https://uploads.github.com/repos/")), None)
    if upload:
        match = re.fullmatch(r"https://uploads\.github\.com/repos/[^/]+/[^/]+/releases/(\d+)/assets\?name=(.+)", upload)
        release = next((r for r in state["releases"] if match and r["id"] == int(match.group(1))), None)
        if method != "POST" or release is None or jq != ".state" or "--input" not in argv:
            return 1, "HTTP 404"
        if state.get("fail_upload"):
            return 1, "HTTP 502"
        release.setdefault("assets", []).append(match.group(2))
        return 0, "uploaded"
    endpoint = next(a for a in argv[1:] if a.startswith("repos/"))
    route = endpoint.split("/", 3)[3]
    if route == "releases" and method == "POST":
        changes = fields(argv)
        new = 1 + max([r["id"] for r in state["releases"]] or [100])
        state["releases"].append({"id": new, "tag_name": changes["tag_name"], "draft": changes.get("draft") is True,
                                  "target": changes.get("target_commitish"), "name": changes.get("name"),
                                  "body": changes.get("body")})
        return (0, str(new)) if jq == ".id" else (2, f"fake gh: unsupported filter {jq}")
    if route == "releases?per_page=100" and method == "GET":
        tag = re.search(r'\.tag_name == "([^"]+)"', jq).group(1)
        drafts_only = "select(.draft and" in jq
        found = [r for r in state["releases"] if r["tag_name"] == tag and (r["draft"] or not drafts_only)]
        if jq.endswith("| .id"):
            return 0, "\n".join(str(r["id"]) for r in found)
        if 'draft=\\(.draft)' in jq:
            return 0, "\n".join(f"{r['id']} draft={str(r['draft']).lower()}" for r in found)
        return 2, f"fake gh: unsupported filter {jq}"
    match = re.fullmatch(r"releases/(\d+)", route)
    if match:
        release = next((r for r in state["releases"] if r["id"] == int(match.group(1))), None)
        if release is None:
            return 1, "HTTP 404"
        if method == "DELETE":
            state["releases"].remove(release)
            return 0, ""
        if method == "PATCH":
            changes = fields(argv)
            if changes.get("draft") is True and state.get("fail_revert"):
                return 1, "HTTP 422: immutable release"
            release.update({k: v for k, v in changes.items() if k in ("draft", "tag_name")})
            if "target_commitish" in changes:
                release["target"] = changes["target_commitish"]
            if changes.get("draft") is False and state.get("move_on_publish"):
                state["refs"][release["tag_name"]] = state["move_on_publish"]
            return 0, ""
        if jq == '"\\(.draft) \\(.tag_name)"':
            return 0, f"{str(release['draft']).lower()} {release['tag_name']}"
        return 2, f"fake gh: unsupported filter {jq}"
    pair = '.object.type + " " + .object.sha'
    match = re.fullmatch(r"git/ref/tags/(.+)", route)
    if match and jq == pair:
        ref = state["refs"].get(match.group(1))
        return (0, " ".join(ref)) if ref else (1, "HTTP 404: Not Found")
    match = re.fullmatch(r"git/tags/([0-9a-f]+)", route)
    if match and jq == pair:
        obj = state["tags"].get(match.group(1))
        return (0, " ".join(obj)) if obj else (1, "HTTP 404: Not Found")
    return 2, f"fake gh: unsupported {argv}"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
