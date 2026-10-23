"""Regression checks for the opt-in lean main agent: scripts/lean_agent.py,
the /ork-meter:lean skill and docs/optional-agents/lean.md (#108)."""
import contextlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402
from support import ROOT  # noqa: E402

lean = support.load("scripts/lean_agent.py")

AGENT = ROOT / "docs" / "optional-agents" / "lean.md"
SKILL = ROOT / "skills" / "lean" / "SKILL.md"


def frontmatter(text):
    if not text.startswith("---\n"):
        return None
    fields = {}
    for line in text.split("---", 2)[1].strip("\n").splitlines():
        key, sep, value = line.partition(":")
        if sep and re.fullmatch(r"[a-z-]+", key):
            fields[key] = value.strip()
    return fields


def run(root, *mode):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = lean.main(["--cwd", str(root), *mode])
    return code, out.getvalue(), err.getvalue()


class AgentDefinition(unittest.TestCase):
    def test_frontmatter_parses_and_names_cannot_collide(self):
        text = AGENT.read_text(encoding="utf-8")
        fields = frontmatter(text)
        self.assertIsNotNone(fields)
        self.assertEqual(fields["name"], "ork-meter-lean")
        self.assertEqual(set(fields), {"name", "description"},
                         "a main agent inherits the session's tools and model; restricting them would break work")
        description = fields["description"]
        self.assertTrue(description and description[0] not in "|>\"'", "a plain one-line scalar")
        self.assertLessEqual(len(description), 160, "listed in every session of a project that installs it")
        self.assertIn("claude --agent ork-meter-lean", description)
        names = {frontmatter(path.read_text(encoding="utf-8"))["name"]
                 for path in AGENT.parent.glob("*.md") if path != AGENT}
        names |= {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")}
        self.assertNotIn(fields["name"], names)
        self.assertTrue(fields["name"].startswith("ork-meter-"), "a project agent name is not namespaced")

    def test_prompt_covers_the_lean_rules_without_weakening_correctness(self):
        body = AGENT.read_text(encoding="utf-8").split("---", 2)[2]
        self.assertLess(len(body.encode("utf-8")), 4096, "short, and the ownership marker stays in the first 4 KiB")
        self.assertIn(lean.MARKER, body[:200])
        for phrase in ("subagent", "ork-meter:explore", "offset", "limit", "Don't re-read",
                       "together", "Still read the code you change", "run the relevant tests",
                       "Confirm before hard-to-reverse", "Report outcomes faithfully"):
            self.assertIn(phrase, body)
        # Plugin variables are not substituted in a copied agent file.
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", body)
        self.assertNotIn("scripts/source.py", body)
        self.assertIsNone(re.search(r"(^|\s)!`", body), "no shell placeholders")

    def test_no_default_agent_and_no_default_setting(self):
        self.assertEqual([path.name for path in (ROOT / "agents").iterdir()], [".gitkeep"])
        for name in (".claude-plugin/plugin.json", "settings.json", ".claude-plugin/settings.json"):
            path = ROOT / name
            if path.exists():
                self.assertNotIn("ork-meter-lean", path.read_text(encoding="utf-8"))
                self.assertNotRegex(path.read_text(encoding="utf-8"), r'"agent"\s*:')

    def test_shipped_definition_is_what_install_copies(self):
        self.assertEqual(lean.definition(), AGENT.read_text(encoding="utf-8"))
        self.assertEqual(lean.SOURCE, AGENT)


class Skill(unittest.TestCase):
    def test_explicit_only_with_anchored_exact_commands(self):
        text = SKILL.read_text(encoding="utf-8")
        fields = frontmatter(text)
        self.assertEqual(fields["name"], "lean")
        self.assertEqual(fields["disable-model-invocation"], "true")
        rules = re.findall(r"Bash\(([^()]*)\)", fields["allowed-tools"])
        prefix = 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/lean_agent.py --cwd "${CLAUDE_PROJECT_DIR}" '
        self.assertEqual(rules, [prefix + mode for mode in ("--install", "--remove", "--status")])
        blocks = re.findall(r"```sh\n(.*?)```", text, re.S)
        self.assertEqual([block.strip() for block in blocks], rules)
        self.assertNotIn("settings.json\"", fields["allowed-tools"])
        for word in ("never reads or edits any settings file", "claude --agent ork-meter-lean",
                     "has not been measured"):
            self.assertIn(word, text)


class InstallRemoveStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ork-lean-")
        self.root = Path(self.tmp.name, "project")
        self.root.mkdir()
        self.agents = self.root / ".claude" / "agents"
        self.target = self.agents / "ork-meter-lean.md"

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_is_idempotent_writes_gitignore_and_remove_cleans_up(self):
        code, out, _ = run(self.root, "--status")
        self.assertEqual(code, 0)
        self.assertIn("Not installed", out)
        self.assertFalse((self.root / ".claude").exists(), "status changes nothing")
        code, out, err = run(self.root, "--install")
        self.assertEqual(code, 0, err)
        self.assertIn("Installed .claude/agents/ork-meter-lean.md", out)
        self.assertIn("claude --agent ork-meter-lean", out)
        self.assertIn("settings.local.json", out)
        self.assertEqual(self.target.read_bytes(), AGENT.read_bytes())
        gitignore = (self.agents / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(gitignore, lean.GITIGNORE_TEXT)
        self.assertEqual([line for line in gitignore.splitlines() if not line.startswith("#")],
                         ["/ork-meter-lean.md", "/.gitignore"], "the person's own agents stay visible to git")
        self.assertEqual(sorted(os.listdir(self.agents)), [".gitignore", "ork-meter-lean.md"])
        before = self.target.stat().st_mtime_ns
        code, out, _ = run(self.root, "--install")
        self.assertEqual(code, 0)
        self.assertIn("Unchanged:", out)
        self.assertEqual(self.target.stat().st_mtime_ns, before)
        code, out, _ = run(self.root, "--status")
        self.assertIn("Installed: .claude/agents/ork-meter-lean.md (current)", out)
        # An older copy we wrote is updated in place.
        self.target.write_text(lean.MARKER + "\nold\n", encoding="utf-8")
        self.assertIn("older", run(self.root, "--status")[1])
        self.assertIn("Updated", run(self.root, "--install")[1])
        self.assertEqual(self.target.read_bytes(), AGENT.read_bytes())
        code, out, _ = run(self.root, "--remove")
        self.assertEqual(code, 0)
        self.assertIn("Removed", out)
        self.assertFalse(self.agents.exists(), "nothing else was there, so the directory goes too")
        self.assertTrue((self.root / ".claude").is_dir())
        self.assertIn("nothing removed", run(self.root, "--remove")[1])

    def test_remove_keeps_the_persons_files(self):
        self.agents.mkdir(parents=True)
        (self.agents / "mine.md").write_text("---\nname: mine\n---\n", encoding="utf-8")
        self.assertEqual(run(self.root, "--install")[0], 0)
        self.assertEqual(run(self.root, "--remove")[0], 0)
        self.assertEqual(os.listdir(self.agents), ["mine.md"])

    def test_foreign_agent_file_is_never_overwritten_or_removed(self):
        self.agents.mkdir(parents=True)
        self.target.write_text("---\nname: ork-meter-lean\n---\nmine\n", encoding="utf-8")
        for mode in ("--install", "--remove"):
            code, _, err = run(self.root, mode)
            self.assertEqual(code, 1)
            self.assertIn("not written by /ork-meter:lean", err)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "---\nname: ork-meter-lean\n---\nmine\n")
        self.assertIn("not written by /ork-meter:lean", run(self.root, "--status")[1])
        self.assertFalse((self.agents / ".gitignore").exists())

    def test_foreign_gitignore_is_left_alone(self):
        self.agents.mkdir(parents=True)
        (self.agents / ".gitignore").write_text("secret.md\n", encoding="utf-8")
        code, out, _ = run(self.root, "--install")
        self.assertEqual(code, 0)
        self.assertIn("is not ours", out)
        self.assertEqual((self.agents / ".gitignore").read_text(encoding="utf-8"), "secret.md\n")
        run(self.root, "--remove")
        self.assertEqual(os.listdir(self.agents), [".gitignore"])

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_refuses_symlinked_directories_and_agent_file(self):
        outside = Path(self.tmp.name, "outside")
        outside.mkdir()
        (self.root / ".claude").symlink_to(outside, target_is_directory=True)
        for mode in ("--install", "--remove", "--status"):
            code, _, err = run(self.root, mode)
            self.assertEqual(code, 1, mode)
            self.assertIn("refusing to write through .claude", err)
        self.assertEqual(os.listdir(outside), [])
        (self.root / ".claude").unlink()
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "agents").symlink_to(outside, target_is_directory=True)
        code, _, err = run(self.root, "--install")
        self.assertEqual(code, 1)
        self.assertIn("not a plain directory", err)
        self.assertEqual(os.listdir(outside), [])
        (self.root / ".claude" / "agents").unlink()
        self.agents.mkdir()
        elsewhere = outside / "target.md"
        elsewhere.write_text(lean.MARKER + "\n", encoding="utf-8")
        self.target.symlink_to(elsewhere)
        self.assertEqual(run(self.root, "--install")[0], 1)
        self.assertEqual(run(self.root, "--remove")[0], 1)
        self.assertEqual(elsewhere.read_text(encoding="utf-8"), lean.MARKER + "\n")

    def test_never_touches_settings(self):
        settings = self.root / ".claude" / "settings.local.json"
        settings.parent.mkdir()
        settings.write_text('{"agent": "other"}', encoding="utf-8")
        run(self.root, "--install")
        run(self.root, "--status")
        run(self.root, "--remove")
        self.assertEqual(settings.read_text(encoding="utf-8"), '{"agent": "other"}')
        self.assertEqual(sorted(os.listdir(settings.parent)), ["settings.local.json"])
        source = (ROOT / "scripts" / "lean_agent.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"open\([^)]*settings")

    def test_cli_runs_isolated_through_the_wrapper(self):
        shell = support.git_bash()
        if shell is None:
            self.skipTest("Git Bash is not installed")
        env = {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_PLUGIN")}
        result = subprocess.run([shell, str(ROOT / "hooks" / "py.sh"), "--script", "scripts/lean_agent.py",
                                 "--cwd", str(self.root), "--install"],
                                cwd=self.tmp.name, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_bytes(), AGENT.read_bytes())
        result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts" / "lean_agent.py"),
                                 "--cwd", str(self.root / "missing"), "--status"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
