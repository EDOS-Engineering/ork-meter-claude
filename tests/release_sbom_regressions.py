"""The release SBOM describes the shipped bytes, or no SBOM is written (#31)."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import LOCKED, ROOT, fake_go_binary, hashlib_hex, hook_lock, load, pip_report, sbom_plugin_tree  # noqa: E402

sbom = load(".github/scripts/build-release-sbom.py")
targets = load(".github/scripts/release_targets.py")
REVISION = "a" * 40


def git(root, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
               GIT_COMMITTER_DATE="2026-10-19T18:00:00Z")
    return subprocess.check_output(["git", *args], cwd=root, env=env, text=True).strip()


class GoBuildInfo(unittest.TestCase):
    def test_the_shipped_runtime_binaries_carry_build_info_matching_go_version_m(self):
        go = shutil.which("go")
        for target in targets.TARGETS:
            binary = ROOT / "runtime" / target / targets.binary_name(target)
            with self.subTest(target=target):
                version, mod = sbom.go_buildinfo(binary.read_bytes())
                info = sbom.parse_modinfo(mod)
                self.assertTrue(version.startswith("go1."), version)
                self.assertEqual(info["path"], "github.com/EDOS-Engineering/ork-meter/cmd/claude-plugin-runtime")
                self.assertTrue(info["deps"])
                self.assertEqual(info["build"]["GOOS"] + "-" + info["build"]["GOARCH"], target)
                self.assertEqual(info["build"]["CGO_ENABLED"], "0")
                self.assertRegex(info["build"]["vcs.revision"], r"^[0-9a-f]{40}$")
                if go:   # the Go toolchain's own reader agrees, where one is installed
                    listed = subprocess.check_output([go, "version", "-m", str(binary)], text=True)
                    self.assertTrue(listed.splitlines()[0].endswith(": " + version))
                    deps = [tuple(line.split("\t")[2:5]) for line in listed.splitlines() if line.startswith("\tdep\t")]
                    self.assertEqual(deps, info["deps"])

    def test_synthetic_build_info_round_trips_and_replacements_win(self):
        data = fake_go_binary(b"\x7fELF" + b"x" * 37, revision=REVISION)
        version, mod = sbom.go_buildinfo(data)
        self.assertEqual(version, "go1.26.9")
        info = sbom.parse_modinfo(mod)
        self.assertEqual(info["build"]["vcs.revision"], REVISION)
        self.assertEqual(info["deps"], [("golang.org/x/sys", "v0.47.0", "h1:o7XGOvZQCADBQQ4Y7VNq2dRWQR7JmOUW8Kxx4ZsNgWs=")])
        replaced = sbom.parse_modinfo("dep\texample.com/a\tv1.0.0\th1:a=\n=>\texample.com/fork\tv1.0.1\th1:b=\n")
        self.assertEqual(replaced["deps"], [("example.com/fork", "v1.0.1", "h1:b=")])

    def test_missing_misaligned_or_truncated_build_info_is_refused(self):
        good = fake_go_binary(b"")
        for data in (b"no build info here", b"x" + good, good[:40]):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                sbom.go_buildinfo(data)


class Inputs(unittest.TestCase):
    def test_requirement_pins(self):
        self.assertEqual(sbom.requirement_pins("# tokenizer\ntiktoken==0.14.0  # pinned\n\nregex==2026.9.29\n"),
                         [("tiktoken", "0.14.0"), ("regex", "2026.9.29")])
        digest, other = "0" * 64, "f" * 64
        hashed = (f"# via pip-compile\ntiktoken==0.14.0 \\\n    --hash=sha256:{digest} \\\n    --hash=sha256:{other}\n"
                  f"regex==2026.9.29 --hash=sha256:{digest}  # via tiktoken\n")
        self.assertEqual(sbom.requirement_pins(hashed), [("tiktoken", "0.14.0"), ("regex", "2026.9.29")])
        for loose in ("tiktoken>=0.14", "tiktoken", "tiktoken==0.14.0 --hash=sha256:00", "-r other.txt",
                      f"tiktoken==0.14.0 --hash=md5:{digest[:32]}", f"tiktoken==0.14.0 --index-url=https://x.invalid",
                      f"tiktoken==0.14.0 \\\n    --hash=sha256:{digest} \\\n    --no-deps"):
            with self.subTest(line=loose), self.assertRaises(SystemExit):
                sbom.requirement_pins(loose + "\n")

    def test_the_lock_must_hash_every_package_and_lock_every_direct_pin(self):
        locked, direct = sbom.locked_requirements(hook_lock(), "tiktoken==0.14.0\n")
        self.assertEqual(sorted(locked), ["idna", "regex", "requests", "tiktoken"])
        self.assertEqual(direct, ["tiktoken"])
        self.assertEqual(locked["regex"][:2], ("regex", "2026.9.29"))
        self.assertEqual(len(locked["regex"][2]), 2)
        unhashed = hook_lock() + "certifi==2026.10.18\n"
        twice = hook_lock() + f"Regex==2026.9.29 --hash=sha256:{'0' * 64}\n"
        for lock, direct_text, pattern in ((unhashed, "tiktoken==0.14.0\n", "certifi==2026.10.18 has no --hash"),
                                           (twice, "tiktoken==0.14.0\n", "locked twice"),
                                           (hook_lock(), "tiktoken==0.15.0\n", "regenerate it"),
                                           (hook_lock(), "tiktoken==0.14.0\nnumpy==2.0.0\n", "numpy==2.0.0"),
                                           (hook_lock(), "tiktoken>=0.14\n", "not an exact")):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(SystemExit, pattern):
                sbom.locked_requirements(lock, direct_text)

    def test_the_repository_inputs_are_locked_and_the_vocabulary_matches_its_pin(self):
        locked, direct = sbom.locked_requirements((ROOT / "requirements-hooks.txt").read_text(),
                                                  (ROOT / "requirements-hooks.in").read_text())
        self.assertEqual(direct, ["tiktoken", "certifi"])
        self.assertTrue({"tiktoken", "regex", "requests", "charset-normalizer", "idna", "urllib3", "certifi"} <= set(locked))
        file_sha, raw_sha = sbom.vocabulary(ROOT)
        self.assertEqual(file_sha, hashlib.sha256((ROOT / sbom.VOCABULARY).read_bytes()).hexdigest())
        self.assertIn(f'VOCAB_SHA256 = "{raw_sha}"', (ROOT / sbom.TOKEN_GATE).read_text())

    def test_a_vocabulary_that_fails_its_pin_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sbom_plugin_tree(root)
            sbom.vocabulary(root)
            (root / sbom.VOCABULARY).write_bytes(__import__("gzip").compress(b"altered\n"))
            with self.assertRaises(SystemExit):
                sbom.vocabulary(root)

    def test_a_pip_report_that_disagrees_with_the_lock_is_refused(self):
        locked, _ = sbom.locked_requirements(hook_lock(), "tiktoken==0.14.0\n")
        resolved = sbom.resolved_python(pip_report(), locked)
        self.assertEqual([p["name"] for p in resolved], ["tiktoken", "regex", "requests", "idna"])
        self.assertEqual(resolved[0]["requires"], ["regex", "requests"])   # extras dropped
        extra = pip_report()
        extra["install"] = extra["install"] + [dict(extra["install"][-1], metadata={"name": "certifi", "version": "1"})]
        other_wheel = copy.deepcopy(pip_report())
        other_wheel["install"][1]["download_info"]["archive_info"]["hashes"]["sha256"] = "e" * 64
        more_locked, _ = sbom.locked_requirements(hook_lock(LOCKED + (("urllib3", "2.8.0"),)), "tiktoken==0.14.0\n")
        for report, lock, pattern in ((pip_report("0.13.0"), locked, "not the locked 0.14.0"),
                                      (pip_report(), more_locked, "resolves urllib3 to None"),
                                      (extra, locked, "certifi 1, which requirements-hooks.txt does not lock"),
                                      (other_wheel, locked, "not one requirements-hooks.txt locks"),
                                      (dict(pip_report(), version="2"), locked, "version '2' is not 1")):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(SystemExit, pattern):
                sbom.resolved_python(report, lock)


class SpdxIds(unittest.TestCase):
    def test_ids_are_valid_and_distinct_where_sanitising_would_merge_them(self):
        a, b = sbom.spdx_id("Go", "example.com/a-b", "v1.0.0"), sbom.spdx_id("Go", "example.com/a/b", "v1.0.0")
        self.assertNotEqual(a, b)
        self.assertNotEqual(sbom.spdx_id("Go", "a", "b-c"), sbom.spdx_id("Go", "a-b", "c"))
        self.assertEqual(a, sbom.spdx_id("Go", "example.com/a-b", "v1.0.0"))
        for spdx in (a, b):
            self.assertRegex(spdx, r"^SPDXRef-[A-Za-z0-9.-]+$")


class Document(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "plugin"
        self.root.mkdir()
        sbom_plugin_tree(self.root)
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "release")
        self.commit = git(self.root, "rev-parse", "HEAD")
        self.runtime = {}
        for target in targets.TARGETS:
            asset = f"{targets.BINARY}-{target}" + (".exe" if target.startswith("windows") else "")
            path = Path(self.temp.name) / asset
            path.write_bytes(fake_go_binary(f"synthetic {target}\n".encode(), revision=REVISION, target=target))
            self.runtime[asset] = {"target": target, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "file": path}

    def tearDown(self):
        self.temp.cleanup()

    def document(self, report=None, commit=REVISION, runtime=None):
        return sbom.build_sbom(self.root, "9.9.9", self.commit, "v1.2.3", commit,
                               ("ork-meter-claude-plugin-9.9.9.zip", "c" * 64), runtime or self.runtime, report)

    def test_the_document_is_spdx_2_3_and_every_relationship_names_a_package(self):
        doc = self.document(pip_report())
        self.assertEqual((doc["spdxVersion"], doc["dataLicense"], doc["SPDXID"]), ("SPDX-2.3", "CC0-1.0", "SPDXRef-DOCUMENT"))
        self.assertEqual(doc["creationInfo"]["created"], "2026-10-19T18:00:00Z")
        self.assertIn(self.commit, doc["documentNamespace"])
        ids = [p["SPDXID"] for p in doc["packages"]]
        self.assertEqual(len(ids), len(set(ids)))
        for spdx in ids:
            self.assertRegex(spdx, r"^SPDXRef-[A-Za-z0-9.-]+$")
        for rel in doc["relationships"]:
            self.assertIn(rel["spdxElementId"], ids + ["SPDXRef-DOCUMENT"])
            self.assertIn(rel["relatedSpdxElement"], ids)
        for package in doc["packages"]:
            for field in ("name", "versionInfo", "downloadLocation", "filesAnalyzed"):
                self.assertIn(field, package, package["SPDXID"])
        json.dumps(doc)

    def test_it_describes_every_asset_with_its_digest_and_what_each_contains(self):
        doc = self.document(pip_report())
        by_id = {p["SPDXID"]: p for p in doc["packages"]}
        rel = {(r["spdxElementId"], r["relationshipType"], r["relatedSpdxElement"]) for r in doc["relationships"]}
        described = {b for a, kind, b in rel if a == "SPDXRef-DOCUMENT" and kind == "DESCRIBES"}
        names = {by_id[d]["packageFileName"]: by_id[d]["checksums"][0]["checksumValue"] for d in described}
        self.assertEqual(names, {"ork-meter-claude-plugin-9.9.9.zip": "c" * 64,
                                 **{a: e["sha256"] for a, e in self.runtime.items()}})
        plugin = sbom.spdx_id("Asset", "ork-meter-claude-plugin-9.9.9.zip")
        source = sbom.spdx_id("Source", "ork-meter", REVISION)
        for asset in self.runtime:
            binary = sbom.spdx_id("Asset", asset)
            self.assertIn((plugin, "CONTAINS", binary), rel)
            self.assertIn((binary, "GENERATED_FROM", source), rel)
            self.assertIn((binary, "CONTAINS", sbom.spdx_id("Go", "golang.org/x/sys", "v0.47.0")), rel)
            self.assertIn((binary, "CONTAINS", sbom.spdx_id("Go", "stdlib", "go1.26.9")), rel)
            self.assertIn(REVISION, by_id[binary]["sourceInfo"])
        vocab = by_id[sbom.spdx_id("File", "o200k_base.tiktoken.gz")]
        self.assertIn((plugin, "CONTAINS", vocab["SPDXID"]), rel)
        self.assertEqual(vocab["checksums"][0]["checksumValue"],
                         hashlib.sha256((self.root / sbom.VOCABULARY).read_bytes()).hexdigest())
        self.assertEqual(vocab["licenseDeclared"], "MIT")
        self.assertIn(sbom.vocabulary(self.root)[1], vocab["comment"])
        TIKTOKEN, REGEX = sbom.spdx_id("PyPI", "tiktoken"), sbom.spdx_id("PyPI", "regex")
        tiktoken, regex = by_id[TIKTOKEN], by_id[REGEX]
        self.assertEqual(tiktoken["versionInfo"], "0.14.0")
        self.assertIn("required by requirements-hooks.in", tiktoken["comment"])
        self.assertIn("a dependency; locked in requirements-hooks.txt at this version with 2 SHA-256 hashes",
                      regex["comment"])
        self.assertEqual(regex["checksums"], [{"algorithm": "SHA256", "checksumValue": hashlib_hex("regex2026.9.29")}])
        self.assertEqual(regex["licenseDeclared"], "Apache-2.0 AND CNRI-Python")
        self.assertEqual(regex["externalRefs"][0]["referenceLocator"], "pkg:pypi/regex@2026.9.29")
        self.assertIn((TIKTOKEN, "OPTIONAL_DEPENDENCY_OF", plugin), rel)
        self.assertIn((REGEX, "DEPENDENCY_OF", TIKTOKEN), rel)
        self.assertIn((sbom.spdx_id("PyPI", "idna"), "DEPENDENCY_OF", sbom.spdx_id("PyPI", "requests")), rel)

    def test_without_a_pip_report_every_locked_package_is_listed(self):
        doc = self.document()
        python = [p for p in doc["packages"] if p["SPDXID"].startswith("SPDXRef-PyPI-")]
        self.assertEqual([(p["name"], p["versionInfo"]) for p in python], list(LOCKED))
        self.assertTrue(all("locked in requirements-hooks.txt" in p["comment"] for p in python))
        self.assertIn("no pip resolution report", doc["creationInfo"]["comment"])

    def test_modules_whose_names_sanitise_alike_are_both_listed(self):
        deps = [("example.com/a-b", "v1.0.0", "h1:a="), ("example.com/a/b", "v1.0.0", "h1:b=")]
        runtime = copy.deepcopy(self.runtime)
        for entry in runtime.values():
            entry["file"].write_bytes(fake_go_binary(b"m", revision=REVISION, deps=deps, target=entry["target"]))
        names = [p["name"] for p in self.document(runtime=runtime)["packages"]]
        self.assertIn("example.com/a-b", names)
        self.assertIn("example.com/a/b", names)

    def test_two_different_packages_with_one_id_are_refused_not_dropped(self):
        deps = [("example.com/a-b", "v1.0.0", "h1:a="), ("example.com/a/b", "v1.0.0", "h1:b=")]
        for entry in self.runtime.values():
            entry["file"].write_bytes(fake_go_binary(b"m", revision=REVISION, deps=deps, target=entry["target"]))
        lossy = lambda *parts: "SPDXRef-" + "-".join(re.sub(r"[^A-Za-z0-9.]+", "-", p) for p in parts)  # noqa: E731
        with unittest.mock.patch.object(sbom, "spdx_id", lossy), self.assertRaisesRegex(SystemExit, "share the SPDX ID"):
            self.document()

    def test_the_same_inputs_give_the_same_bytes(self):
        a, b = Path(self.temp.name) / "a.json", Path(self.temp.name) / "b.json"
        self.assertEqual(sbom.write_sbom(a, self.document(pip_report())), sbom.write_sbom(b, self.document(pip_report())))

    def test_a_binary_from_another_commit_a_modified_tree_or_without_build_info_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "built from ork-meter"):
            self.document(commit="b" * 40)
        for body in (fake_go_binary(b"m", revision=REVISION, modified="true"), b"not a go binary",
                     fake_go_binary(b"m", revision=None), fake_go_binary(b"m", revision=REVISION, modified=None)):
            runtime = copy.deepcopy(self.runtime)
            entry = runtime["ork-meter-plugin-runtime-linux-amd64"]
            entry["file"].write_bytes(body)
            with self.subTest(body=body[:8]), self.assertRaises(SystemExit):
                self.document(runtime=runtime)

    def test_vcs_fields_are_required_even_without_an_ork_meter_commit(self):
        # Grakk's #264 re-verification: with no commit to compare, a binary
        # without vcs.revision passed and a missing vcs.modified read as "false".
        self.assertTrue(self.document(commit=None)["packages"])
        for body, pattern in ((fake_go_binary(b"m", revision=None), "no vcs.revision or vcs.modified"),
                              (fake_go_binary(b"m", revision=REVISION, modified=None), "no vcs.revision or vcs.modified"),
                              (fake_go_binary(b"m", revision=REVISION, modified="true"), "modified ork-meter checkout"),
                              (fake_go_binary(b"m", revision=REVISION, modified=""), "modified ork-meter checkout")):
            runtime = copy.deepcopy(self.runtime)
            runtime["ork-meter-plugin-runtime-linux-amd64"]["file"].write_bytes(body)
            with self.subTest(pattern=pattern, body=body[-60:]), self.assertRaisesRegex(SystemExit, pattern):
                self.document(commit=None, runtime=runtime)


class Wiring(unittest.TestCase):
    def test_publish_release_resolves_wheels_only_without_the_signing_token_and_attests_the_sbom(self):
        workflow = (ROOT / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
        step = workflow[workflow.rindex("- name:", 0, workflow.index("--report")):workflow.index("- name:", workflow.index("--report"))]
        self.assertIn("--only-binary=:all:", step)
        self.assertIn("--dry-run", step)
        self.assertIn("unset ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN", step)
        self.assertIn("--pip-report", workflow)
        attest = workflow[workflow.index("actions/attest-build-provenance"):workflow.index("- name: Create the GitHub Release")]
        self.assertIn("/release/ork-meter-claude-plugin-*.spdx.json", attest)

    def test_publish_release_requires_and_always_passes_the_ork_meter_commit(self):
        workflow = (ROOT / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
        found = workflow.index("ork_meter_commit=$(git log")
        required = workflow.index('if [ -z "$ork_meter_commit" ]; then', found)
        self.assertLess(required, workflow.index('echo "ork_meter_commit=$ork_meter_commit"'))
        self.assertIn("exit 1", workflow[required:workflow.index("fi\n", required)])
        assemble = workflow[workflow.index("build-release-assets.py"):workflow.index("actions/attest-build-provenance")]
        self.assertIn('--ork-meter-commit "$ORK_METER_COMMIT"', assemble)
        self.assertNotIn("if [ -n", assemble)


if __name__ == "__main__":
    unittest.main()
