import contextlib
import copy
import errno
import io
import hashlib
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
from support import ROOT, clean_environ, context, load  # noqa: E402
import entitled
import artifacts
import features
import hook
import ledger
import policy
import record_handles
import recovery
import run
import session
import token_gate

artifact_script = load('scripts/artifact.py')

compact = load('hooks/compact-records.py')


class RecordHandles(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.raw = json.dumps([{'id': i, 'label': 'private-value-' + 'z' * 180} for i in range(120)])
        self.response = {'content': [{'type': 'text', 'text': self.raw,
                                     'annotations': {'audience': ['user']}}],
                         'isError': False, 'metadata': {'source': 'audit'}}
        self.payload = {'tool_name': 'mcp__audit__list_records', 'tool_response': self.response,
                        'scratchpad_dir': self.temporary.name, 'tool_use_id': 'call-1'}
        self.env = {'CLAUDE_PLUGIN_ROOT': str(ROOT), 'ORK_METER_RECORD_HANDLES': '1'}

    def context(self, payload=None, env=None):
        return context(payload or self.payload, env or self.env, seconds=5)

    def test_switched_on_delivery_preserves_metadata_and_exact_raw_recovery(self):
        ctx = self.context()
        output = compact.handle(ctx)['hookSpecificOutput']['updatedToolOutput']
        text = output['content'][0]['text']
        descriptor = json.loads(text.splitlines()[1])
        self.assertEqual(output['metadata'], self.response['metadata'])
        self.assertEqual(output['content'][0]['annotations'], self.response['content'][0]['annotations'])
        self.assertIs(output['isError'], False)
        self.assertEqual(descriptor['record_count'], 120)
        self.assertFalse(descriptor['values_included'])
        self.assertNotIn('private-value-', text)
        path = Path(descriptor['artifact_path'])
        self.assertEqual(path.read_bytes(), self.raw.encode())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), descriptor['source_sha256'])
        self.assertEqual(artifacts.load(ctx.store(), path.stem)['response'], self.response)
        self.assertEqual(self.payload['tool_response'], self.response)

    def test_received_text_is_stored_once_and_recovers_exactly(self):
        # #143 item 3: the .txt, the response's text block and the document's
        # text held three copies. Now only the .txt holds it.
        ctx = self.context()
        text = compact.handle(ctx)['hookSpecificOutput']['updatedToolOutput']['content'][0]['text']
        path = Path(json.loads(text.splitlines()[1])['artifact_path'])
        stored = path.with_suffix('.json').read_text(encoding='utf-8')
        self.assertNotIn('private-value-', stored)
        on_disk = sum(p.read_text(encoding='utf-8').count('private-value-') for p in path.parent.iterdir())
        self.assertEqual(on_disk, 120)   # each of the 120 records once
        self.assertLess(path.with_suffix('.json').stat().st_size, 1024)
        document = artifacts.load(ctx.store(), path.stem)
        self.assertEqual(document['text'], self.raw)
        self.assertEqual(document['response'], self.response)
        self.assertNotIn('text_stored_in', document)
        for argv in (['--full'], ['--select-json', '{"pointers":["/1"]}'], ['--search', 'private-value-'],
                     ['--start', '1', '--limit', '1']):
            out = io.StringIO()
            buffer = io.BytesIO()
            with contextlib.redirect_stdout(out), patch.object(sys.stdout, 'buffer', buffer, create=True):
                self.assertEqual(artifact_script.main(['--scratchpad', self.temporary.name, '--id', path.stem] + argv), 0)
            result = json.loads(out.getvalue() or buffer.getvalue())
            with self.subTest(argv=argv):
                if argv == ['--full']:
                    self.assertEqual(result, self.response)
                elif argv[0] == '--select-json':
                    self.assertEqual(result['projections'][0]['value'], json.loads(self.raw)[1])
                elif argv[0] == '--search':
                    self.assertEqual(result['total_matches'], 1)
                else:
                    self.assertEqual(result['lines'][0]['text'], self.raw)

    def test_edited_text_fails_the_integrity_check(self):
        ctx = self.context()
        text = compact.handle(ctx)['hookSpecificOutput']['updatedToolOutput']['content'][0]['text']
        path = Path(json.loads(text.splitlines()[1])['artifact_path'])
        path.write_text(self.raw.replace('private-value-', 'edited-value-', 1), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            artifacts.load(ctx.store(), path.stem)

    def test_stream_of_a_list_shaped_response_is_a_clear_error(self):
        # An MCP result can be a bare list of content blocks; --stream used to
        # raise AttributeError on it (a traceback, not the CLI's error).
        store = session.Store.for_scratchpad(self.temporary.name)
        cases = ((list(self.response['content']), 'artifact has no requested stream'),
                 ({'stdout': 'out\n', 'stream_encodings': None}, None))
        for response, error in cases:
            path = Path(artifacts.save(store, 'mcp__audit__list_records', response, self.raw))
            err, out, buffer = io.StringIO(), io.StringIO(), io.BytesIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out), \
                    patch.object(sys.stdout, 'buffer', buffer, create=True):
                try:
                    code = artifact_script.main(['--scratchpad', self.temporary.name, '--id', path.stem,
                                                 '--stream', 'stdout'])
                except SystemExit as stop:
                    code = stop.code
            with self.subTest(response=type(response).__name__):
                if error:
                    self.assertEqual(code, 1)
                    self.assertIn(error, err.getvalue())
                    self.assertNotIn('Traceback', err.getvalue())
                else:
                    self.assertEqual((code, buffer.getvalue()), (0, b'out\n'))

    def test_missing_artifact_gives_a_clear_message(self):
        ctx = self.context()
        text = compact.handle(ctx)['hookSpecificOutput']['updatedToolOutput']['content'][0]['text']
        path = Path(json.loads(text.splitlines()[1])['artifact_path'])
        for victim in (path, path.with_suffix('.json')):   # either half gone
            victim.unlink()
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
                artifact_script.main(['--scratchpad', self.temporary.name, '--id', path.stem])
            self.assertEqual(stop.exception.code, 1)
            self.assertIn('no longer on disk', err.getvalue())
            self.assertIn('Run the tool again', err.getvalue())
            self.assertNotIn('Traceback', err.getvalue())
            compact.handle(ctx)   # save again for the next half

    def test_hinted_recovery_command_selects_records_from_the_saved_artifact(self):
        # #146: the artifact is one line of minified JSON, which Read cuts at
        # 2000 characters; the note must name a command that actually works.
        ctx = self.context()
        text = compact.handle(ctx)['hookSpecificOutput']['updatedToolOutput']['content'][0]['text']
        path = Path(json.loads(text.splitlines()[1])['artifact_path'])
        self.assertEqual(len(path.read_text().splitlines()), 1)
        self.assertGreater(len(path.read_text()), 2000)
        hints = [line for line in text.splitlines() if line.startswith('Select records: ')]
        self.assertEqual(len(hints), 1)
        command = hints[0][len('Select records: '):]
        self.assertTrue(recovery.is_recovery_command(command, {'CLAUDE_PLUGIN_ROOT': str(ROOT)}, '/'))
        env = {key: value for key, value in os.environ.items() if not key.startswith('ORK_METER_')}
        # Run it in the shell Claude Code's Bash tool uses. On Windows that is
        # Git Bash; a bare "bash" resolves to System32's WSL launcher first.
        shell = (env.get('CLAUDE_CODE_GIT_BASH_PATH') or 'C:/Program Files/Git/bin/bash.exe'
                 if os.name == 'nt' else 'bash')
        result = subprocess.run([shell, '-c', command], text=True, env=env, capture_output=True,
                                timeout=30, encoding='utf-8')
        self.assertEqual(result.returncode, 0, (command, result.stdout, result.stderr))
        selection = json.loads(result.stdout)
        self.assertTrue(selection['complete'])
        self.assertEqual([item['value'] for item in selection['projections']], json.loads(self.raw)[:3])

    def test_errors_unsupported_shapes_and_opt_out_pass_through(self):
        variants = []
        for update in ({'isError': True}, {'structuredContent': {'other': 'data'}},
                       {'content': [{'type': 'image', 'data': 'x'}]},
                       {'content': self.response['content'] * 2},
                       {'content': [{'type': 'text', 'text': 'not JSON'}]},
                       {'content': [{'type': 'text', 'text': '[{"id":1}]'}]}):
            payload = copy.deepcopy(self.payload)
            payload['tool_response'].update(update)
            variants.append(payload)
        variants.append(dict(self.payload, tool_name='Bash'))
        variants.append(dict(self.payload, is_error=True))
        for payload in variants:
            with self.subTest(payload_keys=list(payload)):
                self.assertIsNone(compact.handle(self.context(payload)))
        self.assertIsNone(compact.handle(self.context(env=dict(self.env, ORK_METER_RECORD_HANDLES='0'))))

    def test_schema_types_presence_and_strict_json_boundaries(self):
        rows = [{'n': i, 'flag': bool(i % 2), 'missing': None} for i in range(16)]
        rows[0]['optional'] = ['value']
        info = record_handles.inspect(json.dumps(rows))
        self.assertEqual(info['fields']['n'], {'types': ['number'], 'present': 16})
        self.assertEqual(info['fields']['flag']['types'], ['boolean'])
        self.assertEqual(info['fields']['optional']['present'], 1)
        for value in ('[' + ','.join(['{"x":1,"x":2}'] * 16) + ']',
                      '[' + ','.join(['{"x":NaN}'] * 16) + ']', json.dumps([{}] * 16),
                      json.dumps([1] * 16), json.dumps(rows * 400),
                      json.dumps([dict.fromkeys(map(str, range(33)), 1)] * 16),
                      json.dumps([{'a' * 3000: 1}] * 16)):
            self.assertIsNone(record_handles.inspect(value))

    def test_old_policy_digest_retains_off_for_missing_new_switch(self):
        store = session.Store.for_scratchpad(self.temporary.name)
        pin = policy.snapshot(self.env, 'old-session')
        del pin['settings']['ORK_METER_RECORD_HANDLES']
        pin['policy_sha256'] = policy.digest(pin['plugin_version'], pin['settings'])
        session.write_json(store.policy_path, pin)
        effective, saved, _ = policy.resolve(store, self.env, 'old-session')
        self.assertEqual(saved, pin['policy_sha256'])
        self.assertEqual(features.value(effective, 'trim', 'record_handles'), 0)
        self.assertEqual(features.value({}, 'trim', 'record_handles'), 0)
        self.assertIsNone(compact.handle(self.context(env={'CLAUDE_PLUGIN_ROOT': str(ROOT)})))

    def run_hook(self, env, session_id='spellings'):
        """hooks/lib/run.py in process, as py.sh runs it, in a fresh session; returns stdout."""
        scratchpad = tempfile.mkdtemp(dir=self.temporary.name)
        payload = dict(self.payload, hook_event_name='PostToolUse', session_id=session_id, scratchpad_dir=scratchpad)
        base = clean_environ()
        data = entitled.write(str(Path(self.temporary.name) / 'plugin-data'))
        captured = io.StringIO()
        with patch.dict(os.environ, dict(base, CLAUDE_PLUGIN_ROOT=str(ROOT), CLAUDE_PLUGIN_DATA=data, **env), clear=True), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(captured):
            run.main(['run.py', str(ROOT / 'hooks/compact-records.py')])
        self.scratchpad = scratchpad
        return captured.getvalue()

    def test_fresh_session_with_the_variable_unset_is_off(self):
        # #146: the default is off; nothing asserted it through the runtime.
        self.assertEqual(policy.snapshot({}, 'fresh')['settings']['ORK_METER_RECORD_HANDLES'], '0')
        self.assertEqual(self.run_hook({}, 'fresh-unset'), '')
        self.assertEqual(os.listdir(self.scratchpad), [], 'and does no session-store work')

    def test_switch_takes_the_same_spellings_as_other_switches(self):
        # #145: only the literal "1" used to turn it on.
        for spelling in ('1', 'on', 'true', 'yes', 'ON', ' Yes '):
            with self.subTest(spelling=spelling):
                self.assertEqual(features.value({'ORK_METER_RECORD_HANDLES': spelling}, 'trim', 'record_handles'), 1)
                self.assertIn('ork-record-handle-v1', self.run_hook({'ORK_METER_RECORD_HANDLES': spelling}, 'on-' + spelling))
        for spelling in ('0', 'off', 'false', 'no', 'OFF', '', 'junk', '2'):
            with self.subTest(spelling=spelling):
                self.assertEqual(features.value({'ORK_METER_RECORD_HANDLES': spelling}, 'trim', 'record_handles'), 0)
                self.assertEqual(self.run_hook({'ORK_METER_RECORD_HANDLES': spelling}, 'off-' + spelling), '')

    def test_any_off_spelling_stops_a_session_pinned_on(self):
        store = session.Store.for_scratchpad(self.temporary.name)
        session.write_json(store.policy_path, policy.snapshot({'ORK_METER_RECORD_HANDLES': 'yes'}, 'pinned'))
        effective, _, _ = policy.resolve(store, {'ORK_METER_RECORD_HANDLES': 'yes'}, 'pinned')
        self.assertEqual(features.value(effective, 'trim', 'record_handles'), 1)
        for spelling in ('0', 'off', 'false', 'no'):
            with self.subTest(spelling=spelling):
                effective, _, actual = policy.resolve(store, {'ORK_METER_RECORD_HANDLES': spelling}, 'pinned')
                self.assertEqual(features.value(effective, 'trim', 'record_handles'), 0)
                self.assertIsNotNone(actual)

    def test_off_switch_ends_before_policy_entitlement_and_circuit(self):
        # #145: every MCP call runs this hook; with the switch off it must not
        # read or write the session pin, entitlement or reducer health.
        with patch.object(policy, 'bind') as bind, patch.object(features, 'gate') as gate, \
                patch.object(hook.circuit, 'bind') as circuit:
            for env in ({}, {'ORK_METER_RECORD_HANDLES': 'off'}):
                with self.subTest(env=env):
                    self.assertEqual(self.run_hook(env, 'early'), '')
                    self.assertEqual(os.listdir(self.scratchpad), [])
        for step in (bind, gate, circuit):
            step.assert_not_called()

    def test_shadow_mode_still_captures_with_the_switch_off(self):
        with patch.object(hook.trace, 'capture') as capture:
            self.assertEqual(self.run_hook({'ORK_METER_MODE': 'shadow'}, 'shadow'), '')
        capture.assert_called_once()

    def test_artifact_write_failure_passes_through_without_a_fault(self):
        # #145 (#128 pattern): a full disk is not a reducer fault.
        with patch.object(session, 'write_private', side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            self.assertIsNone(compact.handle(self.context()))
            self.assertEqual(self.run_hook({'ORK_METER_RECORD_HANDLES': '1'}, 'disk-full'), '')
        self.assertFalse(any('reducer-health' in str(path) for path in Path(self.scratchpad).rglob('*.json')))

    # --- the artifact is written only for an admitted reply (#143, #130 pattern) ---

    def saved(self):
        directory = Path(self.temporary.name) / 'ork-meter' / session.TOOL_OUTPUT_DIR
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def test_reply_refused_by_the_token_gate_leaves_no_artifact(self):
        ctx = self.context()
        with patch.object(token_gate, 'permits_reply', return_value=False):
            self.assertIsNone(compact.handle(ctx))
        self.assertEqual(self.saved(), [])
        self.assertEqual(ledger.load(ctx.store()), [], 'no saving recorded either')

    def test_reply_past_its_deadline_names_and_writes_nothing(self):
        ctx = hook.Context(self.env, self.payload, 'PostToolUse', time.monotonic() - 1)
        ctx.started = time.monotonic() - 60   # past hook.EMIT_LIMIT too
        with patch.object(artifacts, 'prepare', wraps=artifacts.prepare) as prepare:
            self.assertIsNone(compact.handle(ctx))
        prepare.assert_not_called()
        self.assertEqual(self.saved(), [])

    def test_deadline_passing_during_the_write_removes_the_artifact(self):
        ctx = self.context()
        write = artifacts.Pending.write

        def slow(pending):
            path = write(pending)
            self.assertTrue(os.path.exists(path))
            ctx.deadline = time.monotonic() - 1
            ctx.started = time.monotonic() - 60
            return path
        with patch.object(artifacts.Pending, 'write', slow):
            self.assertIsNone(compact.handle(ctx))
        self.assertEqual(self.saved(), [])

    def test_a_half_written_artifact_is_never_cited_or_left(self):
        real = session.write_private

        def failing(path, text):
            if path.endswith('.json'):
                raise OSError(errno.ENOSPC, 'No space left on device')
            return real(path, text)
        with patch.object(session, 'write_private', failing):
            self.assertIsNone(compact.handle(self.context()))
        self.assertEqual(self.saved(), [])

    def test_an_admitted_reply_cites_a_file_that_is_there(self):
        reply = compact.handle(self.context())
        path = Path(json.loads(reply['hookSpecificOutput']['updatedToolOutput']['content'][0]['text'].splitlines()[1])['artifact_path'])
        self.assertEqual(self.saved(), sorted([path.name, path.stem + '.json']))
        self.assertEqual(path.read_text(encoding='utf-8'), self.raw)

    def test_real_hook_runtime_serializes_replacement_and_opt_out(self):
        payload = dict(self.payload, hook_event_name='PostToolUse', session_id='entrypoint')
        base = clean_environ()
        env = dict(base, CLAUDE_PLUGIN_ROOT=str(ROOT), ORK_METER_MODE='reduce',
                   ORK_METER_RECORD_HANDLES='1',
                   CLAUDE_PLUGIN_DATA=entitled.write(str(Path(self.temporary.name) / 'plugin-data')))
        command = [sys.executable, '-I', '-B', str(ROOT / 'hooks/lib/run.py'),
                   str(ROOT / 'hooks/compact-records.py')]
        result = subprocess.run(command, input=json.dumps(payload), text=True, env=env,
                                capture_output=True, timeout=10, check=True)
        output = json.loads(result.stdout)
        self.assertIn('ork-record-handle-v1', json.dumps(output))
        env['ORK_METER_RECORD_HANDLES'] = '0'
        result = subprocess.run(command, input=json.dumps(payload), text=True, env=env,
                                capture_output=True, timeout=10, check=True)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
