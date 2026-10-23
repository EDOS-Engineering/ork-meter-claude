"""A project's settings may only turn Ork Meter levers off (#143 item 4).

Claude Code merges a project's ``.claude/settings.json`` env into the hook
environment; these tests simulate that merge by putting the same values in
the env the hook sees. Not exercised against real Claude Code.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, clean_environ  # noqa: E402
import entitled  # noqa: E402
entitled.install()
import features  # noqa: E402
import hook  # noqa: E402
import policy  # noqa: E402
import project_env  # noqa: E402


class ProjectSettingsOffOnly(unittest.TestCase):
    def setUp(self):
        project_env._cache.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        (self.repo / ".claude").mkdir(parents=True)
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.env = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.root / "state"),
                    "CLAUDE_PLUGIN_ROOT": str(ROOT), "CLAUDE_PLUGIN_DATA": os.environ["CLAUDE_PLUGIN_DATA"],
                    "CLAUDE_PROJECT_DIR": str(self.repo)}
        self.payload = {"session_id": "project-env-" + self.root.name, "scratchpad_dir": str(self.root / "scratch")}
        managed = patch.object(project_env, "MANAGED_FILES", (str(self.root / "managed-settings.json"),))
        managed.start()
        self.addCleanup(managed.stop)
        home = patch.object(project_env, "_real_home", lambda: str(self.home))
        home.start()
        self.addCleanup(home.stop)

    def settings(self, path, env=None, raw=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw if raw is not None else json.dumps({"env": env}), encoding="utf-8")

    def project(self, env=None, raw=None, name="settings.json"):
        self.settings(self.repo / ".claude" / name, env, raw)

    def user(self, env):
        self.settings(self.home / ".claude" / "settings.json", env)

    def bound(self, live, event="PostToolUse"):
        """The env a hook acts on, with ``live`` merged in as Claude Code would."""
        project_env._cache.clear()
        ctx = hook.Context(dict(self.env, **live), dict(self.payload), event, time.monotonic() + 5)
        policy.bind(ctx)
        return ctx

    def test_project_cannot_enable_delta_or_record_handles(self):
        merged = {"ORK_METER_DELTA": "on", "ORK_METER_RECORD_HANDLES": "1"}
        self.project(merged)
        ctx = self.bound(merged)
        self.assertFalse(features.enabled(ctx.env, "delta"))
        self.assertEqual(features.value(ctx.env, "trim", "record_handles"), 0)
        self.assertEqual(sorted(n for n, _, _ in ctx.ignored_project_settings), sorted(merged))
        self.assertTrue(all(p.endswith(os.path.join(".claude", "settings.json")) for _, _, p in ctx.ignored_project_settings))
        pinned = json.loads(Path(ctx.store().policy_path).read_text())["settings"]
        self.assertEqual((pinned["ORK_METER_DELTA"], pinned["ORK_METER_RECORD_HANDLES"]), ("off", "0"))

    def test_project_can_disable_a_lever_the_user_enabled(self):
        self.user({"ORK_METER_DELTA": "on", "ORK_METER_TRIM": "on"})
        self.project({"ORK_METER_DELTA": "off", "ORK_METER_TRIM": "off"})
        ctx = self.bound({"ORK_METER_DELTA": "off", "ORK_METER_TRIM": "off"})
        self.assertFalse(features.enabled(ctx.env, "delta"))
        self.assertFalse(features.enabled(ctx.env, "trim"))
        self.assertEqual(ctx.ignored_project_settings, ())

    def test_user_setting_wins_when_project_sets_the_same_enabling_value(self):
        self.user({"ORK_METER_DELTA": "on"})
        self.project({"ORK_METER_DELTA": "on"})
        ctx = self.bound({"ORK_METER_DELTA": "on"})
        self.assertTrue(features.enabled(ctx.env, "delta"))
        self.assertEqual(ctx.ignored_project_settings, ())

    def test_managed_settings_are_trusted(self):
        self.settings(self.root / "managed-settings.json", {"ORK_METER_RECORD_HANDLES": "yes"})
        self.project({"ORK_METER_RECORD_HANDLES": "1"})
        env, ignored = project_env.trusted(dict(self.env, ORK_METER_RECORD_HANDLES="1"))
        self.assertEqual(features.value(env, "trim", "record_handles"), 1)
        self.assertEqual(ignored, (), "same effect as the managed value: nothing to report")

    def test_malformed_project_file_ignores_enabling_values_without_crashing(self):
        for raw in ("{not json", "\xff", "[1, 2]", '{"env": [1]}', "x" * (project_env.MAX_BYTES + 1)):
            self.project(raw=raw)
            # A fresh session (and pin) each time.
            self.payload = {"session_id": "malformed-%d" % len(raw), "scratchpad_dir": str(self.root / ("s%d" % len(raw)))}
            ctx = self.bound({"ORK_METER_DELTA": "on", "ORK_METER_DEDUPE": "off"})
            self.assertFalse(features.enabled(ctx.env, "delta"), raw[:20])
            self.assertFalse(features.enabled(ctx.env, "dedupe"), "an off value always stands")
            self.assertTrue(features.enabled(ctx.env, "trim"))
        # A directory where the file should be: unreadable, so enabling values are ignored.
        (self.repo / ".claude" / "settings.json").unlink()
        (self.repo / ".claude" / "settings.json").mkdir()
        self.assertFalse(features.enabled(self.bound({"ORK_METER_DELTA": "on"}).env, "delta"))

    def test_settings_local_json_is_project_level(self):
        self.project({"ORK_METER_CONTEXT_HYGIENE": "on", "ORK_METER_MODE": "shadow"}, name="settings.local.json")
        ctx = self.bound({"ORK_METER_CONTEXT_HYGIENE": "on", "ORK_METER_MODE": "shadow"})
        self.assertFalse(features.enabled(ctx.env, "context_hygiene"))
        self.assertEqual(features.mode(ctx.env), "reduce")
        self.assertTrue(all(p.endswith("settings.local.json") for _, _, p in ctx.ignored_project_settings))

    def test_what_a_project_may_set(self):
        honoured = project_env.honoured
        self.assertTrue(honoured("ORK_METER_MODE", "off") and honoured("ORK_METER_MODE", "junk"))
        self.assertFalse(honoured("ORK_METER_MODE", "reduce") or honoured("ORK_METER_MODE", "shadow"))
        self.assertTrue(honoured("ORK_METER_TRIM", "off") and not honoured("ORK_METER_TRIM", "on"))
        self.assertTrue(honoured("ORK_METER_DELTA", "no") and not honoured("ORK_METER_DELTA", "YES"))
        self.assertTrue(honoured("ORK_METER_RECORD_HANDLES", "0") and not honoured("ORK_METER_RECORD_HANDLES", "true"))
        self.assertTrue(honoured("ORK_METER_LOOP_STOP", "off") and not honoured("ORK_METER_LOOP_STOP", "1"))
        self.assertFalse(honoured("ORK_METER_TRIM_BUDGET_BASH", "1k") or honoured("ORK_METER_TRIM_BUDGET_BASH", "999k"))
        # Every lever setting in the registry is classified.
        for lever in features.LEVERS.values():
            for name in list(lever["opt_outs"]) + [t.env for t in lever["tunables"].values()] + [lever.get("opt_in")]:
                if name:
                    self.assertIn(name, project_env._names())

    def test_project_tuning_is_ignored_for_the_users_value(self):
        self.user({"ORK_METER_TRIM_BUDGET_BASH": "32k"})
        self.project({"ORK_METER_TRIM_BUDGET_BASH": "1k", "ORK_METER_LOOP_STOP": "1"})
        ctx = self.bound({"ORK_METER_TRIM_BUDGET_BASH": "1k", "ORK_METER_LOOP_STOP": "1"})
        self.assertEqual(features.trim_budget(ctx.env, "Bash"), 32 * 1024)
        self.assertIsNone(features.value(ctx.env, "loop_guard", "stop"))

    def test_project_that_is_the_user_config_directory_is_trusted(self):
        self.env["CLAUDE_PROJECT_DIR"] = str(self.home)
        self.user({"ORK_METER_DELTA": "on"})
        self.assertTrue(features.enabled(self.bound({"ORK_METER_DELTA": "on"}).env, "delta"))

    def test_payload_cwd_when_project_dir_is_unset(self):
        del self.env["CLAUDE_PROJECT_DIR"]
        self.payload["cwd"] = str(self.repo)
        self.project({"ORK_METER_DELTA": "on"})
        self.assertFalse(features.enabled(self.bound({"ORK_METER_DELTA": "on"}).env, "delta"))

    def test_session_start_says_once_what_was_ignored(self):
        self.project({"ORK_METER_DELTA": "on"})
        payload = dict(self.payload, hook_event_name="SessionStart", source="startup")
        env = clean_environ()
        env.update(self.env, ORK_METER_DELTA="on")
        result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "hooks/lib/run.py"), str(ROOT / "hooks/trim-session.py")],
                                input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        message = json.loads(result.stdout)["systemMessage"]
        self.assertIn("ignored ORK_METER_DELTA in " + str(self.repo / ".claude" / "settings.json"), message)
        self.assertIn("only turn levers off", message)

    def test_no_project_file_leaves_env_untouched(self):
        env = dict(self.env, ORK_METER_DELTA="on")
        self.assertIs(project_env.trusted(env)[0], env)
        self.assertTrue(features.enabled(self.bound({"ORK_METER_DELTA": "on"}).env, "delta"))

    def test_project_cannot_point_user_settings_at_itself(self):
        for redirect in ({"CLAUDE_CONFIG_DIR": str(self.repo / ".claude")}, {"HOME": str(self.repo)}):
            project_env._cache.clear()
            merged = dict(redirect, ORK_METER_DELTA="on")
            self.project(merged)
            env, ignored = project_env.trusted(dict(self.env, **merged), {"cwd": str(self.repo)})
            self.assertFalse(features.enabled(env, "delta"), redirect)
            self.assertEqual([n for n, _, _ in ignored], ["ORK_METER_DELTA"], redirect)

    def test_redirected_home_without_a_real_home_means_no_user_settings(self):
        self.user({"ORK_METER_DELTA": "on"})
        self.project({"USERPROFILE": str(self.repo), "ORK_METER_DELTA": "on"})
        with patch.object(project_env, "_real_home", lambda: None):
            env, _ = project_env.trusted(dict(self.env, ORK_METER_DELTA="on"), {"cwd": str(self.repo)})
        self.assertFalse(features.enabled(env, "delta"))

    def test_project_dir_from_env_does_not_hide_the_cwd_project(self):
        (self.repo / ".git").mkdir()
        (self.repo / "src" / "deep").mkdir(parents=True)
        self.project({"CLAUDE_PROJECT_DIR": "/", "ORK_METER_DELTA": "on"})
        live = dict(self.env, CLAUDE_PROJECT_DIR="/", ORK_METER_DELTA="on")
        env, ignored = project_env.trusted(live, {"cwd": str(self.repo / "src" / "deep")})
        self.assertFalse(features.enabled(env, "delta"))
        self.assertEqual(ignored[0][2], str(self.repo / ".claude" / "settings.json"))

    def test_windows_names_compare_case_insensitively(self):
        self.project({"ork_meter_delta": "on", "Ork_Meter_Trim": "off"})
        with patch.object(project_env, "_case_insensitive", lambda: True):
            env, ignored = project_env.trusted(dict(self.env, ORK_METER_DELTA="on", ORK_METER_TRIM="off"))
        self.assertFalse(features.enabled(dict(env, ORK_METER_TRIM="on"), "delta"))
        self.assertEqual(env["ORK_METER_TRIM"], "off")
        self.assertEqual([n for n, _, _ in ignored], ["ORK_METER_DELTA"])

    def test_a_default_value_is_not_reported(self):
        self.project({"ORK_METER_TRIM": "on", "ORK_METER_DELTA": "off"})
        env, ignored = project_env.trusted(dict(self.env, ORK_METER_TRIM="on", ORK_METER_DELTA="off"))
        self.assertTrue(features.enabled(env, "trim"))
        self.assertEqual(ignored, ())

    def test_managed_settings_drop_ins_are_trusted(self):
        self.settings(self.root / "managed-settings.d" / "10-ork.json", {"ORK_METER_DELTA": "on"})
        self.project({"ORK_METER_DELTA": "on"})
        env, ignored = project_env.trusted(dict(self.env, ORK_METER_DELTA="on"))
        self.assertTrue(features.enabled(env, "delta"))
        self.assertEqual(ignored, ())

    def test_project_retention_never_reaches_the_global_value(self):
        # The environment's value is the age limit for every project's saved
        # output, so a project's is always dropped from it; a value up to the
        # user's applies to this project's own stores (project_retention),
        # and only a higher or malformed one is reported as ignored.
        name = project_env.RETENTION
        def resolve(project, user=None, local=None):
            project_env._cache.clear()
            self.user({name: user} if user is not None else {})
            self.project({name: project})
            self.project({name: local} if local is not None else {}, name="settings.local.json")
            live = dict(self.env, **{name: local if local is not None else project})
            env, ignored = project_env.trusted(live)
            return env.get(name), [n for n, _, _ in ignored], project_env.project_retention(live)
        self.assertEqual(resolve("1", "5"), ("5", [], 1))
        self.assertEqual(resolve("0", "5"), ("5", [], 0))
        self.assertEqual(resolve("5", "5"), ("5", [], 5))
        self.assertEqual(resolve("30", "5"), ("5", [name], None))
        self.assertEqual(resolve("10"), (None, [name], None), "above the default 7")
        self.assertEqual(resolve("3"), (None, [], 3))
        self.assertEqual(resolve("junk", "3"), ("3", [name], None))
        self.assertEqual(resolve("-1", "3"), ("3", [name], None))
        self.assertEqual(resolve("-1"), (None, [name], None), "reported even where the default is unchanged")
        self.assertEqual(resolve("junk"), (None, [name], None))
        self.assertEqual(resolve("1", "5", local="4"), ("5", [], 4), "local overrides shared")
        self.assertEqual(resolve("1", "5", local="9"), ("5", [name], None))

    def test_bound_env_keeps_the_users_retention_when_the_project_lowers_it(self):
        name = project_env.RETENTION
        self.user({name: "5"})
        self.project({name: "1"})
        ctx = self.bound({name: "1"})
        self.assertEqual(ctx.env.get(name), "5")
        self.assertEqual(ctx.ignored_project_settings, ())
        self.assertEqual(project_env.project_retention(ctx.env, ctx.payload), 1)

    def test_project_retention_without_project_files_is_none(self):
        project_env._cache.clear()
        env = dict(self.env, CLAUDE_PROJECT_DIR=str(self.root / "nowhere"))
        self.assertIsNone(project_env.project_retention(env, {"cwd": str(self.root / "nowhere")}))
        self.assertIsNone(project_env.project_retention(None))

    # --- follow-ups from the #176 review ------------------------------------

    def test_a_nested_claude_directory_never_hides_the_checkouts_own(self):
        # Grakk: a repo ships sub/.claude/, and its root settings set
        # CLAUDE_PROJECT_DIR=/ and DELTA=on. A hook whose cwd is sub stopped
        # at sub/.claude/ and never read the root's file.
        (self.repo / ".git").mkdir()
        (self.repo / "sub" / ".claude").mkdir(parents=True)
        self.settings(self.repo / "sub" / ".claude" / "settings.json", {"ORK_METER_TRIM": "off"})
        self.project({"CLAUDE_PROJECT_DIR": "/", "ORK_METER_DELTA": "on"})
        live = dict(self.env, CLAUDE_PROJECT_DIR="/", ORK_METER_DELTA="on", ORK_METER_TRIM="off")
        env, ignored = project_env.trusted(live, {"cwd": str(self.repo / "sub")})
        self.assertFalse(features.enabled(env, "delta"))
        self.assertFalse(features.enabled(env, "trim"), "the nested off value still stands")
        self.assertEqual([(n, p) for n, _, p in ignored],
                         [("ORK_METER_DELTA", str(self.repo / ".claude" / "settings.json"))])

    def test_the_walk_stops_at_the_checkouts_root(self):
        (self.repo / ".git").mkdir()
        (self.repo / "sub").mkdir()
        above = self.root / ".claude" / "settings.json"
        self.settings(above, {"ORK_METER_DELTA": "on"})
        project_env.trusted(dict(self.env, ORK_METER_DELTA="on"), {"cwd": str(self.repo / "sub")})
        self.assertIn(str(self.repo / ".claude" / "settings.json"), project_env._cache)
        self.assertNotIn(str(above), project_env._cache, "above the .git: another checkout's, or none")

    def test_no_payload_cwd_uses_the_hooks_own_working_directory(self):
        (self.repo / ".git").mkdir()
        self.project({"CLAUDE_PROJECT_DIR": "/", "ORK_METER_DELTA": "on"})
        live = dict(self.env, CLAUDE_PROJECT_DIR="/", ORK_METER_DELTA="on")
        with patch.object(project_env.os, "getcwd", lambda: str(self.repo)):
            env, ignored = project_env.trusted(live, {})
        self.assertFalse(features.enabled(env, "delta"))
        self.assertEqual([n for n, _, _ in ignored], ["ORK_METER_DELTA"])

    def test_no_working_directory_at_all_trusts_no_enabling_value(self):
        # Grakk: no payload cwd, os.getcwd() failing and a distrusted
        # CLAUDE_PROJECT_DIR=/ kept DELTA on. With no project to read, every
        # lever setting is resolved as if a project had set it.
        def gone():
            raise FileNotFoundError("cwd removed")
        live = dict(self.env, CLAUDE_PROJECT_DIR="/", ORK_METER_DELTA="on", ORK_METER_TRIM="off")
        with patch.object(project_env.os, "getcwd", gone):
            env, ignored = project_env.trusted(live, {})
            self.assertFalse(features.enabled(env, "delta"))
            self.assertFalse(features.enabled(env, "trim"), "an off value always stands")
            self.assertEqual([(n, p) for n, _, p in ignored], [("ORK_METER_DELTA", project_env.UNKNOWN_PROJECT)])
            self.assertIn("could not be determined", project_env.notice(ignored))
        # The person's own setting still turns it on.
        project_env._cache.clear()
        self.user({"ORK_METER_DELTA": "on"})
        live.pop("ORK_METER_TRIM")   # delta needs trim on
        with patch.object(project_env.os, "getcwd", gone):
            env, ignored = project_env.trusted(live, {})
            self.assertTrue(features.enabled(env, "delta"))
            self.assertEqual(ignored, ())

    def test_non_text_values_are_untrusted_and_reported(self):
        # Grakk: ["on"] or {"x": 1} were dropped by _text() and never marked
        # untrusted, though Claude Code may still pass ["on"] on as "on".
        self.project({"ORK_METER_DELTA": ["on"], "ORK_METER_RECORD_HANDLES": {"x": 1},
                      "ORK_METER_DEDUPE": None})
        live = dict(self.env, ORK_METER_DELTA="on", ORK_METER_RECORD_HANDLES="[object Object]")
        env, ignored = project_env.trusted(live)
        self.assertFalse(features.enabled(env, "delta"))
        self.assertEqual(features.value(env, "trim", "record_handles"), 0)
        self.assertEqual(sorted(n for n, _, _ in ignored),
                         ["ORK_METER_DEDUPE", "ORK_METER_DELTA", "ORK_METER_RECORD_HANDLES"])
        # From the person's own settings the lever stays on; the project's
        # malformed value is still reported.
        project_env._cache.clear()
        self.user({"ORK_METER_DELTA": "on"})
        env, ignored = project_env.trusted(live)
        self.assertTrue(features.enabled(env, "delta"))
        self.assertIn("ORK_METER_DELTA", [n for n, _, _ in ignored])

    def test_a_non_text_home_variable_distrusts_the_home(self):
        # Claude Code may turn ["/x"] into "/x": user settings then come from
        # the account's real home, not from the one the project points at.
        elsewhere = self.root / "elsewhere"
        self.settings(elsewhere / ".claude" / "settings.json", {"ORK_METER_DELTA": "on"})
        self.project({"HOME": [str(elsewhere)], "ORK_METER_DELTA": "on"})
        env, _ = project_env.trusted(dict(self.env, HOME=str(elsewhere), ORK_METER_DELTA="on"))
        self.assertFalse(features.enabled(env, "delta"))

    # --- follow-ups from the #184 review (#185) -------------------------------

    def test_a_submodules_gitfile_never_hides_the_outer_checkouts_settings(self):
        # Grakk: sub/.git as a file (a submodule) stopped the walk, so with
        # CLAUDE_PROJECT_DIR=/ the outer checkout's DELTA=on stood.
        (self.repo / ".git").mkdir()
        (self.repo / "sub" / "src").mkdir(parents=True)
        (self.repo / "sub" / ".git").write_text("gitdir: ../.git/modules/sub\n")
        self.project({"CLAUDE_PROJECT_DIR": "/", "ORK_METER_DELTA": "on"})
        live = dict(self.env, CLAUDE_PROJECT_DIR="/", ORK_METER_DELTA="on")
        env, ignored = project_env.trusted(live, {"cwd": str(self.repo / "sub" / "src")})
        self.assertFalse(features.enabled(env, "delta"))
        self.assertEqual([(n, p) for n, _, p in ignored],
                         [("ORK_METER_DELTA", str(self.repo / ".claude" / "settings.json"))])

    def test_a_submodule_is_its_own_project_for_retention(self):
        (self.repo / ".git").mkdir()
        sub = self.repo / "sub"
        (sub / "src").mkdir(parents=True)
        (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
        self.project({"ORK_METER_RETENTION_DAYS": "0"})
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_PROJECT_DIR"}
        payload = {"cwd": str(sub / "src")}
        self.assertEqual(project_env.retention_project(env, payload), str(sub))
        self.assertIsNone(project_env.project_retention(env, payload), "the outer checkout's value stays its own")
        self.settings(sub / ".claude" / "settings.json", {"ORK_METER_RETENTION_DAYS": "2"})
        project_env._cache.clear()
        self.assertEqual(project_env.project_retention(env, payload), 2)

    def test_outside_a_checkout_the_walk_stops_at_the_home_directory(self):
        home = self.root / "account"
        (home / "loose" / "dir").mkdir(parents=True)
        own = home / "loose" / ".claude" / "settings.json"
        above = self.root / ".claude" / "settings.json"
        self.settings(own, {"ORK_METER_TRIM": "off"})
        self.settings(above, {"ORK_METER_TRIM": "off"})
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_PROJECT_DIR"}
        with patch.object(project_env, "_real_home", lambda: str(home)):
            project_env.trusted(env, {"cwd": str(home / "loose" / "dir")})
        self.assertIn(str(own), project_env._cache)
        self.assertNotIn(str(above), project_env._cache, "above the account's home directory")

    def test_a_shared_sticky_directory_is_never_read(self):
        shared = self.root / "shared"
        (shared / "me" / "work").mkdir(parents=True)
        try:
            os.chmod(shared, 0o1777)
        except OSError:
            self.skipTest("can't make a sticky directory here")
        self.addCleanup(os.chmod, shared, 0o700)
        if os.name == "nt" or not os.stat(shared).st_mode & 0o1002 == 0o1002:
            self.skipTest("no sticky world-writable directories on this platform")
        planted = shared / ".claude" / "settings.json"
        above = self.root / ".claude" / "settings.json"
        self.settings(planted, {"ORK_METER_DELTA": "off"})
        self.settings(above, {"ORK_METER_DELTA": "off"})
        own = shared / "me" / ".claude" / "settings.json"
        self.settings(own, {"ORK_METER_TRIM": "off"})
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_PROJECT_DIR"}
        project_env.trusted(env, {"cwd": str(shared / "me" / "work")})
        self.assertIn(str(own), project_env._cache)
        self.assertNotIn(str(planted), project_env._cache, "anyone can write there")
        self.assertNotIn(str(above), project_env._cache)
        # The shared directory as the cwd itself is the cwd's own project.
        project_env._cache.clear()
        project_env.trusted(env, {"cwd": str(shared)})
        self.assertIn(str(planted), project_env._cache)

    def test_every_planted_claude_directory_up_to_the_root_applies(self):
        # cwd a/b; settings at the checkout's root and in a/.claude. A walk
        # that stopped at the first parent .claude/ would miss the root's.
        (self.repo / ".git").mkdir()
        (self.repo / "a" / "b").mkdir(parents=True)
        self.settings(self.repo / "a" / ".claude" / "settings.json", {"ORK_METER_RECORD_HANDLES": "1"})
        self.project({"CLAUDE_PROJECT_DIR": "/", "ORK_METER_DELTA": "on"})
        live = dict(self.env, CLAUDE_PROJECT_DIR="/", ORK_METER_DELTA="on", ORK_METER_RECORD_HANDLES="1")
        env, ignored = project_env.trusted(live, {"cwd": str(self.repo / "a" / "b")})
        self.assertFalse(features.enabled(env, "delta"))
        self.assertEqual(features.value(env, "trim", "record_handles"), 0)
        self.assertEqual(sorted((n, p) for n, _, p in ignored),
                         [("ORK_METER_DELTA", str(self.repo / ".claude" / "settings.json")),
                          ("ORK_METER_RECORD_HANDLES", str(self.repo / "a" / ".claude" / "settings.json"))])

    def test_retention_bounds_are_shared_with_retention(self):
        import retention
        self.assertIs(project_env.RETENTION_DEFAULT, features.RETENTION_DEFAULT_DAYS)
        self.assertIs(project_env.RETENTION_CEILING, features.RETENTION_MAX_DAYS)
        self.assertEqual(project_env.RETENTION, features.RETENTION_ENV)
        self.assertEqual((retention.MAX_AGE_DAYS, retention.MAX_RETENTION_DAYS, retention.RETENTION_ENV),
                         (features.RETENTION_DEFAULT_DAYS, features.RETENTION_MAX_DAYS, features.RETENTION_ENV))


if __name__ == "__main__":
    unittest.main()
