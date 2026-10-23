"""Actual default-prompt skill discovery and explicit invocation; localhost only."""
import json
import os
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_host import ROOT, Fixture, Host  # noqa: E402
import entitled  # noqa: E402
import host_retry  # noqa: E402
from skill_visibility_regressions import FORMERLY_LISTED, MODEL_INVOCABLE, listing  # noqa: E402


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
class NativeSkills(unittest.TestCase):
    def test_hidden_skills_have_no_discovery_descriptions_and_still_invoke_explicitly(self):
        # tools=None and system=None: the CLI's default tools and system prompt.
        with Host(self, "ork-native-skills-", None, inference=Fixture) as host:
            base, plugin = host.base, host.plugin
            sources = {path: path.read_text(encoding="utf-8") for path in (plugin / "skills").glob("*/SKILL.md")}
            descriptions = {path.parent.name: next(line.split(":", 1)[1].strip() for line in body.split("---", 2)[1].splitlines()
                                                  if line.startswith("description:")) for path, body in sources.items()}
            def request(mode, prompt):
                # "listed" restores the former exception's listing (the
                # plugin before #282) to measure what that line cost.
                for path, body in sources.items():
                    listed = mode == "visible" or mode == "listed" and path.parent.name == FORMERLY_LISTED
                    path.write_text(re.sub(r"^disable-model-invocation: true\n", "", body, flags=re.MULTILINE)
                                    if listed else body, encoding="utf-8")
                config = base / ("config-" + mode)
                config.mkdir()
                (config / ".claude.json").write_text('{"hasCompletedOnboarding":true}')
                env = host.env(mode="off", CLAUDE_CONFIG_DIR=entitled.claude_config(config))
                if mode == "visible":
                    # Older hosts list names but omit descriptions once the
                    # shared discovery budget is consumed by built-in skills.
                    # Expand only the positive control; hidden/stock retain
                    # their normal prompt settings.
                    env["SLASH_COMMAND_TOOL_CHAR_BUDGET"] = "1000000"
                Fixture.calls, Fixture.requests, Fixture.arrivals = [], [], []
                host.run(prompt, "--max-turns", "1", env=env, system=None, plugin=mode != "stock")
                host.assert_no_external_traffic()
                self.assertEqual(len(Fixture.requests), 1)
                return Fixture.requests[0]
            hidden = request("hidden", "Reply OK.")
            visible = request("visible", "Reply OK.")
            stock = request("stock", "Reply OK.")
            listed = request("listed", "Reply OK.")
            def prompt_text(body):
                return json.dumps({key: body.get(key) for key in ("system", "tools", "messages")}, ensure_ascii=False)
            for name in ("logs", "test-runner"):
                self.assertNotIn("ork-meter:" + name, prompt_text(hidden), "optional agents must not be advertised")
            # ork-meter:explore is also the explore skill's name, so the
            # parked agent is checked by its own description too.
            agent = (ROOT / "docs/optional-agents/explore.md").read_text(encoding="utf-8")
            agent_description = next(line.split(":", 1)[1].strip() for line in agent.split("---", 2)[1].splitlines()
                                     if line.startswith("description:"))
            self.assertNotIn(agent_description, prompt_text(hidden), "optional agents must not be advertised")
            self.assertEqual(hidden["tools"], stock["tools"], "default plugin must add no tool schemas")
            # The host lists a model-invocable skill on one line, joining
            # when_to_use to the description (2.1.302). Since #282 explore's
            # line is gone from every request; the "listed" control shows it
            # and gives its per-request bytes.
            def line_of(name):
                fields = listing(next(body for path, body in sources.items()
                                      if path.parent.name == name).split("---", 2)[1])
                line = "- ork-meter:%s: %s - %s" % (name, fields["description"], fields["when_to_use"])
                return json.dumps(line, ensure_ascii=False)[1:-1]
            self.assertIn(line_of(FORMERLY_LISTED), prompt_text(listed), "control must list " + FORMERLY_LISTED)
            self.assertNotIn(line_of(FORMERLY_LISTED), prompt_text(hidden))
            self.assertEqual(hidden["tools"], listed["tools"])
            # With no skill listed, the default plugin adds nothing to request 1
            # (2.1.302): its system, tools and messages are stock's, byte for byte.
            if not MODEL_INVOCABLE:
                self.assertEqual(prompt_text(hidden), prompt_text(stock), "default plugin must add nothing to request 1")
            sizes = {mode: len(prompt_text(body).encode("utf-8")) for mode, body in
                     (("listed", listed), ("hidden", hidden), ("stock", stock))}
            for name, description in descriptions.items():
                if name in MODEL_INVOCABLE:
                    self.assertIn(description, prompt_text(hidden), "allowlisted skill is listed: " + name)
                    self.assertIn(line_of(name), prompt_text(hidden))
                    self.assertNotIn(description, prompt_text(stock), name)
                    continue
                self.assertNotIn(description, prompt_text(hidden), name)
                self.assertNotIn("ork-meter:" + name, prompt_text(hidden), "hidden skill names must not reach the model")
                self.assertNotIn(description, prompt_text(stock), name)
                self.assertIn(description, prompt_text(visible), "control must prove discoverability for " + name)
                self.assertIn("ork-meter:" + name, prompt_text(visible))
            explicit = request("explicit", "/ork-meter:source units.py")
            self.assertIn("Before editing", prompt_text(explicit))
            self.assertIn("scripts/source.py", prompt_text(explicit))
            self.assertIn("units.py", prompt_text(explicit))
            # The person can still run explore (#282: user-invocable only).
            explicit = request("explicit-explore", "/ork-meter:explore millis")
            self.assertIn("Search the workspace once instead of several Grep and Read round-trips", prompt_text(explicit))
            self.assertIn("scripts/source.py", prompt_text(explicit))
            self.assertIn("millis", prompt_text(explicit))
            print("Request 1 (system, tools, messages): %(listed)d bytes with explore listed, %(hidden)d without "
                  "(stock %(stock)d)." % sizes)
            print("Skill discovery: %d bundled descriptions absent, %d allowlisted present; visible and listed controls "
                  "and explicit source and explore invocation verified."
                  % (len(descriptions) - len(MODEL_INVOCABLE), len(MODEL_INVOCABLE)))


if __name__ == "__main__":
    unittest.main()
