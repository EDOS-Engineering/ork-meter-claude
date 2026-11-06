"""Local telemetry tests: no inference or credentials."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'hooks/lib'))
import hook
import hook_telemetry


class Telemetry(unittest.TestCase):
    def invoke(self, handler, **options):
        with tempfile.TemporaryDirectory() as directory:
            payload=dict(hook_event_name='PostToolUse', tool_name='Bash',session_id='private-session',
                         tool_use_id='call-id', tool_input={'command':'SECRET'},tool_response='PRIVATE OUTPUT')
            output=io.StringIO()
            with patch.dict(os.environ, {'ORK_HOOK_TELEMETRY_DIR':directory}), \
                 patch('sys.stdin',io.StringIO(json.dumps(payload))), patch('sys.stdout',output), \
                 patch('features.gate',side_effect=lambda env:env), patch('policy.bind'), \
                 patch('features.carried',side_effect=lambda env,gated:env), \
                 patch('features.mode',return_value='reduce'), patch('circuit.bind',return_value=False), \
                 patch('circuit.validate_reply'), patch('circuit.fault'), \
                 patch('token_gate.permits_reply',return_value=True):
                hook.run(('PostToolUse',),handler,hook_name='test-hook.py',**options)
            raw=(Path(directory)/'hook-invocations.jsonl').read_text()
            for secret in ('SECRET','PRIVATE OUTPUT','private-session','call-id'):
                self.assertNotIn(secret, raw)
            rows=[json.loads(line) for line in raw.splitlines()]
            self.assertEqual(rows[0]['phase'],'started')
            self.assertEqual(rows[-1]['phase'],'finished')
            self.assertEqual(rows[0]['invocation_id'],rows[-1]['invocation_id'])
            return output.getvalue(),rows[-1]

    def test_emission_is_not_model_delivery_proof(self):
        output,row=self.invoke(lambda ctx:hook.updated_tool_output(ctx.event,'short'))
        self.assertTrue(output)
        self.assertEqual(row['decision'],'applied')
        self.assertTrue(row['stdout_emitted'])
        self.assertIsNone(row['model_delivery_confirmed'])
        self.assertLess(row['bytes_after'],row['bytes_before'])

    def test_skips_and_errors_have_distinct_reasons(self):
        output,row=self.invoke(lambda ctx:None,wanted=lambda env:False)
        self.assertEqual(row['reason'],'not-wanted')
        self.assertEqual(output,'')
        def fail(ctx): raise ValueError('secret exception')
        output,row=self.invoke(fail)
        self.assertEqual(row['reason'],'handler-exception')
        self.assertEqual(row['decision'],'failed')

    def test_effect_without_reply_is_recorded(self):
        def effect(ctx): ctx.telemetry_effect.update(waited_seconds=.3,completion_known=True)
        output,row=self.invoke(effect)
        self.assertEqual(output,'')
        self.assertEqual(row['reason'],'local-effect-without-reply')
        self.assertEqual(row['effects']['waited_seconds'],.3)

    def test_full_store_does_not_change_reply(self):
        with patch.object(hook_telemetry,'MAX_BYTES',0):
            with tempfile.TemporaryDirectory() as directory:
                entry=hook_telemetry.Invocation('test',{},0)
                with patch.dict(os.environ,{'ORK_HOOK_TELEMETRY_DIR':directory}): entry.finish()
                self.assertEqual((Path(directory)/'hook-invocations.jsonl').stat().st_size,0)


if __name__=='__main__': unittest.main()
