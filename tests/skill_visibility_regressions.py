"""The token-saving plugin advertises no bundled skill in normal prompts:
every bundled skill is explicit-only (`disable-model-invocation: true`).

`explore` was the one model-invocable exception (#107), listed in every
session so Claude could batch its exploration. It was invoked 0 times in 23
live sessions, even with the #284 listing that names its triggers in
`when_to_use`, so it is user-invocable only too (#282); `/ork-meter:explore`
still runs it. Its description and `when_to_use` stay valid listing scalars:
the native qualification re-lists it as the control that measures what its
line cost each request.
"""
from pathlib import Path
import json
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT  # noqa: E402

# Model-invocable skills, listed in every request. Empty since #282: explore,
# accepted in #107, was never invoked and is explicit-only like the rest.
MODEL_INVOCABLE = frozenset()
# The former exception, re-listed by the native qualification as its control.
FORMERLY_LISTED = "explore"


def listing(front):
    """The frontmatter fields Claude Code would list for a model-invocable skill,
    each exactly once: ``{"description": ..., "when_to_use": ...}``."""
    fields = {}
    for line in front.splitlines():
        key, _, value = line.partition(":")
        if key in ("description", "when_to_use"):
            if key in fields:
                raise AssertionError(key + " appears twice")
            fields[key] = value.strip()
    return fields


class SkillVisibility(unittest.TestCase):
    def test_no_default_agent_descriptions(self):
        self.assertEqual(list((ROOT / "agents").glob("*.md")), [])

    def test_no_default_mcp_registration(self):
        manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertNotIn("mcpServers", manifest, "MCP must not add default tool-schema overhead")
        for name in (".mcp.json", "mcp.json"):
            self.assertFalse((ROOT / name).exists(), "default MCP discovery must remain absent")

    def test_every_bundled_skill_but_the_allowlist_is_explicit_only(self):
        paths = list((ROOT / "skills").glob("*/SKILL.md"))
        self.assertGreaterEqual(len(paths), 8)
        self.assertLessEqual(MODEL_INVOCABLE | {FORMERLY_LISTED}, {path.parent.name for path in paths})
        for path in paths:
            with self.subTest(skill=path.parent.name):
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("---\n"), "frontmatter must be parsed by the host")
                front = text.split("---", 2)[1]
                settings = [line.split(":", 1)[1].strip() for line in front.splitlines()
                            if line.startswith("disable-model-invocation:")]
                self.assertNotIn("user-invocable: false", front, "explicit slash invocation remains available")
                self.assertEqual(re.findall(r"^name: (.+)$", front, re.M), [path.parent.name])
                if path.parent.name not in MODEL_INVOCABLE:
                    self.assertEqual(settings, ["true"])
                    if path.parent.name != FORMERLY_LISTED:
                        continue
                else:
                    self.assertEqual(settings, [], "an allowlisted skill is listed for the model")
                # A listing line, "description - when_to_use", is a
                # per-request cost: two short plain scalars (#282).
                listed = listing(front)
                self.assertEqual(set(listed), {"description", "when_to_use"},
                                 "name the triggers in when_to_use, not only the capability")
                for value in listed.values():
                    self.assertTrue(value and value[0] not in "|>\"'[{&*!%@`", "a plain one-line scalar")
                    self.assertNotIn(": ", value, "a colon-space ends a plain YAML scalar")
                    self.assertNotIn(" #", value, "a space-hash starts a YAML comment")
                self.assertLessEqual(len(listed["description"]) + len(listed["when_to_use"]), 250)

    def test_explore_is_explicit_only(self):
        # #282: 0 invocations in 23 live sessions (P2b, P3a and the #284
        # re-probe); the fallback agreed there drops its listing line.
        front = (ROOT / "skills/explore/SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
        self.assertNotIn("explore", MODEL_INVOCABLE)
        self.assertIn("\ndisable-model-invocation: true\n", front)
        self.assertNotIn("user-invocable", front, "/ork-meter:explore stays available to the person")


    def test_skill_permissions_are_anchored_at_the_plugin_root(self):
        # A leading wildcard such as `bash *hooks/py.sh*` also matches a
        # look-alike `./hooks/py.sh` shipped by an untrusted repository (#126).
        anchored = re.compile(r'bash "\$\{CLAUDE_PLUGIN_ROOT\}/(hooks/py\.sh" --script scripts/\w+\.py'
                              r'|scripts/runtime\.sh" --plugin-data "\$\{CLAUDE_PLUGIN_DATA\}")( |$)')
        seen = 0
        for path in (ROOT / "skills").glob("*/SKILL.md"):
            front = path.read_text(encoding="utf-8").split("---", 2)[1]
            for line in front.splitlines():
                if not line.startswith("allowed-tools:"):
                    continue
                for rule in re.findall(r"Bash\(([^()]*)\)", line):
                    seen += 1
                    with self.subTest(skill=path.parent.name, rule=rule):
                        match = anchored.match(rule)
                        self.assertIsNotNone(match, "rule must start at the plugin root")
                        self.assertNotIn("*", rule[:match.end()])
        self.assertGreaterEqual(seen, 8)

    def test_skill_commands_never_interpolate_arguments(self):
        for path in (ROOT / "skills").glob("*/SKILL.md"):
            blocks = re.findall(r"```sh\n(.*?)```", path.read_text(encoding="utf-8"), re.S)
            for block in blocks:
                with self.subTest(skill=path.parent.name):
                    self.assertIsNone(re.search(r"\$(ARGUMENTS|\d)", block))


if __name__ == "__main__":
    unittest.main()
