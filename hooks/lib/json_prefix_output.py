"""Experimental existing-hook strategy; disabled unless explicitly isolated."""
import os
import time
import artifacts
import delta
import features
import hook
import ledger
import recovery
import session
import token_gate
import trimmer
from json_prefix_factor import choose

FLAG = 'ORK_EXPERIMENT_JSON_PREFIX_V1'
EVENTS = ('PostToolUse',)
FEATURE = 'trim'
REDUCER = 'trim-tool-output'

def handle(ctx):
    if ctx.event not in EVENTS or ctx.env.get(FLAG) != '1' or not features.enabled(ctx.env,'trim'):
        return None
    p = ctx.payload
    response = p.get('tool_response')
    if (p.get('tool_name')!='Bash' or p.get('is_error') or not isinstance(response,dict)
            or response.get('persistedOutputPath') or response.get('backgroundTaskId')
            or response.get('interrupted') or response.get('isImage')):
        return None
    stdout, stderr = response.get('stdout'),response.get('stderr')
    if not isinstance(stdout,str) or stderr != '' or trimmer.nbytes(stdout)<=features.trim_budget(ctx.env,'Bash'):
        return None
    tool_input = p.get('tool_input')
    if not isinstance(tool_input,dict):
        return None
    command = tool_input.get('command')
    exempt = recovery.exempt_script(command,ctx.env,p.get('cwd'))
    if exempt.script is not None:
        return None
    if exempt.reads and recovery.reads_only_in(exempt.reads,os.path.join(ctx.store().path,session.TOOL_OUTPUT_DIR)):
        return None
    if not hook.may_emit(ctx):
        return None
    deadline = time.monotonic()+min(1.5,max(0,ctx.remaining()-.2))
    view = choose(stdout,token_gate.count,deadline=deadline,allow_factor=False)
    if view is None or not hook.may_emit(ctx):
        return None
    pending = artifacts.prepare(ctx.store(),'Bash',response,stdout)
    after = view.bytes_after
    if after>=view.bytes_before:
        return None
    updated = dict(response,stdout=view.text)
    reply = hook.updated_tool_output(ctx.event,updated)
    if not artifacts.admit(ctx,reply,pending):
        return None
    if features.enabled(ctx.env,'delta') and isinstance(command,str) and command.strip():
        name = delta.key('Bash',p.get('agent_id'),p.get('cwd'),command)
        delta.forget(ctx.store(),name)
    row = ledger.trimmed('Bash',view.bytes_before,after)
    row.update(experimental_strategy='json-prefix-v1',view_kind=view.kind,
               document_count=view.document_count)
    ctx.record(row)
    return reply
