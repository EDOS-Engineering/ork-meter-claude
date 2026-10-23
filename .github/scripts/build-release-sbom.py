"""An SPDX 2.3 JSON SBOM for a plugin release's assets (#31).

build-release-assets.py calls build_sbom() after it has written the plugin zip
and the runtime binaries, and ships the result as
ork-meter-claude-plugin-<version>.spdx.json. One document describes every
release asset:

* the plugin zip (the tagged tree as installed), which CONTAINS each runtime
  binary and the bundled o200k_base vocabulary;
* each runtime binary, GENERATED_FROM the EDOS-Engineering/ork-meter commit,
  and CONTAINS every Go module linked into it and the Go standard library.
  These come from the build info the Go linker embeds in the binary itself
  (what `go version -m` prints; -s -w does not strip it), so they describe the
  shipped bytes, not a go.mod. Every binary must record vcs.revision and
  vcs.modified; vcs.revision must equal the ork-meter commit the release
  names, and vcs.modified must be false;
* the bundled vocabulary hooks/lib/data/o200k_base.tiktoken.gz, with the
  SHA-256 of the file and the decompressed SHA-256 pin from
  hooks/lib/token_gate.py, which it must match;
* the Python hook dependencies: every package in requirements-hooks.txt,
  the hash-locked closure of requirements-hooks.in (#265). Each must be an
  exact `name==version` pin with at least one `--hash=sha256:` (one per wheel
  and sdist), and the direct pins in requirements-hooks.in must be locked at
  the same versions. Given a pip installation report (`pip install --dry-run
  --report`), every package it resolves to, with the wheel's URL and SHA-256 as
  an SPDX checksum; the report must resolve exactly the locked packages and
  versions, and each wheel's SHA-256 must be one the lock lists. They are
  optional: without them the hooks use the bundled pure-Python encoder.
  Anything else is refused.

Output is deterministic for the same inputs: its timestamp is the release
commit's committer date. Python 3.10+, standard library and git only.
"""
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone

REPOSITORY = "EDOS-Engineering/ork-meter-claude"
ORK_METER = "EDOS-Engineering/ork-meter"
VOCABULARY = "hooks/lib/data/o200k_base.tiktoken.gz"
VOCABULARY_LICENSE = "hooks/lib/data/LICENSE-tiktoken.txt"
TOKEN_GATE = "hooks/lib/token_gate.py"
REQUIREMENTS = "requirements-hooks.txt"
DIRECT_REQUIREMENTS = "requirements-hooks.in"
PROPRIETARY = "LicenseRef-EDOS-Proprietary"
BUILDINFO_MAGIC = b"\xff Go buildinf:"
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.+!_-]*)")
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


class Refused(SystemExit):
    """The inputs disagree with what the release claims; no SBOM is written."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def spdx_id(*parts):
    """A readable SPDX ID that is unique per ``parts``: the sanitised parts
    (SPDX allows only letters, digits, "." and "-", so x/a-b and x/a/b read
    alike) plus 12 hex digits of the SHA-256 of the exact parts."""
    readable = "-".join(re.sub(r"[^A-Za-z0-9.]+", "-", p).strip("-") for p in parts)
    return f"SPDXRef-{readable}-{sha256(chr(0).join(parts).encode())[:12]}"


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


# Go build info --------------------------------------------------------------

def _uvarint(data, at):
    value = shift = 0
    while True:
        if at >= len(data) or shift > 63:
            raise ValueError("truncated varint")
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7


def go_buildinfo(data):
    """(Go version, module info text) embedded by the Go 1.18+ linker; ValueError if absent.

    The same format debug/buildinfo reads: a 16-byte-aligned header, the magic,
    pointer size, a flags byte whose bit 2 means inline strings, then at +32 two
    varint-length strings. The module info is framed by 16-byte sentinels."""
    at = data.find(BUILDINFO_MAGIC)
    while at >= 0:
        if at % 16 == 0 and at + 32 <= len(data) and data[at + 15] & 0x2:
            try:
                size, start = _uvarint(data, at + 32)
                version = data[start:start + size].decode()
                size, start = _uvarint(data, start + size)
                mod = data[start:start + size]   # sentinels are not UTF-8: strip, then decode
                if version.startswith("go") and len(mod) >= 33 and mod[-17] == 0x0A:
                    return version, mod[16:-16].decode()
            except (ValueError, UnicodeDecodeError):
                pass
        at = data.find(BUILDINFO_MAGIC, at + 1)
    raise ValueError("no Go 1.18+ build info found")


def parse_modinfo(text):
    """{path, main: (path, version), deps: [(path, version, h1)], build: {key: value}}."""
    info = {"path": None, "main": None, "deps": [], "build": {}}
    for line in text.splitlines():
        fields = line.split("\t")
        if fields[0] == "path" and len(fields) > 1:
            info["path"] = fields[1]
        elif fields[0] == "mod" and len(fields) > 2:
            info["main"] = (fields[1], fields[2])
        elif fields[0] == "dep" and len(fields) > 2:
            info["deps"].append([fields[1], fields[2], fields[3] if len(fields) > 3 else ""])
        elif fields[0] == "=>" and len(fields) > 2 and info["deps"]:
            # A replaced module: what was linked is the replacement.
            info["deps"][-1] = [fields[1], fields[2], fields[3] if len(fields) > 3 else ""]
        elif fields[0] == "build" and len(fields) > 1:
            key, _, value = fields[1].partition("=")
            info["build"][key] = value
    info["deps"] = [tuple(d) for d in info["deps"]]
    return info


# Inputs from the plugin tree ------------------------------------------------

def requirements(text, source=REQUIREMENTS):
    """[(name, version, [sha256])] of a requirements file; Refused unless every requirement is an exact pin.

    A pin may carry `--hash=sha256:<hex>` options, on the same line or on
    backslash-continued lines, as `uv pip compile --generate-hashes` and
    pip-compile write them. Comments and blank lines are ignored."""
    found, logical, start = [], "", 1
    lines = text.split("\n")
    for number, raw in enumerate(lines, 1):
        if not logical:
            start = number
        part = raw.split(" #", 1)[0] if not raw.lstrip().startswith("#") else ""
        if part.rstrip().endswith("\\") and number < len(lines):
            logical += part.rstrip()[:-1] + " "
            continue
        logical, line = "", (logical + part).strip()
        if not line:
            continue
        name_version, *options = line.split()
        match = PIN.fullmatch(name_version)
        if not match or not all(HASH.fullmatch(option) for option in options):
            raise Refused(f"{source} line {start}: {line!r} is not an exact name==version pin "
                          "(optionally with --hash=sha256:<hex>); the SBOM records pins only "
                          "(update build-release-sbom.py to change that)")
        found.append((match.group(1), match.group(2), [o.split(":", 1)[1] for o in options]))
    return found


def requirement_pins(text, source=REQUIREMENTS):
    """[(name, version)] of a requirements file; Refused unless every requirement is an exact pin."""
    return [(name, version) for name, version, _ in requirements(text, source)]


def locked_requirements(lock_text, direct_text):
    """{normalized name: (name, version, [sha256])} of requirements-hooks.txt, and the
    normalized names requirements-hooks.in asks for directly.

    Refused unless every locked package has at least one hash, none is locked
    twice, and every direct pin is locked at its version (a stale lock)."""
    locked = {}
    for name, version, hashes in requirements(lock_text):
        if not hashes:
            raise Refused(f"{REQUIREMENTS}: {name}=={version} has no --hash=sha256: options; "
                          "regenerate it with .github/scripts/lock-hook-requirements.py")
        if normalize(name) in locked:
            raise Refused(f"{REQUIREMENTS}: {name} is locked twice")
        locked[normalize(name)] = (name, version, hashes)
    direct = []
    for name, version in requirement_pins(direct_text, DIRECT_REQUIREMENTS):
        found = locked.get(normalize(name))
        if not found or found[1] != version:
            raise Refused(f"{DIRECT_REQUIREMENTS} pins {name}=={version} but {REQUIREMENTS} locks "
                          f"{found and found[1]!r}; regenerate it with .github/scripts/lock-hook-requirements.py")
        direct.append(normalize(name))
    return locked, direct


def vocabulary(root):
    """(file SHA-256, decompressed SHA-256) of the bundled vocabulary; Refused if it fails its pin."""
    data = (root / VOCABULARY).read_bytes()
    pin = re.search(r'^VOCAB_SHA256 = "([0-9a-f]{64})"$', (root / TOKEN_GATE).read_text(encoding="utf-8"), re.M)
    if not pin:
        raise Refused(f"{TOKEN_GATE} has no VOCAB_SHA256 pin")
    decompressed = sha256(gzip.decompress(data))
    if decompressed != pin.group(1):
        raise Refused(f"{VOCABULARY} decompresses to {decompressed}, not the pinned {pin.group(1)}")
    return sha256(data), decompressed


def resolved_python(report, locked):
    """[{name, version, sha256, url, license, requested, requires}] from a pip report.

    Refused unless it resolves exactly the ``locked`` packages
    ({normalized name: (name, version, [sha256])}), each at its locked version
    and from a file whose SHA-256 the lock lists."""
    if str(report.get("version")) != "1":
        raise Refused(f"pip report version {report.get('version')!r} is not 1")
    packages = []
    for item in report.get("install", []):
        meta, download = item.get("metadata", {}), item.get("download_info", {})
        hashes = (download.get("archive_info") or {}).get("hashes") or {}
        requires = []
        for requirement in meta.get("requires_dist") or []:
            requirement, _, marker = requirement.partition(";")
            if "extra" in marker:
                continue
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
            if match:
                requires.append(normalize(match.group(1)))
        packages.append({"name": meta.get("name", ""), "version": meta.get("version", ""),
                         "sha256": hashes.get("sha256"), "url": download.get("url"),
                         "license": meta.get("license_expression"), "requested": bool(item.get("requested")),
                         "requires": requires})
    by_name = {normalize(p["name"]): p for p in packages}
    for name, (_, version, _) in locked.items():
        found = by_name.get(name)
        if not found or found["version"] != version:
            raise Refused(f"pip report resolves {name} to {found and found['version']!r}, not the locked {version}")
    for name, found in by_name.items():
        if name not in locked:
            raise Refused(f"pip report resolves {found['name']} {found['version']}, which {REQUIREMENTS} does not lock")
        if found["sha256"] not in locked[name][2]:
            raise Refused(f"pip report's {found['name']} {found['version']} file has SHA-256 {found['sha256']}, "
                          f"not one {REQUIREMENTS} locks")
    return packages


# The document ---------------------------------------------------------------

def purl(kind, locator):
    return [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl", "referenceLocator": f"pkg:{kind}/{locator}"}]


def package(spdx, name, version, purpose, download="NOASSERTION", sha=None, **extra):
    entry = {"SPDXID": spdx, "name": name, "versionInfo": version, "downloadLocation": download,
             "filesAnalyzed": False, "primaryPackagePurpose": purpose,
             "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION", "copyrightText": "NOASSERTION"}
    if sha:
        entry["checksums"] = [{"algorithm": "SHA256", "checksumValue": sha}]
    entry.update(extra)
    return entry


def build_sbom(root, version, plugin_commit, ork_meter_ref, ork_meter_commit, zip_asset, runtime_assets, pip_report=None):
    """The SPDX document as a dict.

    zip_asset is (name, sha256); runtime_assets maps asset name to
    {target, sha256, file}, where file is the asset on disk."""
    root = Path(root)
    release = f"https://github.com/{REPOSITORY}/releases/download/v{version}"
    epoch = subprocess.check_output(["git", "log", "-1", "--format=%ct", plugin_commit], cwd=root, text=True).strip()
    created = datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    packages, relationships, seen = [], [], {}

    def add(entry):
        # The same module linked into several binaries is one package; two
        # different packages with one ID would silently lose one, so refuse.
        if entry["SPDXID"] in seen:
            if seen[entry["SPDXID"]] != entry:
                raise Refused(f"two different packages share the SPDX ID {entry['SPDXID']}")
        else:
            seen[entry["SPDXID"]] = entry
            packages.append(entry)
        return entry["SPDXID"]

    def relate(a, kind, b):
        relationships.append({"spdxElementId": a, "relationshipType": kind, "relatedSpdxElement": b})

    zip_name, zip_sha = zip_asset
    plugin = add(package(
        spdx_id("Asset", zip_name), "ork-meter-claude-plugin", version, "ARCHIVE", f"{release}/{zip_name}", zip_sha,
        packageFileName=zip_name, supplier="Organization: EDOS", licenseDeclared=PROPRIETARY,
        sourceInfo=f"git archive of {REPOSITORY} commit {plugin_commit} (tag v{version})",
        externalRefs=purl("github", f"{REPOSITORY}@v{version}")))
    relate("SPDXRef-DOCUMENT", "DESCRIBES", plugin)

    source = add(package(
        spdx_id("Source", "ork-meter", ork_meter_commit or ork_meter_ref), "ork-meter", ork_meter_ref, "SOURCE",
        f"git+https://github.com/{ORK_METER}.git@{ork_meter_commit}" if ork_meter_commit else "NOASSERTION",
        supplier="Organization: EDOS",
        sourceInfo=f"{ORK_METER} {ork_meter_ref}" + (f" ({ork_meter_commit})" if ork_meter_commit else ""),
        externalRefs=purl("github", f"{ORK_METER}@{ork_meter_commit or ork_meter_ref}")))

    for asset, entry in sorted(runtime_assets.items()):
        data = Path(entry["file"]).read_bytes()
        try:
            go_version, mod = go_buildinfo(data)
        except ValueError as error:
            raise Refused(f"{asset}: {error}; every runtime binary is a Go 1.18+ build")
        info = parse_modinfo(mod)
        build = info["build"]
        # Always required, with or without an ork-meter commit to compare: a
        # binary without them cannot be tied to any ork-meter source.
        if "vcs.revision" not in build or "vcs.modified" not in build:
            raise Refused(f"{asset} records no vcs.revision or vcs.modified, so it cannot be tied to an ork-meter commit")
        if ork_meter_commit and build["vcs.revision"] != ork_meter_commit:
            raise Refused(f"{asset} was built from ork-meter {build['vcs.revision']}, not {ork_meter_commit}")
        if build["vcs.modified"] != "false":
            raise Refused(f"{asset} was built from a modified ork-meter checkout")
        settings = " ".join(f"{k}={v}" for k, v in build.items() if not k.startswith("vcs"))
        binary = add(package(
            spdx_id("Asset", asset), asset, version, "APPLICATION", f"{release}/{asset}", entry["sha256"],
            packageFileName=asset, supplier="Organization: EDOS",
            sourceInfo=(f"{info['path']} from {ORK_METER} {ork_meter_ref} at vcs.revision "
                        f"{build['vcs.revision']}, built by package-runtime with {go_version} ({settings})"),
            comment=f"runtime target {entry['target']}; modules from the binary's embedded Go build info"))
        relate("SPDXRef-DOCUMENT", "DESCRIBES", binary)
        relate(plugin, "CONTAINS", binary)
        relate(binary, "GENERATED_FROM", source)
        stdlib = add(package(spdx_id("Go", "stdlib", go_version), "stdlib", go_version, "LIBRARY",
                             "https://go.dev/dl/", licenseDeclared="BSD-3-Clause",
                             externalRefs=purl("golang", f"stdlib@{go_version.removeprefix('go')}")))
        relate(binary, "CONTAINS", stdlib)
        for path, mod_version, h1 in info["deps"]:
            dep = add(package(spdx_id("Go", path, mod_version), path, mod_version, "LIBRARY",
                              externalRefs=purl("golang", f"{path}@{mod_version}"),
                              **({"comment": f"go.sum {h1}"} if h1 else {})))
            relate(binary, "CONTAINS", dep)

    vocab_sha, vocab_raw = vocabulary(root)
    vocab = add(package(
        spdx_id("File", "o200k_base.tiktoken.gz"), "o200k_base.tiktoken.gz", "o200k_base", "FILE",
        "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken", vocab_sha,
        packageFileName=VOCABULARY, supplier="Organization: OpenAI", licenseDeclared="MIT",
        copyrightText="Copyright (c) 2022 OpenAI, Shantanu Jain",
        comment=f"tiktoken o200k_base vocabulary, gzip; decompressed SHA-256 {vocab_raw} is pinned in "
                f"{TOKEN_GATE} and checked by the hooks before use; licence in {VOCABULARY_LICENSE}"))
    relate(plugin, "CONTAINS", vocab)

    locked, direct = locked_requirements((root / REQUIREMENTS).read_text(encoding="utf-8"),
                                         (root / DIRECT_REQUIREMENTS).read_text(encoding="utf-8"))

    def locked_as(name):
        how = f"required by {DIRECT_REQUIREMENTS}" if name in direct else "a dependency"
        return (f"{how}; locked in {REQUIREMENTS} at this version with {len(locked[name][2])} SHA-256 "
                "hashes, one per wheel and sdist, which pip --require-hashes enforces")

    if pip_report is None:
        for name, (display, version, _) in sorted(locked.items()):
            dep = add(package(spdx_id("PyPI", name), display, version, "LIBRARY",
                              externalRefs=purl("pypi", f"{name}@{version}"),
                              comment=f"{locked_as(name)}; optional, installed by the customer"))
            relate(dep, "OPTIONAL_DEPENDENCY_OF", plugin)
    else:
        resolved = resolved_python(pip_report, locked)
        environment = pip_report.get("environment", {})
        where = (f"pip {pip_report.get('pip_version', '?')} on Python {environment.get('python_full_version', '?')} "
                 f"{environment.get('sys_platform', '?')} {environment.get('platform_machine', '?')}")
        ids = {}
        for item in resolved:
            name = normalize(item["name"])
            ids[name] = add(package(
                spdx_id("PyPI", name), item["name"], item["version"], "LIBRARY", item["url"] or "NOASSERTION",
                item["sha256"], licenseDeclared=item["license"] or "NOASSERTION",
                externalRefs=purl("pypi", f"{name}@{item['version']}"),
                comment=(f"{locked_as(name)}; the wheel and SHA-256 shown, one of those, as resolved by {where}; "
                         "other platforms install another locked wheel; optional, installed by the customer")))
        for item in resolved:
            name = normalize(item["name"])
            if name in direct:
                relate(ids[name], "OPTIONAL_DEPENDENCY_OF", plugin)
            for needed in item["requires"]:
                if needed in ids:
                    relate(ids[needed], "DEPENDENCY_OF", ids[name])

    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"ork-meter-claude-plugin-{version}",
        "documentNamespace": f"https://github.com/{REPOSITORY}/releases/tag/v{version}/sbom/{plugin_commit}",
        "creationInfo": {
            "created": created,
            "creators": ["Organization: EDOS", "Tool: build-release-sbom.py"],
            "comment": ("Release assets of the Ork Meter plugin for Claude Code. Runtime modules come from each "
                        "binary's embedded Go build info. Python packages are the hooks' optional tokenizer, every "
                        "one locked with its hashes in requirements-hooks.txt" +
                        ("; each wheel is the one pip resolved when the release was published."
                         if pip_report is not None else "; no pip resolution report was given.")),
        },
        "hasExtractedLicensingInfos": [{
            "licenseId": PROPRIETARY, "name": "EDOS proprietary licence",
            "extractedText": "Copyright (c) 2026 EDOS. All rights reserved. See LICENSE in the plugin tree.",
        }],
        "packages": packages,
        "relationships": relationships,
    }


def write_sbom(path, document):
    Path(path).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    return sha256(Path(path).read_bytes())
