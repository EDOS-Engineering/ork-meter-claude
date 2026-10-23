"""Point the marketplace's ork-meter entry at a release tag, or check that it does.

Usage:
  point-marketplace.py set <version>   rewrite .claude-plugin/marketplace.json to v<version>
  point-marketplace.py check           verify the entry it already has

Run from the repository root with the tags fetched. Customers install the
plugin from the commit this entry names, not from main (docs/release.md,
"Release channel"), so the entry is a `url` (git) source on this repository's
HTTPS URL with the release tag as `ref` and the tag's commit as `sha`. Claude
Code checks out `sha` when both are set, so a tag moved later changes nothing.
It is `url` rather than `github` because Claude Code 2.1.302 fetched a
`github` source over SSH on `claude plugin update`, which failed on a machine
without an SSH key for github.com although the install had fallen back to
HTTPS; the HTTPS URL installs and updates the same way everywhere.

Before the first release tag is merged, the entry may be the bootstrap form
{"source": "url", "url": URL} with no `ref` or `sha`, which installs `main`;
`check` accepts it only while no v<version> tag is merged into HEAD, and the
first `set` replaces it.

Both commands require that the tag v<version> exists, that its commit is the
entry's sha and is in HEAD's history (merged to main), and that plugin.json at
that commit says <version>. `set` is how a release goes live and how a
rollback re-points to an earlier release; either way a human merges the
pull request that carries it. Python 3.10+, standard library and git only.
"""
import json
from pathlib import Path
import re
import subprocess
import sys

MARKETPLACE = Path(".claude-plugin/marketplace.json")
MANIFEST = ".claude-plugin/plugin.json"
PLUGIN = "ork-meter"
URL = "https://github.com/EDOS-Engineering/ork-meter-claude.git"
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[A-Za-z0-9.]+)?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def resolve(tag):
    """The commit tag points at, if it is merged into HEAD, and plugin.json's version there."""
    try:
        commit = git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
    except subprocess.CalledProcessError:
        raise ValueError(f"tag {tag} does not exist (fetch the tags first)") from None
    if subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"]).returncode != 0:
        raise ValueError(f"tag {tag} ({commit}) is not in HEAD's history; only merged releases are installable")
    version = json.loads(git("show", f"{commit}:{MANIFEST}")).get("version")
    return commit, version


def entry(marketplace):
    matches = [p for p in marketplace.get("plugins", []) if isinstance(p, dict) and p.get("name") == PLUGIN]
    if len(matches) != 1:
        raise ValueError(f"marketplace.json has {len(matches)} entries named {PLUGIN}, expected one")
    return matches[0]


def released_tags():
    """Release tags v<version> merged into HEAD."""
    tags = git("tag", "--list", "v*", "--merged", "HEAD").split()
    return sorted(t for t in tags if VERSION_RE.match(t[1:]))


def bootstrap_source():
    """The entry before the first release: this repository's main."""
    return {"source": "url", "url": URL}


def source_for(version, commit):
    return {"source": "url", "url": URL, "ref": f"v{version}", "sha": commit}


def validate(source, resolver, released=None):
    """Raise ValueError unless source is this repository pinned to a merged release tag and its commit.

    Before any release tag is merged into HEAD (``released`` False, or unset
    and no such tag), the bootstrap entry is accepted instead: Claude Code
    installs main. Once a release tag exists it is refused."""
    if source == bootstrap_source():
        if released is None:
            released = bool(released_tags())
        if released:
            raise ValueError(f"the {PLUGIN} entry installs main, but a release tag is merged; "
                             "pin it with point-marketplace.py set <version>")
        return f"{PLUGIN} installs main: no release tag is merged yet (bootstrap entry)"
    expected_keys = {"source", "url", "ref", "sha"}
    if not isinstance(source, dict) or set(source) != expected_keys:
        raise ValueError(f"the {PLUGIN} source must be an object with exactly {sorted(expected_keys)}, got {source!r}")
    if source["source"] != "url" or source["url"] != URL:
        raise ValueError(f"the {PLUGIN} source must be url {URL}, got {source['source']} {source['url']}")
    ref, sha = source["ref"], source["sha"]
    if not (isinstance(ref, str) and ref.startswith("v") and VERSION_RE.match(ref[1:])):
        raise ValueError(f"ref {ref!r} is not a release tag v<version>")
    if not (isinstance(sha, str) and SHA_RE.match(sha)):
        raise ValueError(f"sha {sha!r} is not a full 40-character lowercase commit SHA")
    commit, version = resolver(ref)
    if commit != sha:
        raise ValueError(f"tag {ref} is {commit}, but the entry pins sha {sha}")
    if version != ref[1:]:
        raise ValueError(f"plugin.json at {ref} says version {version!r}, not {ref[1:]}")
    return f"{PLUGIN} installs {ref} ({sha})"


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    if not ((command == "set" and len(argv) == 3) or (command == "check" and len(argv) == 2)):
        print("usage: point-marketplace.py set <version> | check", file=sys.stderr)
        return 2
    try:
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        plugin = entry(marketplace)
        if command == "set":
            version = argv[2].removeprefix("v")
            if not VERSION_RE.match(version):
                raise ValueError(f"version {version!r} is not semver without build metadata")
            commit, _ = resolve(f"v{version}")
            plugin["source"] = source_for(version, commit)
        verdict = validate(plugin["source"], resolve)
        if command == "set":
            MARKETPLACE.write_text(json.dumps(marketplace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else exc
        print(f"::error::marketplace.json: {detail}")
        return 1
    print(f"marketplace.json: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
