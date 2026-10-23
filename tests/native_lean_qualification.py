"""The installed CLI sees the lean agent only after /ork-meter:lean install (#108); localhost only.

Before install `claude --agent ork-meter-lean` is refused and the agent's
description is in no prompt; after install the agent's prompt is the session's
system prompt, its description is listed in other sessions of that project (the
documented cost), and `--append-system-prompt-file` keeps the default prompt.
After remove it is refused again. ~/.claude/settings.json must be unchanged.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_host import ROOT, Fixture, Host  # noqa: E402
import host_retry  # noqa: E402
import support  # noqa: E402

AGENT = ROOT / "docs" / "optional-agents" / "lean.md"


def digest(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
class NativeLeanAgent(unittest.TestCase):
    def test_agent_is_available_only_while_installed(self):
        user_settings = Path.home() / ".claude" / "settings.json"
        settings_before = digest(user_settings)
        text = AGENT.read_text(encoding="utf-8")
        description = next(line.split(":", 1)[1].strip() for line in text.split("---", 2)[1].splitlines()
                           if line.startswith("description:"))
        body_line = next(line for line in text.split("---", 2)[2].splitlines() if line.startswith("You are"))
        # No plugin, tools=None and system=None: the stock CLI with its default
        # tools and prompt, where the agent shows up only once installed.
        with Host(self, "ork-native-lean-", None, inference=Fixture) as host:
            base, work = host.base, host.work
            runs = []

            def session(*extra):
                config = base / ("config-%d" % len(runs))
                config.mkdir()
                (config / ".claude.json").write_text('{"hasCompletedOnboarding":true}')
                Fixture.calls, Fixture.requests, Fixture.arrivals = [], [], []
                # Project agents load from the "project" setting source.
                command = host.command("--max-turns", "1", *extra, system=None, plugin=False,
                                       setting_sources="project", restricted=False)
                result = subprocess.run(command, input="Reply OK.", cwd=work, env=host.env(mode=None, CLAUDE_CONFIG_DIR=str(config)),
                                        capture_output=True, text=True, encoding="utf-8", timeout=120)
                runs.append(result)
                request = Fixture.requests[0] if Fixture.requests else None
                return result, request

            def system(request):
                return json.dumps(request.get("system"), ensure_ascii=False)

            def texts(request):
                return "\n".join(block.get("text", "") for block in request["system"])

            def lean(mode):
                return subprocess.run([support.git_bash(), str(ROOT / "hooks" / "py.sh"), "--script", "scripts/lean_agent.py",
                                       "--cwd", str(work), mode], capture_output=True, text=True, timeout=60)

            refused, request = session("--agent", "ork-meter-lean")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("ork-meter-lean' not found", refused.stdout + refused.stderr)
            self.assertIsNone(request, "a refused agent must not reach the model")
            plain, before = session()
            self.assertEqual(plain.returncode, 0, plain.stderr[-1000:])
            self.assertNotIn(description, json.dumps(before, ensure_ascii=False))
            self.assertNotIn(body_line, json.dumps(before, ensure_ascii=False))

            self.assertEqual(lean("--install").returncode, 0)
            self.assertEqual(sorted(os.listdir(work / ".claude")), ["agents"])
            chosen, chosen_request = session("--agent", "ork-meter-lean")
            self.assertEqual(chosen.returncode, 0, chosen.stderr[-1000:])
            self.assertIn(body_line, system(chosen_request))
            plain, listed = session()
            self.assertEqual(plain.returncode, 0)
            self.assertNotIn(body_line, system(listed))
            self.assertIn(description, json.dumps(listed, ensure_ascii=False),
                          "installed: listed as an available agent (the documented cost)")
            appended, request = session("--append-system-prompt-file", ".claude/agents/ork-meter-lean.md")
            self.assertEqual(appended.returncode, 0, appended.stderr[-1000:])
            self.assertIn(body_line, system(request))
            # The identity line differs between modes; the core prompt must be kept whole.
            core = max((block.get("text", "") for block in before["system"]), key=len).strip()
            self.assertGreater(len(core), 1000)
            self.assertIn(core, texts(request), "the default prompt is kept")
            self.assertNotIn(core, texts(chosen_request), "--agent replaces the default prompt")

            self.assertEqual(lean("--remove").returncode, 0)
            refused, request = session("--agent", "ork-meter-lean")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIsNone(request)
            host.assert_no_external_traffic()
        self.assertEqual(digest(user_settings), settings_before, "~/.claude/settings.json must be unchanged")
        print("Lean agent: refused before install and after remove; system prompt replaced with --agent, kept with "
              "--append-system-prompt-file; user settings unchanged.")


if __name__ == "__main__":
    unittest.main()
