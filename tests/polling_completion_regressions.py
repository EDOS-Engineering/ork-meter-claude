"""Production native completion hooks: semantics, cancellation and policy."""
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support
from support import context, payload, run_hook
import entitled
import polling_receipt
import polling_runtime as runtime
import polling_shell
import polling_shell_scan


class Tests(unittest.TestCase):
    def test_preparation_and_nonzero_exit_preserve_native_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            root = work/'state'; root.mkdir()
            polling_receipt.write(root/'deadline.json', dict(origin_monotonic=time.monotonic()))
            command = ("cat > task.py <<'PY'\nimport sys\nprint('EXACT_OUTPUT')\n"
                       "print('EXACT_ERROR', file=sys.stderr)\nsys.exit(7)\nPY\n"+
                       shlex.quote(sys.executable.replace('\\', '/'))+" task.py > owned.log")
            original = payload(work, 'Bash', dict(command=command, timeout=7200000), cwd=str(work))
            updated = runtime.launch(original, root)
            self.assertTrue(updated['command'].startswith(command+'\n'))
            self.assertEqual(updated['timeout'], 7200000)
            baseline = subprocess.run([polling_shell_scan.bash_binary(), '--noprofile', '--norc', '-c', command],
                                      cwd=work, capture_output=True, timeout=10)
            result = subprocess.run([polling_shell_scan.bash_binary(), '--noprofile', '--norc', '-c', updated['command']],
                                    cwd=work, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(result.stdout, b'')
            self.assertEqual(result.stderr, baseline.stderr)
            self.assertIn(b'EXACT_ERROR', result.stderr)
            self.assertEqual((work/'owned.log').read_text(), 'EXACT_OUTPUT\n')
            row = runtime.tasks(root)[0][1]
            self.assertEqual(runtime.completed(row)['exit_code'], 7)

    def test_parse_only_admission_never_executes_the_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            target = str(work/'SHOULD_NOT_EXIST').replace('\\', '/')
            command = 'python3 -c '+shlex.quote('open('+repr(target)+',"w").write("x")')
            self.assertEqual(polling_shell.admission(command, work), [])
            self.assertFalse((work/'SHOULD_NOT_EXIST').exists())

    def test_shell_control_and_unowned_sleeps_pass_through(self):
        for command in ('python3 x &', 'python3 x | cat', 'python3 x; trap "exit 2" EXIT',
                        'python3 x\nexit 7', 'eval "python3 x"', 'exec python3 x',
                        'source x; python3 x', 'PATH=/tmp; python3 x',
                        'python3 "${x@P}"', 'python3 <(echo data)', 'f() { python3 x; }; f',
                        "cat > x <<PY\n$(trap 'exit 2' EXIT)\nPY\npython3 x"):
            with self.subTest(command=command):
                self.assertIsNone(polling_shell.admission(command, Path('/tmp')))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            polling_receipt.write(root/'deadline.json', dict(origin_monotonic=time.monotonic()))
            self.assertIsNone(runtime.poll(payload(root, 'Bash', {'command':'sleep 540; cat unrelated.log'},
                                                   cwd=str(root)), root))

    def test_concurrent_cancellation_returns_unknown_without_waiting_full_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            polling_receipt.write(root/'deadline.json', dict(origin_monotonic=time.monotonic()))
            row = dict(receipt=str(root/'missing.done'), nonce='a'*32, paths=[str(root/'owned.log')],
                       cancelled=False, tool_use_id='launch', background_id=None)
            polling_receipt.write(root/'task-test.json', row)
            timer = threading.Timer(.1, lambda: polling_receipt.write(root/'task-test.json', dict(row, cancelled=True)))
            timer.start()
            started = time.monotonic()
            try:
                updated, note = runtime.poll(payload(root, 'Bash', {'command':'sleep 540; cat owned.log'},
                                                    cwd=str(root)), root)
            finally:
                timer.join()
            self.assertLess(time.monotonic()-started, 2)
            self.assertEqual(updated['command'], 'cat owned.log')
            self.assertIn('unknown after native cancellation', note)
            self.assertNotIn('completed with exit', note)

    def test_only_native_batch_boundary_renews_the_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            common = payload(work, 'Bash', dict(command='python3 -c "print(1)"'), cwd=str(work))
            now = [10.]
            with patch.object(runtime.time, 'monotonic', lambda: now[0]):
                runtime.handle(context(common, event='UserPromptSubmit'))
                state = next(work.rglob('deadline.json'))
                for index in range(3):
                    now[0] += 1
                    item = dict(common, tool_use_id=str(index))
                    runtime.handle(context(item, event='PreToolUse'))
                    runtime.handle(context(item, event='PostToolUse'))
                    self.assertEqual(runtime.read(state)['origin_monotonic'], 10.)
                now[0] = 30.
                runtime.handle(context(common, event='PostToolBatch'))
                self.assertEqual(runtime.read(state)['origin_monotonic'], 30.)

    def test_signed_trim_policy_and_off_shadow_modes_remain_effective(self):
        for mode in ('off', 'shadow'):
            with tempfile.TemporaryDirectory() as temporary:
                env = {'ORK_METER_MODE':mode}
                entitled.install(env)
                item = payload(Path(temporary), 'Bash', dict(command='python3 task.py'),
                               cwd=temporary, hook_event_name='PreToolUse')
                reply = run_hook(item, runtime.handle, events=('PreToolUse',), env=env, feature='trim', budget=271)
                self.assertIsNone(reply)
                self.assertFalse(list(Path(temporary).rglob('task-*.json')))
        with tempfile.TemporaryDirectory() as temporary:
            item = payload(Path(temporary), 'Bash', dict(command='python3 task.py'),
                           cwd=temporary, hook_event_name='PreToolUse')
            self.assertIsNone(run_hook(item, runtime.handle, events=('PreToolUse',),
                                      env={'CLAUDE_PLUGIN_DATA':temporary}, feature='trim', budget=271))

    def test_corrupt_or_foreign_receipt_never_means_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'receipt.done'
            polling_receipt.write(path, dict(nonce='wrong', exit_code=0, completed_monotonic=time.monotonic()))
            self.assertIsNone(runtime.completed(dict(receipt=str(path), nonce='correct')))

    def test_nested_data_and_preparation_forms_remain_admitted(self):
        for command in (
            'W=/tmp/work; NS=$(python3 -c "print(123)"); python3 run.py "${NS}"',
            'python3 -c \'print("trap; exit; &")\' && false',
            'cd /tmp; python3 run.py > work.log 2>&1',
            "cat > run.py <<'PY'\nprint('literal $(exit 99)')\nPY\npython3 run.py",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(polling_shell.admission(command, Path('/tmp')))

    def test_registry_limit_and_duplicate_native_ids_fail_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            polling_receipt.write(root/'deadline.json', dict(origin_monotonic=time.monotonic()))
            item = payload(root, 'Bash', dict(command='python3 task.py'), cwd=str(root))
            self.assertIsNotNone(runtime.launch(item, root))
            self.assertIsNone(runtime.launch(item, root))
            for index in range(runtime.MAX_TASKS+1):
                polling_receipt.write(root/('task-fixture-'+str(index)+'.json'), {})
            count = len(list(root.glob('task-*.json')))
            self.assertIsNone(runtime.launch(dict(item, tool_use_id='new'), root))
            self.assertEqual(len(list(root.glob('task-*.json'))), count)

    def test_missing_native_timing_never_starts_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            item = payload(work, 'Bash', dict(command='python3 task.py', run_in_background=True), cwd=str(work))
            self.assertIsNone(runtime.handle(context(item, event='PreToolUse')))
            self.assertFalse(list(work.rglob('task-*.json')))


if __name__ == '__main__': unittest.main()
