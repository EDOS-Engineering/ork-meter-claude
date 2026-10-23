"""Deliver a schema and exact local artifact for oversized received MCP record arrays."""
import artifacts
import features
import hook
import ledger
import record_handles
import trimmer

EVENTS = ('PostToolUse',)
FEATURE = 'trim'
REDUCER = 'record-handles'


def wanted(env):
    """Every MCP call runs this hook, so with the switch off (the default) it
    ends before the session pin, entitlement and circuit are read (#145).
    Shadow capture still needs the full runtime."""
    return features.mode(env) == 'shadow' or features.value(env, 'trim', 'record_handles') == 1


def handle(ctx):
    if features.value(ctx.env, 'trim', 'record_handles') != 1:
        return None
    payload = ctx.payload
    tool = payload.get('tool_name')
    response = payload.get('tool_response')
    if (not isinstance(tool, str) or not tool.startswith('mcp__') or payload.get('is_error')
            or isinstance(response, dict) and (response.get('isError') or response.get('structuredContent') is not None)):
        return None
    blocks = response.get('content') if isinstance(response, dict) else response
    if (not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict)
            or blocks[0].get('type') != 'text' or not isinstance(blocks[0].get('text'), str)):
        return None
    text = blocks[0]['text']
    before = trimmer.nbytes(text)
    if before <= features.trim_budget(ctx.env, 'Bash'):
        return None
    info = record_handles.inspect(text)
    if info is None:
        return None
    if not hook.may_emit(ctx):
        return None   # naming hashes the whole result; a late reply is never printed
    # Named first, written only for a reply that will be printed (#130
    # pattern, #143): a refused or late reply leaves nothing on disk.
    pending = artifacts.prepare(ctx.store(), tool, response, text)   # text stored once (#143)
    delivered = record_handles.describe(info, pending.path, before)
    if trimmer.nbytes(delivered) >= before:
        return None
    content = [dict(blocks[0], text=delivered)]
    updated = dict(response, content=content) if isinstance(response, dict) else content
    reply = hook.updated_tool_output(ctx.event, updated)
    if not artifacts.admit(ctx, reply, pending):
        return None
    ctx.record(ledger.trimmed(tool, before, trimmer.nbytes(delivered)))
    return reply
