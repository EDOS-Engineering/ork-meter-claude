"""Native producer pipelines must be collected without changing shell semantics."""
from pathlib import Path
import json,shlex,subprocess,sys,tempfile,time,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import support
from support import payload
import polling_runtime as runtime
import polling_receipt
import polling_shell_scan as scanner

class Tests(unittest.TestCase):
    def test_python_tee_preserves_pipeline_exit_stderr_and_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            work=Path(temporary);state=work/'state';state.mkdir()
            polling_receipt.write(state/'deadline.json',dict(origin_monotonic=time.monotonic()))
            python=shlex.quote(sys.executable.replace('\\','/'))
            command=python+' -c '+shlex.quote('import sys;print("PIPE_STDOUT");print("PIPE_STDERR",file=sys.stderr);sys.exit(7)')+' | tee owned.log'
            item=payload(work,'Bash',dict(command=command,run_in_background=True,timeout=600000),cwd=str(work))
            updated=runtime.launch(item,state)
            self.assertIsNotNone(updated)
            self.assertFalse(updated['run_in_background'])
            baseline=subprocess.run([scanner.bash_binary(),'--noprofile','--norc','-c',command],cwd=work,capture_output=True,timeout=10)
            actual=subprocess.run([scanner.bash_binary(),'--noprofile','--norc','-c',updated['command']],cwd=work,capture_output=True,timeout=10)
            self.assertEqual((actual.returncode,actual.stdout,actual.stderr),(baseline.returncode,baseline.stdout,baseline.stderr))
            self.assertEqual(actual.returncode,0) # Pipeline status is tee's native status, not Python's seven.
            self.assertEqual((work/'owned.log').read_text(),'PIPE_STDOUT\n')
            task=runtime.tasks(state)[0][1]
            self.assertEqual(runtime.completed(task)['exit_code'],baseline.returncode)
            self.assertIn(str((work/'owned.log').resolve()),task['paths'])  # owned paths are resolved (macOS /var -> /private/var)
    def test_background_collection_honors_original_native_execution_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            work=Path(temporary);state=work/'state';state.mkdir()
            polling_receipt.write(state/'deadline.json',dict(origin_monotonic=time.monotonic()-200))
            item=payload(work,'Bash',dict(command='python3 render.py > owned.log',run_in_background=True,timeout=600000),cwd=str(work))
            updated=runtime.launch(item,state)
            self.assertEqual(updated['timeout'],600000)
            self.assertFalse(updated['run_in_background'])
            row=runtime.tasks(state)[0][1]
            self.assertTrue(row['collected_background'])
            self.assertEqual(row['original_timeout_ms'],600000)
    def test_implicit_background_timeout_keeps_available_collection_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            work=Path(temporary);state=work/'state';state.mkdir()
            polling_receipt.write(state/'deadline.json',dict(origin_monotonic=time.monotonic()))
            updated=runtime.launch(payload(work,'Bash',dict(command='python3 render.py',run_in_background=True),cwd=str(work)),state)
            self.assertTrue(200000 < updated['timeout'] < 270000)
    def test_recorded_cd_pipeline_is_admitted_without_running_it(self):
        command='cd /tmp/work; python3 -I /tmp/job.py $PWD 1 2>&1 | tee work/shot_1_run.log'
        self.assertIsNotNone(scanner.admission(command,Path('/tmp/work')))
        self.assertIsNone(scanner.admission('python3 job.py | cat',Path('/tmp')))
        self.assertIsNone(scanner.admission('python3 job.py |& tee out',Path('/tmp')))
        self.assertIsNone(scanner.admission('python3 job.py | tee out &',Path('/tmp')))
    def test_owned_native_background_output_skips_long_sleep_on_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            work=Path(temporary);state=work/'state';state.mkdir()
            polling_receipt.write(state/'deadline.json',dict(origin_monotonic=time.monotonic()))
            receipt=state/'owned.done';nonce='c'*32
            task=dict(receipt=str(receipt),nonce=nonce,paths=[],cancelled=False,tool_use_id='producer',background_id='owned-id')
            polling_receipt.write(state/'task-owned.json',task)
            polling_receipt.write(receipt,dict(nonce=nonce,exit_code=0,completed_monotonic=time.monotonic()))
            item=payload(work,'Bash',dict(command='sleep 590; cat /tmp/tasks/owned-id.output',timeout=600000),cwd=str(work))
            started=time.monotonic();updated,note=runtime.poll(item,state)
            self.assertLess(time.monotonic()-started,1)
            self.assertEqual(updated['command'],'cat /tmp/tasks/owned-id.output')
            self.assertIn('original sleep was not executed',note)
            self.assertIn('completed with exit 0',note)
if __name__=='__main__':unittest.main()
