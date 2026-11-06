"""Experimental native handoff waiting; integration is disabled by default."""
import re
import time


def collect(tasks,tool_ids,read,completed,deadline,clock=time.monotonic,sleep=time.sleep):
    """Return status evidence only, never source output or a fabricated receipt.

    Selection is fixed before waiting. Refresh each task to observe concurrent
    cancellation and validate identity/nonce against the initially owned row.
    The caller supplies a deadline leaving time to emit its normal hook reply.
    """
    pending={};results=[];started=clock()
    for path,row in tasks:
        ident=row.get('tool_use_id');background=row.get('background_id')
        if (ident in tool_ids and not row.get('cancelled') and isinstance(background,str)
                and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',background)):
            pending[ident]=(path,row)
    while pending:
        for ident,(path,original) in list(pending.items()):
            row=read(path)
            status=None;done=None
            if (not isinstance(row,dict) or any(row.get(key)!=original.get(key)
                    for key in ('tool_use_id','background_id','nonce','receipt'))):
                status='ownership-changed'
            elif row.get('cancelled'):
                status='cancelled'
            else:
                done=completed(row)
                if done is not None:status='completed'
            if status is not None:
                results.append(dict(source_tool_use_id=ident,background_id=original['background_id'],
                    status=status,exit_code=done['exit_code'] if done is not None else None,
                    waited_seconds=max(0,clock()-started),kind='native-handoff-collection',
                    completion_known=status=='completed',sleep_removed=False))
                del pending[ident]
        if not pending:break
        left=deadline-clock()
        if left<=0:break
        sleep(min(.05,left))
    for ident,(_,row) in pending.items():
        results.append(dict(source_tool_use_id=ident,background_id=row['background_id'],status='deadline',
            exit_code=None,waited_seconds=max(0,clock()-started),kind='native-handoff-collection',
            completion_known=False,sleep_removed=False))
    return results
