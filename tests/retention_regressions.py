"""Saved tool-output retention (hooks/lib/retention.py, #143 items 2-3).

Hermetic: the store roots are this test's temporary directory only
(session.SYSTEM_TEMP_BASES and tempfile.tempdir are replaced), so a machine
with live Claude Code sessions keeps its real stores.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402
import hook
import project_env
import retention
import session


summary_hook = load('hooks/session-summary.py')
start_hook = load('hooks/trim-session.py')
artifact_script = load('scripts/artifact.py')
DAY = 86400
MIB = 1024 * 1024



def generous_budget():
    """For a test that checks what one complete pass does, not the time
    bound: a slow CI runner (Windows) can otherwise stop the pass early."""
    return patch.object(retention, "TIME_BUDGET_SECONDS", 30)


# Tests of the time bound itself keep the real budget; every other test
# checks what a pass does, so it runs under generous_budget() (#201).
TIME_BOUND_TESTS = {'test_time_bound_is_respected', 'test_state_at_its_bound_reads_and_writes_quickly',
                    'test_the_state_files_read_and_write_count_against_the_time_budget',
                    'test_caller_deadline_already_passed_does_nothing'}


class Retention(unittest.TestCase):
    def setUp(self):
        if self._testMethodName not in TIME_BOUND_TESTS:
            budget = generous_budget()
            budget.start()
            self.addCleanup(budget.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(session, 'SYSTEM_TEMP_BASES', ())
        patcher.start()
        self.addCleanup(patcher.stop)
        saved, tempfile.tempdir = tempfile.tempdir, str(self.root / 'tmp')
        (self.root / 'tmp').mkdir()
        self.addCleanup(setattr, tempfile, 'tempdir', saved)
        self.env = {'CLAUDE_CODE_TMPDIR': str(self.root), 'XDG_STATE_HOME': str(self.root / 'state'),
                    'CLAUDE_PLUGIN_ROOT': str(ROOT)}
        self.now = time.time()
        self.counter = 0
        # Project settings (hooks/lib/project_env.py) come only from this
        # test's directories: its home, no managed settings.
        self.home = self.root / 'home'
        (self.home / '.claude').mkdir(parents=True)
        self.env['HOME'] = str(self.home)
        for name, value in (('_real_home', lambda: str(self.home)),
                            ('MANAGED_FILES', (str(self.root / 'managed-settings.json'),))):
            patcher = patch.object(project_env, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        project_env._cache.clear()
        self.addCleanup(project_env._cache.clear)

    def scratchpad(self, sid, project='proj'):
        path = self.root / ('claude-%d' % os.getuid() if hasattr(os, 'getuid') else 'claude-0') / project / sid / 'scratchpad'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def mine(self):
        """The current session's tool-output directory (layout-derived roots)."""
        return str(self.tool_output('me'))

    def tool_output(self, sid, project='proj'):
        path = self.scratchpad(sid, project) / 'ork-meter' / 'tool-output'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def artifact(self, sid, age_days=0.0, size=10, pair=True, project='proj'):
        """One saved artifact (.txt and .json) of ``size`` bytes each, ``age_days`` old."""
        self.counter += 1
        stem = '%064x' % self.counter
        directory = self.tool_output(sid, project)
        paths = [directory / (stem + '.txt')] + ([directory / (stem + '.json')] if pair else [])
        when = self.now - age_days * DAY
        for path in paths:
            path.write_bytes(b'x' * size)
            os.utime(path, (when, when))
        return paths

    def context(self, sid, event='SessionEnd', project='proj', env=None, **payload):
        payload = dict(payload, session_id=sid, scratchpad_dir=str(self.scratchpad(sid, project)), hook_event_name=event)
        return hook.Context(dict(self.env if env is None else env), payload, event, time.monotonic() + 1.5)

    def repo(self, name, days=None, user=None):
        """A checkout whose .claude/settings.json sets ORK_METER_RETENTION_DAYS
        to ``days`` (absent when None); ``user`` sets the person's own."""
        repo = self.root / 'repos' / name
        (repo / '.claude').mkdir(parents=True, exist_ok=True)
        block = {} if days is None else {retention.RETENTION_ENV: days}
        (repo / '.claude' / 'settings.json').write_text(json.dumps({'env': block}))
        (self.home / '.claude' / 'settings.json').write_text(
            json.dumps({'env': {} if user is None else {retention.RETENTION_ENV: user}}))
        project_env._cache.clear()
        return str(repo)

    def start(self, sid, project, cwd, env=None):
        """SessionStart of ``sid`` in ``project`` (its store's directory),
        run from checkout ``cwd``. Its tool-output exists (prune only runs
        from a session that has one)."""
        self.tool_output(sid, project)
        retention.at_session_start(self.context(sid, event='SessionStart', project=project, env=env,
                                                cwd=cwd, source='startup'))

    def marker(self, sid, project='proj'):
        return self.tool_output(sid, project).parent / retention.MARKER_NAME

    NONE = {'days': None}

    def pinned(self, sid, project='proj'):
        """The store's pin as JSON, or 'absent'."""
        path = self.marker(sid, project)
        return json.loads(path.read_text()) if path.exists() else 'absent'

    def exists(self, paths):
        return [p.exists() for p in paths]

    # --- age -----------------------------------------------------------------

    def test_artifacts_older_than_the_age_limit_are_deleted_in_pairs(self):
        old = self.artifact('other', age_days=8)
        young = self.artifact('other', age_days=6)
        unrelated = self.tool_output('other') / 'notes.txt'
        unrelated.write_text('not ours to delete')
        os.utime(unrelated, (self.now - 30 * DAY,) * 2)
        with generous_budget():
            stats = retention.prune(self.env, current=self.mine(), now=self.now)
        self.assertEqual(self.exists(old), [False, False])
        self.assertEqual(self.exists(young), [True, True])
        self.assertTrue(unrelated.exists())
        self.assertTrue(stats['pass_complete'])
        self.assertEqual(stats['deleted_files'], 2)

    def test_fallback_layout_stores_are_pruned_too(self):
        directory = Path(tempfile.tempdir) / ('claude-ork-meter-%d' % (os.getuid() if hasattr(os, 'getuid') else 0)) / 'sid' / 'ork-meter' / 'tool-output'
        directory.mkdir(parents=True)
        old = directory / ('%064x.txt' % 999)
        old.write_text('old')
        os.utime(old, (self.now - 9 * DAY,) * 2)
        mine = directory.parent.parent.parent / 'me' / 'ork-meter' / 'tool-output'
        mine.mkdir(parents=True)
        retention.prune(self.env, current=str(mine), now=self.now)
        self.assertFalse(old.exists())

    def test_env_override_sets_the_age_and_parses_like_a_tunable(self):
        three = self.artifact('other', age_days=3)
        retention.prune(dict(self.env, ORK_METER_RETENTION_DAYS='2'), current=self.mine(), now=self.now)
        self.assertEqual(self.exists(three), [False, False])
        for raw, days in (('', 7), ('junk', 7), ('30', 30), ('0', 0), ('99999', retention.MAX_RETENTION_DAYS)):
            self.assertEqual(retention.retention_days({'ORK_METER_RETENTION_DAYS': raw}), days, raw)
        self.assertEqual(retention.retention_days({}), retention.MAX_AGE_DAYS)

    def test_zero_days_never_widens_what_other_sessions_lose(self):
        # 0 (possibly from a project's settings) only ends this session's own
        # files at its end; other sessions keep the default age rule.
        live = self.artifact('other', age_days=0.5)
        recent = self.artifact('crashed', age_days=3)
        old = self.artifact('ancient', age_days=8)
        self.env['ORK_METER_RETENTION_DAYS'] = '0'
        retention.prune(self.env, current=self.mine(), now=self.now)
        retention.at_session_end(self.context('me', reason='prompt_input_exit'))
        retention.at_session_start(self.context('me', event='SessionStart', source='startup'))
        self.assertEqual(self.exists(live) + self.exists(recent), [True] * 4)
        self.assertEqual(self.exists(old), [False, False])

    def test_short_days_never_reach_inside_the_grace(self):
        live = self.artifact('other', age_days=0.5)
        retention.prune(dict(self.env, ORK_METER_RETENTION_DAYS='0k'), current=self.mine(), now=self.now)
        retention.prune(dict(self.env, ORK_METER_RETENTION_DAYS='1'), current=self.mine(), now=self.now,
                        size_cap=0)
        self.assertEqual(self.exists(live), [True, True])

    # --- current session ------------------------------------------------------

    def test_current_session_is_never_pruned_by_age_or_size(self):
        mine = self.artifact('me', age_days=30, size=MIB)
        theirs = self.artifact('other', age_days=2, size=MIB)
        stats = retention.prune(self.env, current=str(self.tool_output('me')), now=self.now, size_cap=MIB)
        self.assertEqual(self.exists(mine), [True, True])
        self.assertEqual(self.exists(theirs), [False, False])   # over the cap, and not mine
        self.assertEqual(stats['estimated_bytes'], 2 * MIB)    # the current session still counts

    @unittest.skipIf(os.name == 'nt', 'symlinks need privileges on Windows')
    def test_current_session_under_another_spelling_is_still_protected(self):
        mine = self.artifact('me', age_days=30)
        real = self.scratchpad('me').parents[2]   # claude-<uid>
        link = self.root / 'other-spelling' / 'claude'
        link.parent.mkdir()
        link.symlink_to(real)
        retention.prune(self.env, current=str(link / 'proj' / 'me' / 'scratchpad' / 'ork-meter' / 'tool-output'),
                        now=self.now)
        self.assertEqual(self.exists(mine), [True, True])

    def test_session_start_hook_prunes_others_and_spares_its_own(self):
        mine = self.artifact('me', age_days=30)
        theirs = self.artifact('other', age_days=30)
        start_hook.handle(self.context('me', event='SessionStart', source='resume'))
        self.assertEqual(self.exists(mine), [True, True])
        self.assertEqual(self.exists(theirs), [False, False])

    # --- size cap -------------------------------------------------------------

    def test_size_cap_deletes_oldest_first_until_under_the_cap(self):
        oldest = self.artifact('a', age_days=5, size=MIB)
        middle = self.artifact('b', age_days=4, size=MIB)
        newest = self.artifact('a', age_days=2, size=MIB)
        stats = retention.prune(self.env, current=self.mine(), now=self.now, size_cap=3 * MIB)   # 6 MiB held
        self.assertEqual(self.exists(oldest), [False, False])
        self.assertEqual(self.exists(middle), [False, False])
        self.assertEqual(self.exists(newest), [True, True])
        self.assertEqual(stats['estimated_bytes'], 2 * MIB)

    def test_size_cap_never_deletes_a_file_modified_within_the_grace(self):
        # Another session running right now: its fresh output survives the
        # cap, which is left exceeded rather than break that session.
        fresh = self.artifact('concurrent', age_days=0.9, size=MIB)
        older = self.artifact('ended', age_days=3, size=MIB)
        stats = retention.prune(self.env, current=self.mine(), now=self.now, size_cap=0)
        self.assertEqual(self.exists(fresh), [True, True])
        self.assertEqual(self.exists(older), [False, False])
        self.assertEqual(stats['estimated_bytes'], 2 * MIB)
        self.assertEqual(retention.LIVE_GRACE_SECONDS, 24 * 3600)

    def test_a_store_outside_the_claude_layout_prunes_nothing_beside_it(self):
        # A test's scratchpad (tests/lib.sh: $tmp/scratchpad) has no siblings,
        # so a hook run there never reaches a real claude-<uid> directory.
        old = self.artifact('other', age_days=30)
        loose = self.root / 'tmp' / 'scratchpad' / 'ork-meter' / 'tool-output'
        loose.mkdir(parents=True)
        stats = retention.prune(self.env, current=str(loose), now=self.now, size_cap=0)
        self.assertEqual(self.exists(old), [True, True])
        self.assertEqual(stats['scanned_dirs'], 0)

    def test_under_the_cap_nothing_young_is_deleted(self):
        young = [self.artifact('a', age_days=d, size=1000) for d in (1, 2, 3)]
        retention.prune(self.env, current=self.mine(), now=self.now)
        self.assertTrue(all(all(self.exists(p)) for p in young))

    # --- SessionEnd -------------------------------------------------------------

    def test_clear_and_logout_delete_the_sessions_own_artifacts(self):
        for reason in ('clear', 'logout'):
            with self.subTest(reason=reason):
                mine = self.artifact('me-' + reason)
                theirs = self.artifact('other-' + reason)
                retention.at_session_end(self.context('me-' + reason, reason=reason))
                self.assertEqual(self.exists(mine), [False, False])
                self.assertEqual(self.exists(theirs), [True, True])

    def test_other_end_reasons_keep_artifacts_for_resume(self):
        for reason in ('prompt_input_exit', 'other', None):
            with self.subTest(reason=reason):
                mine = self.artifact('keep')
                retention.at_session_end(self.context('keep', reason=reason))
                self.assertEqual(self.exists(mine), [True, True])

    def test_zero_days_deletes_at_session_end_whatever_the_reason(self):
        self.env['ORK_METER_RETENTION_DAYS'] = '0'
        mine = self.artifact('me')
        retention.at_session_end(self.context('me', reason='prompt_input_exit'))
        self.assertEqual(self.exists(mine), [False, False])

    def test_session_end_hook_applies_retention_and_still_deletes_delta_bases(self):
        ctx = self.context('me', reason='clear')
        base = Path(ctx.store().delta_base_path('k'))
        base.write_text('base')
        mine = self.artifact('me')
        old = self.artifact('other', age_days=10)
        summary_hook.handle(ctx)
        self.assertFalse(base.exists())
        self.assertEqual(self.exists(mine) + self.exists(old), [False] * 4)
        self.assertTrue(Path(ctx.store().summary_path).exists())

    # --- bounds and failure ------------------------------------------------------

    def test_entry_bound_stops_early_and_a_later_run_finishes(self):
        old = [self.artifact('s%02d' % i, age_days=10) for i in range(20)]
        stats = retention.prune(self.env, current=self.mine(), now=self.now, max_entries=10, size_cap=0)
        self.assertFalse(stats['pass_complete'])
        left = sum(p.exists() for pair in old for p in pair)
        self.assertGreater(left, 0)
        self.assertLessEqual(stats['deleted_files'], 10)
        for _ in range(10):
            retention.prune(self.env, current=self.mine(), now=self.now, max_entries=10)
        self.assertEqual(sum(p.exists() for pair in old for p in pair), 0)

    def on_disk(self):
        return sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file() and '/tool-output/' in p.as_posix())

    def test_size_cap_converges_when_no_run_sees_everything(self):
        # Grakk's case scaled down: 3 sessions x 2000 x 64 KiB with 5000
        # entries a run never finished a scan, so the cap never fired. Here
        # 3 x 200 artifacts (400 files each) against 500 entries a run.
        for sid in ('a', 'b', 'c'):
            for i in range(200):
                self.artifact(sid, age_days=1.5 + (i % 50) / 10, size=512)
        fresh = self.artifact('live', age_days=0.2, size=512)
        cap = 100 * 1024
        self.assertGreater(self.on_disk(), cap * 5)
        first = retention.prune(self.env, current=self.mine(), now=self.now, max_entries=500, size_cap=cap)
        self.assertFalse(first['pass_complete'])
        self.assertGreater(first['deleted_files'], 0)   # the cap fires on a partial scan
        for _ in range(30):
            retention.prune(self.env, current=self.mine(), now=self.now, max_entries=500, size_cap=cap)
        self.assertLessEqual(self.on_disk(), cap)
        self.assertEqual(self.exists(fresh), [True, True])
        text = (self.root / 'state' / 'ork-meter' / retention.STATE_NAME).read_text()
        self.assertEqual(json.loads(text)['version'], retention.STATE_VERSION)
        # Hashed keys only: no store path, project or session name on disk.
        for name in ('proj', 'scratchpad', 'claude-', str(self.root)):
            self.assertNotIn(name, text)

    def state_file(self):
        return self.root / 'state' / 'ork-meter' / retention.STATE_NAME

    def root_key(self):
        return retention._key(self.scratchpad('me').parents[2])

    def write_state(self, entries, pass_=0, cursor=None):
        path = self.state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'version': retention.STATE_VERSION, 'roots': {
            self.root_key(): {'cursor': cursor, 'pass': pass_, 'bytes': entries}}}))

    def test_a_tampered_state_file_cannot_force_deletion(self):
        # Grakk: a huge size from a future pass deleted every other file
        # outside the grace and never aged out of the estimate.
        files = [self.artifact('s%d' % (i % 5), age_days=2, size=100) for i in range(30)]
        real = sum(p.stat().st_size for pair in files for p in pair)
        self.write_state({'f' * 32: [10 ** 12, 10 ** 9]})
        stats = retention.prune(self.env, current=self.mine(), now=self.now, size_cap=10 * 1024)
        self.assertEqual(stats['deleted_files'], 0)
        self.assertEqual(stats['estimated_bytes'], real)
        self.assertTrue(all(all(self.exists(pair)) for pair in files))

    def test_a_tampered_size_is_clamped_and_ages_out(self):
        fresh = [self.artifact('s%d' % i, age_days=0.5, size=100) for i in range(3)]   # inside the grace
        self.write_state({'e' * 32: [10 ** 15, 0]})
        stats = retention.prune(self.env, current=self.mine(), now=self.now, size_cap=10 * 1024)
        self.assertLessEqual(stats['estimated_bytes'], retention.MAX_STORE_BYTES + 10 * 1024)
        for _ in range(3):   # never refreshed during a whole pass: forgotten
            stats = retention.prune(self.env, current=self.mine(), now=self.now, size_cap=10 * 1024)
        self.assertEqual(stats['estimated_bytes'], 600)
        self.assertTrue(all(all(self.exists(pair)) for pair in fresh))

    def test_a_malformed_state_file_is_ignored(self):
        old = self.artifact('other', age_days=2, size=100)
        for content in ('not json', '[]', '{"version": 2, "roots": []}',
                        json.dumps({'version': retention.STATE_VERSION, 'roots': {self.root_key(): {
                            'pass': 'x', 'cursor': ['a', -1], 'bytes': {
                                '../../etc': [10 ** 12, 0], 'a' * 32: ['big', 0], 'b' * 32: [True, 0],
                                'c' * 32: [-5, 0], 'd' * 32: [1, 2, 3], 'e' * 32: {'x': 1}}}}})):
            with self.subTest(content=content[:30]):
                self.state_file().parent.mkdir(parents=True, exist_ok=True)
                self.state_file().write_text(content)
                stats = retention.prune(self.env, current=self.mine(), now=self.now, size_cap=10 * 1024)
                self.assertEqual(stats['deleted_files'], 0)
                self.assertEqual(stats['estimated_bytes'], 200)
        self.assertEqual(self.exists(old), [True, True])

    def test_the_state_file_is_bounded(self):
        # Remembered sizes count only for stores that exist, so the stores
        # are real; the bound is lowered to keep the test quick.
        bound = 20
        stores = [str(self.tool_output('s%02d' % i)) for i in range(bound + 10)]
        for i in range(len(stores)):
            self.artifact('s%02d' % i)
        many = {retention._key(path): [1, 0 if i < 5 else 1] for i, path in enumerate(stores)}
        self.write_state(many, pass_=1)
        with patch.object(retention, 'MAX_STATE_ENTRIES', bound):
            retention.prune(self.env, current=self.mine(), now=self.now, max_entries=1)
        saved = json.loads(self.state_file().read_text())['roots'][self.root_key()]['bytes']
        self.assertEqual(len(saved), bound)
        self.assertEqual(min(v[1] for v in saved.values()), 1)   # the oldest pass went first

    def test_listing_sessions_is_bounded_and_resumes(self):
        old = [self.artifact('s%03d' % i, age_days=10, pair=False) for i in range(300)]
        stats = retention.prune(self.env, current=self.mine(), now=self.now, max_entries=50)
        self.assertFalse(stats['pass_complete'])
        self.assertLess(stats['deleted_files'], 50)
        for _ in range(40):
            if not any(p.exists() for pair in old for p in pair):
                break
            retention.prune(self.env, current=self.mine(), now=self.now, max_entries=50)
        self.assertFalse(any(p.exists() for pair in old for p in pair))

    # --- follow-ups from the #177 review ----------------------------------------

    def test_a_fresh_session_prunes_its_siblings_at_session_start(self):
        # Grakk: SessionStart pruned nothing unless the session's own
        # tool-output/ already existed, so a fresh session never did.
        old = self.artifact('other', age_days=10)
        ctx = self.context('fresh', event='SessionStart', source='startup')
        own = Path(ctx.store().path) / session.TOOL_OUTPUT_DIR
        self.assertFalse(own.exists())
        stats = retention.at_session_start(ctx)
        self.assertEqual(self.exists(old), [False, False])
        self.assertEqual(stats['deleted_files'], 2)
        self.assertFalse(own.exists(), 'no directory is created to prune')

    def test_a_huge_session_of_its_own_cannot_starve_the_rest(self):
        # Grakk: one store with more files than a run examines used up every
        # run before any other store was looked at.
        for _ in range(1500):
            self.artifact('me', age_days=0.1, pair=False)
        old = [self.artifact('s%d' % i, age_days=10) for i in range(5)]
        stats = retention.prune(self.env, current=self.mine(), now=self.now, max_entries=1200)
        self.assertEqual(sum(p.exists() for pair in old for p in pair), 0)
        self.assertEqual(stats['scanned_dirs'], 5)

    def test_a_huge_session_of_its_own_is_sized_in_turns(self):
        for _ in range(2500):
            self.artifact('me', age_days=0.1, size=100, pair=False)
        seen = [retention.prune(self.env, current=self.mine(), now=self.now)['estimated_bytes'] for _ in range(4)]
        self.assertEqual(seen[0], 1000 * 100, 'a fair share of one run')
        self.assertEqual(seen[2:], [2500 * 100] * 2, 'the whole store once it has been swept')

    def test_a_huge_session_is_swept_in_turns_to_its_end(self):
        # Every run started a big store's listing from the top again: once
        # the first entries were all young, its older files were never seen.
        old, young = [], []
        for i in range(230):
            (old if i % 8 == 0 else young).extend(self.artifact('big', age_days=10 if i % 8 == 0 else 0.1, pair=False))
        others = [self.artifact('s%d' % i, age_days=10) for i in range(3)]
        for _ in range(30):
            retention.prune(self.env, current=self.mine(), now=self.now, max_entries=60)
        self.assertFalse(any(p.exists() for p in old))
        self.assertTrue(all(p.exists() for p in young))
        self.assertFalse(any(p.exists() for pair in others for p in pair))

    def test_more_projects_than_a_runs_entries_still_get_past_the_listing(self):
        # Grakk: listing the project directories cost one entry each, so
        # with more projects than MAX_ENTRIES no run ever reached a session.
        old = [self.artifact('s', age_days=10, pair=False, project='p%02d' % i) for i in range(40)]
        for _ in range(40):
            retention.prune(self.env, current=self.mine(), now=self.now, max_entries=20)
        self.assertFalse(any(p.exists() for pair in old for p in pair))

    def test_a_reordered_listing_never_makes_the_cursor_skip_a_store(self):
        # Grakk: the cursor was an offset into an unsorted listing, so a
        # listing that came back in another order skipped stores.
        old = [self.artifact('s%02d' % i, age_days=10, pair=False) for i in range(12)]
        self.mine()
        first = retention.prune(self.env, current=self.mine(), now=self.now, max_entries=10)
        self.assertFalse(first['pass_complete'])
        real = os.scandir

        class Reversed:
            def __init__(self, path):
                with real(path) as entries:
                    self.entries = list(entries)[::-1]

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, *exc):
                return False
        with patch.object(retention.os, 'scandir', Reversed):
            for _ in range(20):
                if retention.prune(self.env, current=self.mine(), now=self.now, max_entries=10)['pass_complete']:
                    break
        self.assertFalse(any(p.exists() for pair in old for p in pair), 'every store visited in one pass')

    def test_the_state_files_read_and_write_count_against_the_time_budget(self):
        # Grakk: the state was read and written outside the 200 ms budget.
        old = self.artifact('other', age_days=10)
        self.write_state({})
        clock = [time.monotonic()]
        load = retention._load_state

        def slow_load(env, root):
            clock[0] += retention.TIME_BUDGET_SECONDS * 0.6   # reading took 60 % of the budget
            return load(env, root)
        with patch.object(retention.time, 'monotonic', lambda: clock[0]), \
                patch.object(retention, '_load_state', slow_load):
            stats = retention.prune(self.env, current=self.mine(), now=self.now)
        self.assertEqual(self.exists(old), [True, True], 'no time left once the write is kept back')
        self.assertFalse(stats['pass_complete'])
        self.assertTrue(self.state_file().exists(), 'the cursor is still saved')

    def test_state_at_its_bound_reads_and_writes_quickly(self):
        many = {'%032x' % i: [10 ** 6, 0] for i in range(retention.MAX_STATE_ENTRIES)}
        self.write_state(many)
        # Best of three: one sample on a busy runner (WSL) once took 0.102 s
        # against the 0.1 s bound. A real slowdown is slow on every attempt.
        elapsed = []
        for _ in range(3):
            t0 = time.monotonic()
            state, known = retention._load_state(self.env, self.root_key())
            session.write_json(str(self.state_file()), state)
            elapsed.append(time.monotonic() - t0)
            self.assertEqual(len(known['bytes']), retention.MAX_STATE_ENTRIES)
        self.assertLess(min(elapsed), retention.TIME_BUDGET_SECONDS / 2, elapsed)

    def test_the_cursor_names_no_directory(self):
        for i in range(6):
            self.artifact('s%d' % i, age_days=10)
        retention.prune(self.env, current=self.mine(), now=self.now, max_entries=4)
        text = self.state_file().read_text()
        cursor = json.loads(text)['roots'][self.root_key()]['cursor']
        self.assertEqual(cursor[0], retention._order('proj'))
        for name in ('proj', 's0', 's1', 's2', 's3'):
            self.assertNotIn('"%s"' % name, text)

    # --- follow-ups from the #184 review (#185) ------------------------------------

    def test_forged_current_pass_entries_never_drive_the_cap(self):
        # Grakk: 100 forged keys stamped with the current pass, clamped to
        # 4 GiB, deleted every other session's file older than the grace
        # (1000 -> 0) before they aged out two passes later.
        for stamp in ('current', 'previous'):
            with self.subTest(stamp=stamp):
                files = [self.artifact('s%d' % (i % 10), age_days=2, size=50) for i in range(500)]
                real = sum(p.stat().st_size for pair in files for p in pair)
                state = json.loads(self.state_file().read_text()) if self.state_file().exists() else {}
                current = state.get('roots', {}).get(self.root_key(), {}).get('pass', 1) or 1
                seen = current if stamp == 'current' else current - 1
                forged = {'%032x' % (0xf00 + i): [retention.MAX_STORE_BYTES, seen] for i in range(100)}
                self.write_state(forged, pass_=current)
                for _ in range(8):   # partial runs, over more than two passes
                    stats = retention.prune(self.env, current=self.mine(), now=self.now,
                                            max_entries=150, size_cap=1024 * 1024)
                    self.assertEqual(stats['deleted_files'], 0)
                    self.assertLessEqual(stats['estimated_bytes'], real * 1.1)
                self.assertTrue(all(all(self.exists(pair)) for pair in files))
                saved = json.loads(self.state_file().read_text())['roots'][self.root_key()]['bytes']
                self.assertFalse(set(forged) & set(saved), 'no store of that key exists: forgotten')
                for pair in files:
                    for path in pair:
                        path.unlink()

    def test_a_scan_out_of_budget_never_returns_half_a_pair(self):
        # #190: a snapshot's text lives only in its .txt, so deleting one
        # half of a pair leaves the other unrecoverable.
        for i in range(3):
            self.artifact('other', age_days=8)
        directory = str(self.tool_output('other'))
        for left in range(0, 7):
            with self.subTest(entries=left):
                budget = retention._Budget(time.monotonic() + 5, left)
                groups, _, complete = retention._scan(directory, budget)
                self.assertEqual(len(groups), left // 2)
                self.assertTrue(all(len(item[2]) == 2 for item in groups.values()))
                self.assertEqual(complete, left >= 6)
        # Time running out between the two halves drops the half examined.
        budget = retention._Budget(time.monotonic() + 5, retention.MAX_ENTRIES)
        take, calls = budget.take, []

        def take_three():
            calls.append(1)
            return len(calls) <= 3 and take()
        budget.take = take_three
        groups, _, complete = retention._scan(directory, budget)
        self.assertFalse(complete)
        self.assertEqual([len(item[2]) for item in groups.values()], [2])

    def test_listed_store_keys_are_the_state_keys(self):
        stores = [str(self.tool_output('s%d' % i, project='p%d' % (i % 2))) for i in range(4)]
        budget = retention._Budget(time.monotonic() + 5, retention.MAX_ENTRIES)
        root, levels = retention._root(self.mine())
        _, listings, complete = retention._list(root, levels, budget)
        self.assertTrue(complete)
        keys = {key for found in listings.values() for _, _, key in found}
        self.assertTrue({retention._key(path) for path in stores + [self.mine()]} <= keys)

    def test_every_spelling_of_a_store_has_one_key_and_stays_protected(self):
        # _key normalises: a relative, dotted or trailing-slash spelling of
        # the current store is still recognised as this session's own.
        mine = self.artifact('me', age_days=30, size=MIB)
        theirs = self.artifact('other', age_days=2, size=MIB)
        real = self.mine()
        spellings = [real + os.sep, os.path.join(real, '..', 'tool-output'),
                     os.path.join(os.path.dirname(real), '.', 'tool-output')]
        try:
            spellings.append(os.path.relpath(real))
        except ValueError:   # Windows: another drive than the working directory
            pass
        for spelling in spellings:
            self.assertEqual(retention._key(spelling), retention._key(real), spelling)
        for spelling in spellings:
            stats = retention.prune(self.env, current=spelling, now=self.now, size_cap=MIB)
            self.assertEqual(self.exists(mine), [True, True], spelling)
            self.assertEqual(stats['estimated_bytes'], 2 * MIB, spelling)   # its own, counted once
        self.assertEqual(self.exists(theirs), [False, False])
        saved = json.loads(self.state_file().read_text())['roots'][self.root_key()]['bytes']
        self.assertEqual(saved[retention._key(real)][0], 2 * MIB, 'one entry for the store, however spelled')

    def test_entries_from_before_the_previous_pass_are_not_counted(self):
        self.artifact('other', age_days=2, size=100)
        key = retention._key(str(self.tool_output('other')))
        self.write_state({key: [retention.MAX_STORE_BYTES, 0]}, pass_=5)
        state, known = retention._load_state(self.env, self.root_key())
        self.assertNotIn(key, known['bytes'], 'measured in neither this pass nor the last')

    def test_past_the_state_bound_the_estimate_is_a_lower_bound(self):
        # 60 stores, room for 20, runs that see ~10: the cap acts on measured bytes only.
        bound, count, size = 20, 60, 1000
        for i in range(count):
            self.artifact('s%02d' % i, age_days=0.5, size=size)
        real = count * 2 * size
        with patch.object(retention, 'MAX_STATE_ENTRIES', bound):
            for _ in range(24):
                stats = retention.prune(self.env, current=self.mine(), now=self.now,
                                        max_entries=30, size_cap=10 ** 12)
                self.assertLessEqual(stats['estimated_bytes'], real)

    def test_a_skewed_sample_never_deletes_under_the_cap(self):
        # Grakk: with room for 50 stores, one 200 MiB store among 300 (real
        # total under 256 MiB) took the extrapolated estimate to 11,070 MiB
        # and the cap wiped the big store. Only measured sizes may delete.
        bound = 50
        big = self.tool_output('big') / ('%064x.txt' % 0xb16)
        with open(big, 'wb') as stream:
            stream.truncate(200 * MIB)   # sparse: the size without the disk
        os.utime(big, (self.now - 2 * DAY, self.now - 2 * DAY))
        for i in range(299):
            self.artifact('s%03d' % i, age_days=2, size=512)
        real = self.on_disk()
        self.assertLess(real, retention.SIZE_CAP_BYTES)
        with patch.object(retention, 'MAX_STATE_ENTRIES', bound):
            for _ in range(40):
                stats = retention.prune(self.env, current=self.mine(), now=self.now, max_entries=150)
                self.assertEqual(stats['deleted_files'], 0)
                self.assertLessEqual(stats['estimated_bytes'], real)
        self.assertTrue(big.exists())

    def test_a_measured_total_over_the_cap_still_deletes_past_the_bound(self):
        bound, count, size = 20, 60, 1000
        old = [self.artifact('s%02d' % i, age_days=2, size=size) for i in range(count)]
        # Measured alone (the 20 remembered stores plus those a run sees)
        # holds more than this cap: that is enough to delete.
        cap = 15 * 2 * size
        with patch.object(retention, 'MAX_STATE_ENTRIES', bound):
            first = retention.prune(self.env, current=self.mine(), now=self.now, max_entries=30, size_cap=cap)
            deleted = first['deleted_files'] + sum(
                retention.prune(self.env, current=self.mine(), now=self.now,
                                max_entries=30, size_cap=cap)['deleted_files'] for _ in range(30))
        self.assertGreater(deleted, 0)
        self.assertLess(self.on_disk(), count * 2 * size)
        self.assertTrue(any(p.exists() for pair in old for p in pair))

    def test_a_state_full_of_forged_keys_never_extrapolates(self):
        files = [self.artifact('s%d' % i, age_days=2, size=100) for i in range(5)]
        real = sum(p.stat().st_size for pair in files for p in pair)
        for i in range(20):   # sessions that saved nothing
            self.scratchpad('empty%02d' % i)
        path = self.state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        forged = {'%032x' % i: [1, 1] for i in range(retention.MAX_STATE_ENTRIES)}
        path.write_text(json.dumps({'version': retention.STATE_VERSION, 'roots': {self.root_key(): {
            'cursor': None, 'pass': 1, 'bytes': forged}}}))
        for _ in range(3):
            stats = retention.prune(self.env, current=self.mine(), now=self.now, size_cap=real - 1)
            self.assertLessEqual(stats['estimated_bytes'], real)
        self.assertEqual(sum(p.exists() for pair in files for p in pair), 2 * len(files) - 2,
                         'only what the real sizes justify: one artifact over the cap')

    def test_a_failed_pin_write_leaves_no_marker_and_still_prunes(self):
        # Grakk: on a full disk stream.write raised out of pin(): that run's
        # prune was skipped, and the empty marker read as "none" for good.
        repo = self.repo('a', days='2')
        old = self.artifact('other', age_days=10)
        real = os.fdopen

        class Full:
            def __init__(self, fd):
                self.fd = fd

            def __enter__(self):
                return self

            def write(self, text):
                raise OSError(28, 'No space left on device')

            def __exit__(self, *exc):
                os.close(self.fd)
                return False

        marker = self.marker('a1', 'pa')

        def fdopen(fd, mode='r', *args, **kwargs):
            # Only the marker's write fails (the disk filled just then).
            if 'w' in mode and marker.exists() and os.path.samestat(os.fstat(fd), os.stat(marker)):
                return Full(fd)
            return real(fd, mode, *args, **kwargs)
        with patch.object(retention.os, 'fdopen', fdopen):
            stats = self.start_stats('a1', 'pa', repo)
        self.assertIsNotNone(stats, 'the prune still ran')
        self.assertEqual(self.exists(old), [False, False])
        self.assertEqual(self.pinned('a1', 'pa'), 'absent', 'no empty marker left behind')
        self.start('a1', 'pa', repo)
        self.assertEqual(self.pinned('a1', 'pa'), {'days': 2}, 'the next start pins')

    def start_stats(self, sid, project, cwd):
        self.tool_output(sid, project)
        return retention.at_session_start(self.context(sid, event='SessionStart', project=project,
                                                       cwd=cwd, source='startup'))

    def test_other_sessions_directories_are_never_removed(self):
        old = self.artifact('other', age_days=10)
        retention.prune(self.env, current=self.mine(), now=self.now)
        self.assertEqual(self.exists(old), [False, False])
        self.assertTrue(self.tool_output('other').is_dir())

    @unittest.skipIf(os.name == 'nt', 'symlinks need privileges on Windows')
    def test_a_linked_level_on_the_way_to_tool_output_is_refused(self):
        elsewhere = self.root / 'elsewhere' / 'ork-meter' / 'tool-output'
        elsewhere.mkdir(parents=True)
        victim = elsewhere / ('%064x.txt' % 77)
        victim.write_text('x')
        os.utime(victim, (self.now - 30 * DAY,) * 2)
        pad = self.scratchpad('linked')
        (pad / 'ork-meter').symlink_to(elsewhere.parent)
        retention.prune(self.env, current=self.mine(), now=self.now, size_cap=0)
        self.assertTrue(victim.exists())

    def test_hook_runs_never_touch_anything_outside_their_temp_root(self):
        # Like tests/lib.sh: the test owns `inside`; `outside` stands in for
        # the real system temp (session.SYSTEM_TEMP_BASES, tempfile), where
        # a developer's real claude-<uid> and fallback stores live.
        inside, outside = self.root / 'inside', self.root / 'outside'
        uid = os.getuid() if hasattr(os, 'getuid') else 0
        decoys = [outside / ('claude-%d' % uid) / 'proj' / 'sid' / 'scratchpad' / 'ork-meter' / 'tool-output',
                  outside / ('claude-ork-meter-%d' % uid) / 'sid' / 'ork-meter' / 'tool-output']
        for directory in decoys:
            directory.mkdir(parents=True)
            for i in range(3):
                f = directory / ('%064x.txt' % i)
                f.write_text('decoy')
                os.utime(f, (self.now - 30 * DAY,) * 2)
        before = sorted((str(p), p.stat().st_mtime) for p in outside.rglob('*'))
        (inside / 'tmpdir').mkdir(parents=True)
        env = {'XDG_STATE_HOME': str(inside / 'state'), 'CLAUDE_PLUGIN_ROOT': str(ROOT),
               'ORK_METER_RETENTION_DAYS': '0'}
        with patch.object(session, 'SYSTEM_TEMP_BASES', (str(outside),)), \
                patch.object(tempfile, 'tempdir', str(inside / 'tmpdir')):
            for scratch in (str(inside / 'scratchpad'), None):
                for event, extra in (('SessionStart', {'source': 'startup'}), ('SessionEnd', {'reason': 'clear'}),
                                     ('SessionEnd', {'reason': 'other'})):
                    payload = dict(extra, session_id='guard', hook_event_name=event)
                    if scratch:
                        payload['scratchpad_dir'] = scratch
                    ctx = hook.Context(dict(env), payload, event, time.monotonic() + 1.5)
                    (start_hook if event == 'SessionStart' else summary_hook).handle(ctx)
        self.assertEqual(sorted((str(p), p.stat().st_mtime) for p in outside.rglob('*')), before)

    def test_time_bound_is_respected(self):
        for i in range(200):
            self.artifact('s%03d' % (i % 40), age_days=10, pair=False)
        clock = iter(range(10**6))
        real = time.monotonic
        start = real()
        # Each monotonic() call advances 10 ms: the 200 ms budget lasts ~20 checks.
        with patch.object(retention.time, 'monotonic', side_effect=lambda: start + next(clock) * 0.01):
            stats = retention.prune(self.env, current=self.mine(), now=self.now)
        self.assertFalse(stats['pass_complete'])
        self.assertLess(stats['deleted_files'], 200)
        # Without a fake clock a real run of a few hundred files is well inside it.
        t0 = real()
        retention.prune(self.env, current=self.mine(), now=self.now)
        self.assertLess(real() - t0, retention.TIME_BUDGET_SECONDS + 0.3)

    def test_caller_deadline_already_passed_does_nothing(self):
        old = self.artifact('other', age_days=10)
        stats = retention.prune(self.env, current=self.mine(), now=self.now, deadline=time.monotonic() - 1)
        self.assertEqual(self.exists(old), [True, True])
        self.assertFalse(stats['pass_complete'])

    def test_errors_fail_open(self):
        old = self.artifact('other', age_days=10)
        with patch.object(retention.os, 'unlink', side_effect=PermissionError('denied')):
            stats = retention.prune(self.env, current=self.mine(), now=self.now)
        self.assertEqual(stats['deleted_files'], 0)
        self.assertEqual(self.exists(old), [True, True])
        with patch.object(retention, 'prune', side_effect=RuntimeError('boom')):
            self.assertIsNone(retention.at_session_start(self.context('me', event='SessionStart')))
            self.assertIsNone(retention.at_session_end(self.context('me', reason='clear')))
            ctx = self.context('me', reason='other')
            summary_hook.handle(ctx)   # the summary is still written
            self.assertTrue(json.loads(Path(ctx.store().summary_path).read_text())['reason'] == 'other')
            reply = start_hook.handle(self.context('me', event='SessionStart', source='startup'))
            self.assertTrue(reply is None or 'systemMessage' in reply)

    # --- a project lowering its own retention ----------------------------------

    def test_a_project_lowering_to_one_day_prunes_only_its_own_stores(self):
        repo = self.repo('a', days='1', user='5')
        sibling = self.artifact('a2', age_days=2, project='pa')
        unmarked = self.artifact('a3', age_days=2, project='pa')   # same project, no marker: user's 5 days
        other = self.artifact('b1', age_days=2, project='pb')
        self.start('a2', 'pa', repo)
        self.assertEqual(json.loads(self.marker('a2', 'pa').read_text()), {'days': 1})
        self.start('a1', 'pa', repo)
        self.assertEqual(self.exists(sibling), [False, False])
        self.assertEqual(self.exists(unmarked) + self.exists(other), [True] * 4)
        self.assertEqual(self.pinned('b1', 'pb'), 'absent')   # never started: nothing pinned

    def test_a_project_above_the_users_value_is_ignored(self):
        repo = self.repo('a', days='10', user='5')
        sibling = self.artifact('a2', age_days=4, project='pa')
        old = self.artifact('a3', age_days=6, project='pa')
        self.start('a2', 'pa', repo)
        self.start('a1', 'pa', repo)
        self.assertEqual(self.pinned('a2', 'pa'), self.NONE)
        self.assertEqual(self.exists(sibling), [True, True])
        env, ignored = project_env.trusted(dict(self.env, ORK_METER_RETENTION_DAYS='10'), {'cwd': repo})
        self.assertEqual(env['ORK_METER_RETENTION_DAYS'], '5')
        self.assertEqual([n for n, _, _ in ignored], ['ORK_METER_RETENTION_DAYS'])
        # The user's 5 days applies everywhere, project 'a' included.
        self.start('a1', 'pa', repo, env=env)
        self.assertEqual(self.exists(old), [False, False])

    def test_project_zero_ends_its_own_files_and_siblings_after_the_grace_only(self):
        repo = self.repo('a', days='0', user='7')
        mine = self.artifact('a1', age_days=0.1, project='pa')
        sibling_fresh = self.artifact('a2', age_days=0.5, project='pa')
        sibling_old = self.artifact('a2', age_days=1.5, project='pa')
        unmarked = self.artifact('a3', age_days=1.5, project='pa')
        other = self.artifact('b1', age_days=1.5, project='pb')
        self.start('a2', 'pa', repo)
        self.assertEqual(json.loads(self.marker('a2', 'pa').read_text()), {'days': 0})
        self.start('a1', 'pa', repo)
        retention.at_session_end(self.context('a1', project='pa', cwd=repo, reason='prompt_input_exit'))
        self.assertEqual(self.exists(mine), [False, False])          # its own, at its own end
        self.assertEqual(self.exists(sibling_old), [False, False])   # a 0-marked store, past the grace
        self.assertEqual(self.exists(sibling_fresh), [True, True])   # within the grace
        self.assertEqual(self.exists(unmarked) + self.exists(other), [True] * 4)

    def test_a_zero_marker_never_shortens_another_store(self):
        # Each store follows only its own marker, whoever's project it is.
        repo_a, repo_b = self.repo('a', days='0'), None
        self.start('a2', 'pa', repo_a)
        same_project = self.artifact('a3', age_days=3, project='pa')
        other_project = self.artifact('b1', age_days=3, project='pb')
        repo_b = self.repo('b')   # no project value
        self.start('b2', 'pb', repo_b)
        retention.prune(self.env, current=str(self.tool_output('b2', 'pb')), now=self.now, size_cap=10 ** 12)
        self.assertEqual(self.exists(same_project) + self.exists(other_project), [True] * 4)
        self.assertEqual(self.pinned('a3', 'pa'), 'absent')
        self.assertEqual(self.pinned('b2', 'pb'), self.NONE)

    def test_a_zero_marker_never_lets_another_pruner_inside_the_grace(self):
        repo = self.repo('a', days='0')
        self.start('a2', 'pa', repo)
        fresh = self.artifact('a2', age_days=0.9, project='pa')
        for current in (('a1', 'pa'), ('b1', 'pb')):
            retention.prune(self.env, current=str(self.tool_output(*current)), now=self.now, size_cap=0)
            retention.at_session_end(self.context(current[0], project=current[1], cwd=repo, reason='clear'))
        self.assertEqual(self.exists(fresh), [True, True])
        # Only its own SessionEnd ends it.
        retention.at_session_end(self.context('a2', project='pa', cwd=repo, reason='prompt_input_exit'))
        self.assertEqual(self.exists(fresh), [False, False])

    def test_a_tampered_marker_never_lengthens_shortens_or_crashes(self):
        outside = self.root / 'outside.json'
        outside.write_text('{"days": 0}')
        bad = {'long': '{"days": 999}', 'text': 'x', 'string': '{"days": "x"}', 'negative': '{"days": -1}',
               'bool': '{"days": false}', 'float': '{"days": 0.5}', 'list': '[0]', 'nul-byte': '\x00',
               'big': '{"days": 0, "pad": "%s"}' % ('x' * 400)}
        kept, gone = [], []
        for sid, text in bad.items():
            self.tool_output(sid)
            self.marker(sid).write_text(text)
            kept.append(self.artifact(sid, age_days=5))     # under the user's 6 days
            gone.append(self.artifact(sid, age_days=6.5))   # past it: garbage never lengthens
        self.tool_output('dir')
        self.marker('dir').mkdir()
        kept.append(self.artifact('dir', age_days=5))
        if os.name != 'nt':
            self.tool_output('link')
            self.marker('link').symlink_to(outside)   # a marker from outside the store is refused
            kept.append(self.artifact('link', age_days=5))
        with generous_budget():
            stats = retention.prune(dict(self.env, ORK_METER_RETENTION_DAYS='6'), current=self.mine(), now=self.now)
        self.assertTrue(stats['pass_complete'])
        self.assertTrue(all(all(self.exists(p)) for p in kept))
        self.assertFalse(any(any(self.exists(p)) for p in gone))
        for text in bad.values():
            self.assertIsNone(retention._marker_days(str(self.write_marker(text))))

    @unittest.skipUnless(hasattr(os, 'getuid'), 'ownership is POSIX only')
    def test_a_marker_another_user_owns_counts_as_none(self):
        store = self.write_marker('{"days": 1}')
        self.assertEqual(retention._marker_days(str(store)), 1)
        with patch.object(os, 'getuid', lambda: os.stat(store).st_uid + 1):
            self.assertIsNone(retention._marker_days(str(store)))

    def write_marker(self, text):
        store = self.root / 'loose-store'
        store.mkdir(exist_ok=True)
        (store / retention.MARKER_NAME).write_text(text)
        return store

    def test_a_valid_marker_is_capped_by_the_users_value(self):
        self.tool_output('m')
        self.marker('m').write_text('{"days": 3}')
        three = self.artifact('m', age_days=3.5)
        retention.prune(self.env, current=self.mine(), now=self.now)
        self.assertEqual(self.exists(three), [False, False])
        self.marker('m').write_text('{"days": 30}')
        six = self.artifact('m', age_days=6.5)
        eight = self.artifact('m', age_days=8)
        retention.prune(self.env, current=self.mine(), now=self.now)
        self.assertEqual(self.exists(six) + self.exists(eight), [True, True, False, False])

    def test_a_pin_of_either_kind_survives_later_starts(self):
        # The first SessionStart decides; a changed setting applies to the
        # project's new sessions (new stores), never to an existing pin.
        repo = self.repo('a', days='2')
        self.start('a1', 'pa', repo)
        self.repo('a', days=None)
        self.start('a1', 'pa', repo)
        self.assertEqual(self.pinned('a1', 'pa'), {'days': 2})
        self.start('a2', 'pa', repo)
        self.assertEqual(self.pinned('a2', 'pa'), self.NONE)
        self.repo('a', days='0')
        for source in ('resume', 'fork', 'clear', 'compact', None):
            ctx = self.context('a2', event='SessionStart', project='pa', cwd=repo, source=source)
            retention.at_session_start(ctx)
        self.assertEqual(self.pinned('a2', 'pa'), self.NONE)
        self.repo('a', days='9')   # above the user's (default 7): pinned as none
        self.start('a3', 'pa', repo)
        self.assertEqual(self.pinned('a3', 'pa'), self.NONE)

    def test_later_starts_from_another_projects_cwd_never_repin(self):
        # Resume, fork or a start with no source, from Q's cwd (Q sets 0),
        # leave P's pin and files alone, with or without $CLAUDE_PROJECT_DIR.
        p, q = self.repo('p'), self.repo('q', days='0')
        for declared in (True, False):
            for source in ('resume', 'fork', None):
                with self.subTest(claude_project_dir=declared, source=source):
                    sid = 'p-%s-%s' % (declared, source)
                    env = dict(self.env, CLAUDE_PROJECT_DIR=p) if declared else dict(self.env)
                    mine = self.artifact(sid, age_days=0.1, project='pp')
                    older = self.artifact(sid, age_days=1.5, project='pp')
                    self.start(sid, 'pp', p, env=env)
                    later = self.context(sid, event='SessionStart', project='pp', env=env, cwd=q,
                                         **({} if source is None else {'source': source}))
                    retention.at_session_start(later)
                    retention.at_session_end(self.context(sid, project='pp', env=env, cwd=q,
                                                          reason='prompt_input_exit'))
                    self.assertEqual(self.pinned(sid, 'pp'), self.NONE)
                    self.assertEqual(self.exists(mine), [True, True])
                    retention.prune(self.env, current=str(self.tool_output('q1', 'pq')), now=self.now)
                    self.assertEqual(self.exists(older), [True, True])

    def test_a_corrupt_pin_is_never_replaced_by_a_lower_one(self):
        repo = self.repo('a', days='0')
        for index, text in enumerate(('x', '{"days": "x"}', '{"days": 999}', '')):
            with self.subTest(pin=text):
                sid = 'c%d' % index
                self.tool_output(sid, 'pa')
                self.marker(sid, 'pa').write_text(text)
                mine = self.artifact(sid, age_days=0.1, project='pa')
                older = self.artifact(sid, age_days=1.5, project='pa')
                self.start(sid, 'pa', repo)
                retention.at_session_end(self.context(sid, project='pa', cwd=repo, reason='other'))
                self.assertEqual(self.marker(sid, 'pa').read_text(), text)
                self.assertEqual(self.exists(mine), [True, True])   # the global value, not 0
                retention.prune(self.env, current=self.mine(), now=self.now)
                self.assertEqual(self.exists(older), [True, True])

    def test_the_project_is_pinned_at_start_never_taken_from_a_moved_cwd(self):
        # A session of P whose cwd moves into Q (which sets 0) neither marks
        # nor ends P's store by Q's value: the project is decided at start.
        p, q = self.repo('p'), self.repo('q', days='0')
        for declared in (True, False):
            with self.subTest(claude_project_dir=declared):
                sid = 'p-%s' % declared
                env = dict(self.env, CLAUDE_PROJECT_DIR=p) if declared else dict(self.env)
                mine = self.artifact(sid, age_days=0.1, project='pp')
                self.start(sid, 'pp', p if not declared else q, env=env)   # declared wins over the cwd
                compact = self.context(sid, event='SessionStart', project='pp', env=env, cwd=q, source='compact')
                retention.at_session_start(compact)
                retention.at_session_end(self.context(sid, project='pp', env=env, cwd=q, reason='prompt_input_exit'))
                self.assertEqual(self.pinned(sid, 'pp'), self.NONE)
                self.assertEqual(self.exists(mine), [True, True])
        # Other pruners leave P's older files alone too.
        older = self.artifact('p-True', age_days=1.5, project='pp')
        retention.prune(self.env, current=str(self.tool_output('q1', 'pq')), now=self.now)
        self.assertEqual(self.exists(older), [True, True])

    def test_a_parent_directorys_claude_settings_are_not_inherited(self):
        parent = Path(self.repo('parent', days='0'))
        child = parent / 'loose' / 'dir'
        child.mkdir(parents=True)
        self.assertIsNone(project_env.project_retention(self.env, {'cwd': str(child)}))
        mine = self.artifact('c1', age_days=0.1, project='pc')
        self.start('c1', 'pc', str(child))
        retention.at_session_end(self.context('c1', project='pc', cwd=str(child), reason='other'))
        self.assertEqual(self.pinned('c1', 'pc'), self.NONE)
        self.assertEqual(self.exists(mine), [True, True])
        # Inside a checkout (its .git), a subdirectory belongs to it.
        (parent / '.git').mkdir()
        project_env._cache.clear()
        self.assertEqual(project_env.project_retention(self.env, {'cwd': str(child)}), 0)

    def test_the_missing_message_shows_the_limit_in_force(self):
        repo = self.repo('a', days='1', user='5')
        store = self.tool_output('m').parent
        raw = dict(os.environ, **self.env, ORK_METER_RETENTION_DAYS='1', CLAUDE_PROJECT_DIR=repo)
        def message():
            err = __import__('io').StringIO()
            with patch.dict(os.environ, raw, clear=True), patch('sys.stderr', err), \
                    self.assertRaises(SystemExit):
                artifact_script.main(['--scratchpad', str(store.parent), '--id', 'ab' * 32])
            return err.getvalue()
        self.assertIn('for 5 days', message())   # not the project's raw 1
        self.marker('m').write_text('{"days": 2}')
        self.assertIn('for 2 days', message())   # this store's own pinned limit
        self.marker('m').write_text('{"days": 99}')
        self.assertIn('for 5 days', message())

    def test_a_project_value_in_the_environment_never_sets_the_global_days(self):
        # Claude Code merges the project's value into the hook environment;
        # the trusted env (hooks/lib/policy.py binds it) carries the user's.
        repo = self.repo('a', days='1', user='5')
        merged = dict(self.env, ORK_METER_RETENTION_DAYS='1', CLAUDE_PROJECT_DIR=repo)
        env, ignored = project_env.trusted(merged, {'cwd': repo})
        self.assertEqual((env['ORK_METER_RETENTION_DAYS'], ignored), ('5', ()))
        self.assertEqual(retention.retention_days(env), 5)
        other = self.artifact('b1', age_days=2, project='pb')
        sibling = self.artifact('a2', age_days=2, project='pa')
        self.start('a2', 'pa', repo, env=env)
        self.start('a1', 'pa', repo, env=env)
        self.assertEqual(self.exists(other), [True, True])
        self.assertEqual(self.exists(sibling), [False, False])

    def test_missing_message_names_the_policy(self):
        text = retention.missing_message('ab' * 32, {'ORK_METER_RETENTION_DAYS': '3'})
        self.assertIn('no longer on disk', text)
        self.assertIn('3 days', text)
        self.assertIn('256 MiB', text)


if __name__ == '__main__':
    unittest.main()
