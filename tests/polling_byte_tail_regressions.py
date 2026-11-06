"""Bounded owned-file byte polls retain native command and output semantics."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support
import polling_runtime as runtime
import polling_receipt
import polling_shell_scan as scanner


class Tests(unittest.TestCase):
    def parse(self, command, enabled=True):
        with patch.dict(os.environ, ORK_EXPERIMENT_POLLING_TAIL_V1='1' if enabled else '0'):
            return runtime.simple_poll({'tool_name':'Bash','tool_input':{'command':command}})

    def test_observed_and_equivalent_bounded_native_options(self):
        for option in ('-c 600', '-c600', '--bytes 600', '--bytes=600',
                       '-n600', '--lines 600', '--lines=600', '-c 0'):
            command = 'sleep 540; tail '+option+' /tmp/tasks/owned.output'
            self.assertEqual(self.parse(command),
                             ('/tmp/tasks/owned.output', command.split('; ',1)[1], 540.0))
        self.assertIsNone(self.parse('sleep 540; tail -c 600 owned.log', False))

    def test_follow_dynamic_multiple_files_and_unbounded_counts_abstain(self):
        for option in ('-f', '-F', '--follow', '-c +600', '-c -600', '-c 1000001',
                       '-c "$N"', '--bytes=NaN', '-c 600 other.log', '-c 600 -f'):
            self.assertIsNone(self.parse('sleep 540; tail '+option+' owned.log'), option)

    def test_unowned_byte_poll_never_removes_sleep(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            with patch.dict(os.environ, ORK_EXPERIMENT_POLLING_TAIL_V1='1'):
                item = support.payload(root, 'Bash', {'command':'sleep 540; tail -c 600 unowned.log'}, cwd=str(root))
                self.assertIsNone(runtime.poll(item, root))

    def test_owned_receipt_and_exact_native_byte_tail(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve(); state = root/'state'; state.mkdir()
            log = root/'owned.log'; log.write_bytes(bytes(range(256))*5)
            receipt = state/'done.json'
            polling_receipt.write(receipt, {'nonce':'a'*32,'exit_code':7,'completed_monotonic':time.monotonic()})
            polling_receipt.write(state/'task-owned.json', {'nonce':'a'*32,'receipt':str(receipt),
                'paths':[str(log)],'tool_use_id':'producer','background_id':None,'cancelled':False})
            polling_receipt.write(state/'deadline.json', {'origin_monotonic':time.monotonic()})
            original = 'tail -c 600 '+shlex.quote(log.as_posix())
            with patch.dict(os.environ, ORK_EXPERIMENT_POLLING_TAIL_V1='1'):
                changed = runtime.poll(support.payload(root, 'Bash', {'command':'sleep 540; '+original}, cwd=str(root)), state)
            self.assertIsNotNone(changed)
            self.assertEqual(changed[0]['command'], original)
            self.assertIn('completed with exit 7', changed[1])
            actual = subprocess.run([scanner.bash_binary(),'--noprofile','--norc','-c',original],capture_output=True,timeout=10)
            self.assertEqual(actual.returncode, 0)
            self.assertEqual(actual.stdout, log.read_bytes()[-600:])
            self.assertEqual(actual.stderr, b'')
            metadata = runtime.read(state/'task-owned.json')
            metadata['cancelled'] = True
            polling_receipt.write(state/'task-owned.json', metadata)
            with patch.dict(os.environ, ORK_EXPERIMENT_POLLING_TAIL_V1='1'):
                self.assertIsNone(runtime.poll(support.payload(root, 'Bash',
                    {'command':'sleep 540; '+original}, cwd=str(root)), state))


if __name__ == '__main__': unittest.main()
