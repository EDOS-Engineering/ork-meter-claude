"""Observed relative tee ownership and conservative shell cwd boundaries."""
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support
import polling_shell_scan as scanner


class Tests(unittest.TestCase):
    def admit(self, command, root, enabled=True):
        with patch.dict(os.environ, ORK_EXPERIMENT_POLLING_WORKSPACE_V1='1',
                        ORK_EXPERIMENT_POLLING_CWD_OUTPUT_V1='1' if enabled else '0'):
            return scanner.admission(command, root)

    def test_observed_literal_same_directory_tee_and_default_off(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve()
            command = 'cd '+shlex.quote(root.as_posix())+'; python3 job.py 2>&1 | tee output.log'
            self.assertEqual(self.admit(command, root, False), [])
            self.assertEqual(self.admit(command, root), [str(root/'output.log')])

    def test_guarded_cd_tracks_tee_and_redirect_in_new_directory(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve(); (root/'sub').mkdir()
            self.assertEqual(self.admit('cd sub && python3 job.py > first.log | tee second.log', root),
                             [str(root/'sub/first.log'), str(root/'sub/second.log')])

    @unittest.skipUnless(os.name == 'nt', 'Native Git Bash drive path normalization')
    def test_windows_git_bash_and_wsl_drive_spellings(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve()
            drive = root.drive[0].lower()
            suffix = root.as_posix()[3:]
            for name in ('/'+drive+'/'+suffix, '/mnt/'+drive+'/'+suffix):
                command = 'cd '+shlex.quote(name)+'; python3 job.py | tee out.log'
                self.assertEqual(self.admit(command, root), [str(root/'out.log')])

    def test_outputs_before_and_after_cd_resolve_at_their_own_command(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve(); (root/'sub').mkdir()
            self.assertEqual(self.admit('python3 prep.py > before.log; cd sub && python3 job.py | tee after.log', root),
                             [str(root/'before.log'), str(root/'sub/after.log')])

    def test_guard_failure_cannot_claim_paths_after_chain_boundary(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve(); (root/'sub').mkdir()
            self.assertEqual(self.admit('cd sub && python3 job.py | tee guarded.log; python3 next.py | tee unknown.log', root),
                             [str(root/'sub/guarded.log')])
            self.assertEqual(self.admit('cd sub && cd .; python3 job.py | tee unknown.log', root), [])

    def test_unguarded_dynamic_missing_pipeline_and_or_cd_abstain(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve(); (root/'sub').mkdir()
            for command in ('cd sub; python3 job.py | tee out.log',
                            'cd "$DIR"; python3 job.py | tee out.log',
                            'cd missing && python3 job.py | tee out.log',
                            'cd sub | tee out.log; python3 job.py',
                            'cd sub || python3 job.py | tee out.log'):
                self.assertEqual(self.admit(command, root), [], command)

    def test_workspace_escape_and_absolute_paths_with_unknown_cwd(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve(); (root/'sub').mkdir()
            self.assertEqual(self.admit('cd .. && python3 job.py | tee out.log', root), [])
            command = 'cd "$DIR"; python3 job.py | tee '+shlex.quote((root/'known.log').as_posix())
            self.assertEqual(self.admit(command, root), [str(root/'known.log')])

    @unittest.skipIf(os.name == 'nt', 'Windows symlink privileges vary')
    def test_symlink_cd_does_not_guess_logical_parent(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve(); (root/'real').mkdir(); (root/'link').symlink_to(root/'real')
            self.assertEqual(self.admit('cd link && python3 job.py | tee out.log', root), [])


if __name__ == '__main__': unittest.main()
