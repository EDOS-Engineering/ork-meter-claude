"""Local HTTP loops retain native semantics and become receipt-owned waits."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support
from support import payload, run_hook
import entitled
import polling_http
import polling_receipt
import polling_runtime as runtime
import polling_shell_scan as scanner


def loop(base, interval='0.01'):
    return ('P=owned; while true; do h=$(curl -s '+base+'/history/$P); '
            'if echo "$h" | grep -q \'"completed"\\|"error"\'; then '
            'echo "$h" > history.json; break; fi; sleep '+interval+'; done; '
            'python3 -c \'print("EXACT_OUTPUT")\'')


class Tests(unittest.TestCase):
    def state(self, temporary):
        work = Path(temporary)
        root = work/'state'; root.mkdir()
        polling_receipt.write(root/'deadline.json', dict(origin_monotonic=time.monotonic()))
        return work, root

    def task(self, root, key='one', origin='http://127.0.0.1:8189', done=False):
        row = dict(receipt=str(root/(key+'.done')), nonce='a'*32,
                   paths=[], background_id=key, cancelled=False,
                   tool_use_id=key, poll_origin=origin)
        polling_receipt.write(root/('task-'+key+'.json'), row)
        if done:
            polling_receipt.write(row['receipt'], dict(nonce=row['nonce'], exit_code=0,
                                                       completed_monotonic=time.monotonic()))
        return row

    def test_actual_benchmark_loops_are_admitted_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            work, root = self.state(temporary)
            command = 'cd '+shlex.quote(str(work))+'; '+loop('http://127.0.0.1:8189')
            self.assertIsNotNone(scanner.admission(command, work))
            updated = runtime.launch(payload(work, 'Bash', dict(command=command,
                                     run_in_background=True, timeout=3600000), cwd=str(work)), root)
            self.assertFalse(updated['run_in_background'])
            self.assertEqual(updated['timeout'], 3600000)
            self.assertFalse((work/'history.json').exists())
            self.assertEqual(runtime.tasks(root)[0][1]['poll_origin'], 'http://127.0.0.1:8189')

    def test_native_loop_output_exit_and_receipt_with_real_local_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(.08)
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"completed":true}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                work, root = self.state(temporary)
                base = 'http://127.0.0.1:'+str(server.server_port)
                command = loop(base)+"; python3 -c 'import sys; print(\"EXACT_ERROR\",file=sys.stderr); sys.exit(7)'"
                updated = runtime.launch(payload(work, 'Bash', dict(command=command), cwd=str(work)), root)
                baseline = subprocess.run([scanner.bash_binary(), '-c', command], cwd=work,
                                          capture_output=True, timeout=5)
                expected = (work/'history.json').read_bytes()
                process = subprocess.Popen([scanner.bash_binary(), '-c', updated['command']],
                                           cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                changed, note = runtime.poll(payload(work, 'Bash',
                    dict(command='sleep 540; curl -s '+base+'/queue | head -c 200'), cwd=str(work)), root)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual((process.returncode, stdout, stderr),
                                 (baseline.returncode, baseline.stdout, baseline.stderr))
                self.assertEqual(process.returncode, 7)
                self.assertEqual((work/'history.json').read_bytes(), expected)
                self.assertEqual(changed['command'], 'curl -s '+base+'/queue | head -c 200')
                self.assertIn('completed with exit 7', note)
                self.assertTrue(runtime.read(next(root.glob('wait-*.json')))['sleep_removed'])
        finally:
            server.shutdown(); server.server_close()

    def test_unknown_remote_mutating_and_ambiguous_queue_checks_pass_through(self):
        with tempfile.TemporaryDirectory() as temporary:
            work, root = self.state(temporary)
            for cmd in ('sleep 540; curl -s http://127.0.0.1:8189/queue',
                        'sleep 540; curl -X POST http://127.0.0.1:8189/queue',
                        'sleep 540; curl -s http://example.com:8189/queue'):
                self.assertIsNone(runtime.poll(payload(work, 'Bash', dict(command=cmd), cwd=str(work)), root))
            self.task(root, 'one'); self.task(root, 'two')
            self.assertIsNone(runtime.poll(payload(work, 'Bash', dict(command=
                'sleep 540; curl -s http://127.0.0.1:8190/queue'), cwd=str(work)), root))
            self.assertIsNone(runtime.poll(payload(work, 'Bash', dict(command=
                'sleep 540; curl -s http://127.0.0.1:8189/queue'), cwd=str(work)), root))

    def test_cancellation_is_not_reported_as_http_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            work, root = self.state(temporary)
            row = self.task(root)
            row['cancelled'] = True
            polling_receipt.write(root/'task-one.json', row)
            changed, note = runtime.poll(payload(work, 'Bash', dict(command=
                'sleep 540; curl -s http://127.0.0.1:8189/queue'), cwd=str(work)), root)
            self.assertNotIn('completed with exit', note)
            self.assertIn('unknown after native cancellation', note)

    def test_hook_policy_delivers_loop_rewrite_only_in_reduce_mode(self):
        for mode in ('reduce', 'off', 'shadow'):
            with tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                env = {'ORK_METER_MODE': mode}
                entitled.install(env)
                item = payload(work, 'Bash', dict(command=loop('http://127.0.0.1:8189'),
                    run_in_background=True), cwd=str(work), hook_event_name='UserPromptSubmit')
                run_hook(item, runtime.handle, events=('UserPromptSubmit',), env=env, feature='trim', budget=271)
                item['hook_event_name'] = 'PreToolUse'
                reply = run_hook(item, runtime.handle, events=('PreToolUse',), env=env, feature='trim', budget=271)
                if mode == 'reduce':
                    self.assertFalse(reply['hookSpecificOutput']['updatedInput']['run_in_background'])
                else:
                    self.assertIsNone(reply)

    def test_previous_completed_clip_does_not_hide_current_owned_loop(self):
        with tempfile.TemporaryDirectory() as temporary:
            work, root = self.state(temporary)
            self.task(root, 'previous', done=True)
            self.task(root, 'current')
            polling_receipt.write(root/'deadline.json', dict(origin_monotonic=time.monotonic()-300))
            changed, note = runtime.poll(payload(work, 'Bash', dict(command=
                'sleep 540; curl -s http://127.0.0.1:8189/queue'), cwd=str(work)), root)
            self.assertEqual(changed['command'], 'curl -s http://127.0.0.1:8189/queue')
            self.assertIn('unknown at the request wait deadline', note)
            row = runtime.read(next(root.glob('wait-*.json')))
            self.assertEqual(row['source_tool_use_id'], 'current')
            self.assertFalse(row['completion_known'])

    def test_cd_prefixed_native_background_file_read_matches_owned_loop(self):
        with tempfile.TemporaryDirectory() as temporary:
            work, root = self.state(temporary)
            self.task(root, done=True)
            cmd = 'cd '+shlex.quote(str(work))+'; sleep 540; cat /tmp/tasks/one.output; ls output'
            changed, note = runtime.poll(payload(work, 'Bash', dict(command=cmd), cwd=str(work)), root)
            self.assertEqual(changed['command'], cmd.replace('sleep 540; ', ''))
            self.assertIn('completed with exit 0', note)
            plain = cmd.replace('sleep 540; ', '')
            self.assertIsNotNone(runtime.poll(payload(work, 'Bash', dict(command=plain), cwd=str(work)), root))
            self.assertIsNotNone(runtime.poll(payload(work, 'Bash', dict(command=plain.replace('; ls', '\nls')), cwd=str(work)), root))

    def test_unsupported_loop_shell_controls_still_pass_through(self):
        for suffix in ('; exit 7', '; trap "exit 2" EXIT', '; eval "python3 x"', '; exec python3 x',
                       '; python3 x &', '; source x', '; PATH=/tmp; python3 x'):
            self.assertIsNone(scanner.admission(loop('http://127.0.0.1:8189')+suffix, Path('/tmp')))


if __name__ == '__main__':
    unittest.main()
