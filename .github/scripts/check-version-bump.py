"""Require a plugin.json version bump when the executable surface changed since the latest release.

Usage: check-version-bump.py [<rev>]   (default HEAD; run from the repository root)

The latest release is the highest-versioned v<semver> tag merged into <rev>.
If the plugin's executable surface at <rev> differs from that tag's, the
plugin.json version at <rev> must be greater than the tag's version (semver
precedence). If the surface is unchanged, the version may equal the tag's. It
may never be lower. With no release tag in <rev>'s history there is nothing to
compare against and the check passes.

The executable surface is everything a customer's Claude Code installs and
runs: SURFACE below, plus .claude-plugin/plugin.json except its "version".

Versions follow the reader's rule in tests/features-contract.test.sh, which the
Ork Meter platform applies to plugin_version: MAJOR.MINOR.PATCH, optionally
with one pre-release suffix of letters, digits and dots (0.4.0-dev,
0.4.0-rc.1). Build metadata (+...) is refused: semver ignores it in
precedence, so it can never be a bump. See docs/release.md, "Version bumps".

Needs the tags: in CI, check out with fetch-depth: 0. Python 3.10+, standard
library and git only.
"""
import json
import re
import subprocess
import sys

MANIFEST = ".claude-plugin/plugin.json"
SURFACE = (
    "hooks", "scripts", "bin", "runtime", "skills", "agents", "commands",
    "output-styles", "monitors", ".mcp.json", ".lsp.json", "settings.json",
    "requirements-hooks.txt",
    # Imported by scripts/package_runtime.py, which is on the surface.
    ".github/scripts/release_targets.py",
)
VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})(?:-([A-Za-z0-9.]{1,16}))?$"
)
IDENTIFIER_RE = re.compile(r"^(0|[1-9][0-9]*|[0-9]*[A-Za-z][A-Za-z0-9]*)$")


def parse(version):
    """A sortable key with semver 2.0 precedence; ValueError if not an accepted version."""
    match = VERSION_RE.match(version) if isinstance(version, str) else None
    if not match:
        raise ValueError(f"version {version!r} is not MAJOR.MINOR.PATCH[-pre.release] "
                         "(letters, digits and dots, at most 16, no build metadata)")
    core = tuple(int(part) for part in match.group(1, 2, 3))
    if match.group(4) is None:
        return core + ((1,),)  # a release ranks above every pre-release of the same core
    identifiers = match.group(4).split(".")
    if not all(IDENTIFIER_RE.match(i) for i in identifiers):
        raise ValueError(f"version {version!r} has an empty pre-release identifier "
                         "or a numeric one with a leading zero")
    # Numeric identifiers rank below alphanumeric ones; a longer list ranks
    # above its own prefix, which tuple comparison already does.
    pre = tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in identifiers)
    return core + ((0,) + pre,)


def manifest_without_version(text):
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{MANIFEST} is not a JSON object")
    return {k: v for k, v in data.items() if k != "version"}, data.get("version")


def validate(tag, tag_version, head_version, surface_changed):
    """Return a one-line verdict, or raise ValueError when the version must be bumped."""
    released, current = parse(tag_version), parse(head_version)
    if current < released:
        raise ValueError(f"{MANIFEST} version {head_version} is lower than the latest release "
                         f"{tag} ({tag_version}); versions never go back (roll back by re-pointing "
                         "the marketplace, docs/release.md \"Rollback\")")
    if surface_changed and current == released:
        raise ValueError(f"the executable surface changed since {tag} but {MANIFEST} still says "
                         f"{head_version}; bump it above {tag_version} (for example "
                         f"{suggest(tag_version)}), docs/release.md \"Version bumps\"")
    if surface_changed:
        return f"surface changed since {tag}; version {head_version} > {tag_version}: ok"
    return f"surface unchanged since {tag}; version {head_version}: ok"


def suggest(version):
    major, minor, patch = (int(p) for p in VERSION_RE.match(version).group(1, 2, 3))
    return f"{major}.{minor}.{patch + 1} or {major}.{minor + 1}.0-dev"


def latest_tag(tags):
    """The highest v<version> tag; tags that are not accepted versions are ignored."""
    best = None
    for tag in tags:
        if not tag.startswith("v"):
            continue
        try:
            key = parse(tag[1:])
        except ValueError:
            continue
        if best is None or key > best[0]:
            best = (key, tag)
    return best[1] if best else None


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def surface_changed(tag, rev):
    paths = git("diff", "--no-renames", "--name-only", f"{tag}^{{commit}}", rev, "--", *SURFACE).split()
    before, _ = manifest_without_version(git("show", f"{tag}^{{commit}}:{MANIFEST}"))
    after, _ = manifest_without_version(git("show", f"{rev}:{MANIFEST}"))
    if before != after:
        paths.append(f"{MANIFEST} (fields other than version)")
    return paths


def main(argv):
    if len(argv) > 2:
        print("usage: check-version-bump.py [<rev>]", file=sys.stderr)
        return 2
    rev = argv[1] if len(argv) == 2 else "HEAD"
    try:
        _, head_version = manifest_without_version(git("show", f"{rev}:{MANIFEST}"))
        parse(head_version)
        tag = latest_tag(git("tag", "--merged", rev, "--list", "v*").split())
        if tag is None:
            print(f"No v<version> release tag in {rev}'s history; version {head_version}: ok")
            return 0
        tag_version = tag[1:]
        changed = surface_changed(tag, rev)
        for path in changed[:20]:
            print(f"  changed since {tag}: {path}")
        if len(changed) > 20:
            print(f"  ... and {len(changed) - 20} more")
        print(validate(tag, tag_version, head_version, bool(changed)))
    except (subprocess.CalledProcessError, ValueError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else exc
        print(f"::error::Version check failed: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
