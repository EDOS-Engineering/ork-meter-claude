"""The release-asset builder ships exactly the reviewed commit, or nothing."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import fake_go_binary, load, pip_report, sbom_plugin_tree  # noqa: E402
assets = load(".github/scripts/build-release-assets.py")


def git(root, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.check_output(["git", *args], cwd=root, env=env, text=True).strip()


class ReleaseAssets(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "plugin"
        (self.root / ".claude-plugin").mkdir(parents=True)
        (self.root / ".claude-plugin/plugin.json").write_text('{\n  "name": "ork-meter",\n  "version": "9.9.9"\n}\n')
        (self.root / "README.md").write_text("synthetic\n")
        sbom_plugin_tree(self.root)
        self.binaries = {}
        for target in assets.TARGETS:
            name = assets.BINARY + (".exe" if target.startswith("windows") else "")
            package = self.root / "runtime" / target
            package.mkdir(parents=True)
            body = fake_go_binary(f"synthetic {target}\n".encode(), revision="a" * 40, target=target)
            (package / name).write_bytes(body)
            (package / "SHA256").write_text(hashlib.sha256(body).hexdigest() + "\n")
            self.binaries[target] = body
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "release: plugin 9.9.9 with runtime from ork-meter v1.2.3")
        (self.root / ".local").mkdir()
        (self.root / ".local/untracked").write_text("never shipped\n")
        self.output = Path(self.temp.name) / "out"

    def tearDown(self):
        self.temp.cleanup()

    def test_assets_cover_the_zip_each_runtime_and_a_manifest_that_agrees(self):
        record = assets.build(self.root, self.output, "9.9.9", "v1.2.3", "HEAD", "a" * 40)
        names = sorted(p.name for p in self.output.iterdir())
        expected = ["manifest.json", "ork-meter-claude-plugin-9.9.9.zip", "ork-meter-claude-plugin-9.9.9.zip.sha256",
                    "ork-meter-claude-plugin-9.9.9.spdx.json", "ork-meter-claude-plugin-9.9.9.spdx.json.sha256"]
        for target in assets.TARGETS:
            asset = f"ork-meter-plugin-runtime-{target}" + (".exe" if target.startswith("windows") else "")
            expected += [asset, asset + ".sha256"]
            self.assertEqual((self.output / asset).read_bytes(), self.binaries[target])
        self.assertEqual(names, sorted(expected))
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["plugin_version"], "9.9.9")
        self.assertEqual(manifest["tag"], "v9.9.9")
        self.assertEqual(manifest["ork_meter_ref"], "v1.2.3")
        self.assertEqual(manifest["ork_meter_commit"], "a" * 40)
        self.assertEqual(manifest["plugin_commit"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(manifest, record)
        for name, value in manifest["assets"].items():
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), value, name)
            self.assertEqual((self.output / (name + ".sha256")).read_text(), f"{value}  {name}\n")
        self.assertEqual(set(manifest["assets"]) - {"ork-meter-claude-plugin-9.9.9.zip", "ork-meter-claude-plugin-9.9.9.spdx.json"},
                         {entry["asset"] for entry in manifest["runtime"].values()})
        self.assertEqual(set(manifest["runtime"]), set(assets.TARGETS))

    def test_the_sbom_is_an_asset_its_digest_is_in_the_manifest_and_it_names_every_other_asset(self):
        manifest = assets.build(self.root, self.output, "9.9.9", "v1.2.3", "HEAD", "a" * 40, pip_report())
        name = "ork-meter-claude-plugin-9.9.9.spdx.json"
        digest = hashlib.sha256((self.output / name).read_bytes()).hexdigest()
        self.assertEqual(manifest["sbom"], {"asset": name, "format": "SPDX-2.3", "sha256": digest})
        self.assertEqual(manifest["assets"][name], digest)
        document = json.loads((self.output / name).read_text())
        self.assertEqual(document["spdxVersion"], "SPDX-2.3")
        listed = {p["packageFileName"]: p["checksums"][0]["checksumValue"]
                  for p in document["packages"] if p["SPDXID"].startswith("SPDXRef-Asset-")}
        self.assertEqual(listed, {n: v for n, v in manifest["assets"].items() if n != name})
        self.assertIn("regex", {p["name"] for p in document["packages"]})

    def test_a_pip_report_that_disagrees_with_the_pin_writes_no_sbom_and_no_manifest(self):
        with self.assertRaises(SystemExit):
            assets.build(self.root, self.output, "9.9.9", "v1.2.3", "HEAD", "a" * 40, pip_report("0.13.0"))
        self.assertFalse((self.output / "manifest.json").exists())
        self.assertFalse((self.output / "ork-meter-claude-plugin-9.9.9.spdx.json").exists())

    def test_zip_holds_the_tracked_tree_as_installed_and_nothing_untracked(self):
        assets.build(self.root, self.output, "9.9.9", "v1.2.3", "HEAD")
        with zipfile.ZipFile(self.output / "ork-meter-claude-plugin-9.9.9.zip") as archive:
            names = set(archive.namelist())
            self.assertIn("ork-meter/.claude-plugin/plugin.json", names)
            self.assertIn("ork-meter/runtime/linux-amd64/ork-meter-plugin-runtime", names)
            self.assertIn("ork-meter/runtime/windows-amd64/ork-meter-plugin-runtime.exe", names)
            self.assertFalse(any(".local" in n or ".git/" in n for n in names), names)
            self.assertEqual(archive.read("ork-meter/runtime/linux-amd64/SHA256").decode().strip(),
                             hashlib.sha256(self.binaries["linux-amd64"]).hexdigest())

    def test_version_mismatch_missing_target_and_tampered_binary_are_refused(self):
        with self.assertRaises(SystemExit):
            assets.build(self.root, self.output, "9.9.8", "v1.2.3", "HEAD")
        self.assertFalse(any(self.output.iterdir()) if self.output.exists() else False)
        tampered = self.root / "runtime/linux-arm64" / assets.BINARY
        tampered.write_bytes(b"altered after review\n")
        with self.assertRaises(SystemExit):
            assets.build(self.root, self.output / "tampered", "9.9.9", "v1.2.3", "HEAD")
        (self.root / "runtime/linux-arm64/SHA256").unlink()
        with self.assertRaises(SystemExit):
            assets.build(self.root, self.output / "missing", "9.9.9", "v1.2.3", "HEAD")
        self.assertFalse((self.output / "tampered/manifest.json").exists())
        self.assertFalse((self.output / "missing/manifest.json").exists())

    def test_a_non_empty_output_directory_is_refused(self):
        self.output.mkdir()
        (self.output / "stale").write_text("from an earlier run\n")
        with self.assertRaises(SystemExit):
            assets.build(self.root, self.output, "9.9.9", "v1.2.3", "HEAD")
        self.assertEqual([p.name for p in self.output.iterdir()], ["stale"])


if __name__ == "__main__":
    unittest.main()
