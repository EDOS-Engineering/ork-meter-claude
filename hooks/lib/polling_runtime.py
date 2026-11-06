"""Completion-aware native Bash collection with native hook request timing.

Only UserPromptSubmit/PostToolBatch establish the next request's conservative
origin. Per-tool hooks never renew that window, including concurrent tools.
No output silence, file size, or process-ID disappearance proves completion.
"""
import hashlib
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
import time

import polling_receipt
import polling_shell
import polling_http
import session

WINDOW = 270
MAX_TASKS = 128


def identity(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def read(path):
    try:
        if path.is_symlink() or path.stat().st_size > 8192:
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def remaining(root):
    value = read(root/'deadline.json') or {}
    origin = value.get('origin_monotonic')
    if type(origin) not in (int, float) or not math.isfinite(origin):
        return 0
    age = time.monotonic()-origin
    return max(0, WINDOW-age-1) if age >= 0 else 0


def tasks(root):
    paths = list(islice(root.glob('task-*.json'), MAX_TASKS+1))
    if len(paths) > MAX_TASKS:
        return []
    found = []
    for path in paths:
        row = read(path)
        if not isinstance(row, dict):
            continue
        receipt, nonce, names = row.get('receipt'), row.get('nonce'), row.get('paths')
        if (not isinstance(receipt, str) or Path(receipt).parent != root or
                not isinstance(nonce, str) or not re.fullmatch('[0-9a-f]{32}', nonce) or
                not isinstance(names, list) or not all(isinstance(name, str) for name in names) or
                not isinstance(row.get('tool_use_id'), str) or type(row.get('cancelled')) is not bool):
            continue
        found.append((path, row))
    return found


def completed(row):
    value = read(Path(row['receipt'])) or {}
    code = value.get('exit_code')
    stamp = value.get('completed_monotonic')
    if (value.get('nonce') == row.get('nonce') and type(code) is int and 0 <= code <= 255 and
            type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= stamp <= time.monotonic()):
        return value
    return None


def launch(payload, root):
    value = payload.get('tool_input', {})
    if payload.get('tool_name') != 'Bash' or len(list(islice(root.glob('task-*.json'), MAX_TASKS))) >= MAX_TASKS:
        return None
    if 'run_in_background' in value and type(value['run_in_background']) is not bool:
        return None
    timeout = value.get('timeout', 120000)
    if type(timeout) is not int or timeout < 1:
        return None
    paths = polling_shell.admission(value.get('command'), Path(payload['cwd']).resolve())
    if paths is None:
        return None
    key = identity(payload['tool_use_id'])
    if (root/('task-'+key+'.json')).exists():
        return None
    receipt = root/(key+'.done')
    nonce = secrets.token_hex(16)
    left = remaining(root)
    task = dict(receipt=str(receipt), nonce=nonce,
        tool_use_id=payload['tool_use_id'], paths=paths, background_id=None, cancelled=False,
        announce_completed=value.get('run_in_background') is True and left >= 1,
        collected_background=value.get('run_in_background') is True and left >= 1,
        original_timeout_ms=timeout)
    origin = polling_http.loop_origin(value['command'])
    if origin is not None:
        task['poll_origin'] = origin
    polling_receipt.write(root/('task-'+key+'.json'), task)
    finish = shlex.join([sys.executable.replace('\\', '/'), '-I',
                        str(Path(polling_receipt.__file__).resolve()).replace('\\', '/'), str(receipt), nonce])
    updated = dict(value, command=value['command']+'\n'+finish+' "$?"')
    if value.get('run_in_background') is True:
        if left >= 1:
            # Native execution is not a blocking hook. Honor the original Bash
            # timeout while collecting its real output, rather than manufacture
            # extra model polls at the hook's separate 270-second wait boundary.
            updated.update(run_in_background=False, timeout=timeout if 'timeout' in value else max(timeout, int(left*1000)))
    else:
        updated.update(run_in_background=False, timeout=max(timeout, int(left*1000)))
    return updated


def simple_poll(payload):
    value = payload.get('tool_input', {})
    if payload.get('tool_name') == 'Read' and isinstance(value.get('file_path'), str):
        return value['file_path'], None, None
    if payload.get('tool_name') != 'Bash' or value.get('run_in_background'):
        return None
    command = value.get('command')
    if not isinstance(command, str):
        return None
    match = re.fullmatch(r'\s*(?:sleep\s+([0-9]+(?:\.[0-9]+)?)([smhd]?)\s*(?:;|&&)\s*)?((?:cat|tail)\s+[^\n]+)\s*', command)
    if not match or any(c in match[3] for c in '$`|;&<>()\\'):
        return None
    try:
        words = shlex.split(match[3])
    except ValueError:
        return None
    if len(words) < 2 or words[-1].startswith('-'):
        return None
    options = words[1:-1]
    if words[0] == 'cat' and options not in ([], ['--']):
        return None
    if words[0] == 'tail':
        supported = (not options or
                     (len(options) == 1 and re.fullmatch(r'-[0-9]+', options[0])) or
                     (len(options) == 2 and options[0] == '-n' and options[1].isdigit()))
        if not supported and os.environ.get('ORK_EXPERIMENT_POLLING_TAIL_V1') == '1':
            from polling_tail_consumer import bounded_tail
            supported = bounded_tail([(word, True) for word in words[:-1]])
        if not supported:
            return None
    seconds = float(match[1])*{'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]] if match[1] else None
    return words[-1], match[3] if match[1] else None, seconds


def poll_input(payload):
    value = payload.get('tool_input', {})
    command = value.get('command')
    if payload.get('tool_name') == 'Bash' and isinstance(command, str):
        prefix = re.match(r'\s*cd\s+(\S+|\"[^\"$`\\]+\"|\x27[^\x27]+\x27)\s*(;|&&)\s*', command)
        if prefix:
            try:
                directory = shlex.split(prefix[1])
            except ValueError:
                return None
            if len(directory) != 1 or any(c in directory[0] for c in '$`*?['):
                return None
            cwd = Path(directory[0])
            if not cwd.is_absolute():
                cwd = Path(payload['cwd'])/cwd
            # A failed cd must not accidentally bind relative reads to a task.
            if not cwd.is_dir():
                return None
            parsed = poll_input(dict(payload, cwd=str(cwd),
                                     tool_input=dict(value, command=command[prefix.end():])))
            if parsed is not None:
                path, updated, seconds = parsed
                path = str(Path(path) if Path(path).is_absolute() else cwd/path)
                return path, command[:prefix.end()]+updated if updated is not None else None, seconds
            return None
    parsed = simple_poll(payload)
    if parsed is not None:
        return parsed
    value = payload.get('tool_input', {})
    command = value.get('command')
    if payload.get('tool_name') != 'Bash' or value.get('run_in_background') or not isinstance(command, str):
        return None
    quote, escaped = None, False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == '\\' and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char in ';\n':
            parsed = simple_poll(dict(payload, tool_input=dict(value, command=command[:index])))
            suffix = command[index+1:].lstrip()
            if parsed is not None and suffix and not suffix.startswith(';'):
                return parsed[0], parsed[1]+command[index:] if parsed[1] is not None else None, parsed[2]
    return None


def poll(payload, root, effects=None):
    http_poll = polling_http.delayed_queue(payload)
    parsed = http_poll or poll_input(payload)
    if parsed is None:
        return None
    name, command, seconds = parsed
    path = Path(name)
    path = str(path if path.is_absolute() else Path(payload['cwd'])/path)
    matches = [(source, row) for source, row in tasks(root) if
               (http_poll is not None and row.get('poll_origin') == name) or
               (http_poll is None and (path in row.get('paths', []) or
               (row.get('background_id') and Path(name).name == row['background_id']+'.output' and
                Path(name).parent.name == 'tasks')))]
    if http_poll is not None and len(matches) > 1:
        # Previous clips on the same renderer must not obscure the one live
        # owned poll. Concurrent outstanding polls remain ambiguous.
        outstanding = [(source, row) for source, row in matches
                       if not row.get('cancelled') and completed(row) is None]
        if len(outstanding) == 1:
            matches = outstanding
    if len(matches) != 1:
        return None
    source, row = matches[0]
    if row.get('cancelled') and os.environ.get('ORK_EXPERIMENT_POLLING_TAIL_V1') == '1':
        return None
    started = time.monotonic()
    done = completed(row)
    while done is None and not row.get('cancelled') and remaining(root) > .2:
        time.sleep(min(.05, remaining(root)))
        row = read(source) or dict(row, cancelled=True)
        done = completed(row)
    note = ('Polling collector: owned native task completed with exit '+str(done['exit_code'])+'.'
            if done else 'Polling collector: completion and exit remain unknown'+
            (' after native cancellation.' if row.get('cancelled') else ' at the request wait deadline.'))
    updated = None
    if command is not None:
        updated = dict(payload['tool_input'], command=command)
        note += (' Replaced the requested '+str(seconds)+' second polling sleep with a '+
                 format(time.monotonic()-started, '.3f')+' second completion/deadline wait; '
                 'the original sleep was not executed.')
    polling_receipt.write(root/('wait-'+identity(payload['tool_use_id'])+'.json'),
        dict(source_tool_use_id=row['tool_use_id'], waited_seconds=time.monotonic()-started,
             sleep_removed=command is not None, requested_sleep_seconds=seconds,
             completion_known=done is not None, cancelled=bool(row.get('cancelled'))))
    if effects is not None:
        effects.update(waited_seconds=round(time.monotonic()-started, 6),
                       sleep_removed=command is not None, completion_known=done is not None,
                       background_completion_collected=done is not None,
                       cancelled=bool(row.get('cancelled')))
    return updated, note+' Native read output and exit status are unchanged.'


def handle(ctx):
    payload = ctx.payload
    sid = payload.get('session_id')
    if not isinstance(sid, str) or not sid:
        return None
    root = Path(session.fs_path(ctx.store().path))/'polling'/identity(payload.get('transcript_path', sid))
    session.private_dir(str(root))
    event = ctx.event
    if event in ('UserPromptSubmit', 'PostToolBatch'):
        polling_receipt.write(root/'deadline.json', dict(origin_monotonic=time.monotonic(), boundary=event))
        notes = []
        if event == 'PostToolBatch':
            calls = payload.get('tool_calls')
            current_ids = set()
            reported_ids = set()
            for call in calls if isinstance(calls, list) else []:
                tool_id = call.get('tool_use_id') if isinstance(call, dict) else None
                if not isinstance(tool_id, str) or not tool_id:
                    continue
                current_ids.add(tool_id)
                delivery = root/('delivery-'+identity(tool_id)+'.json')
                note = read(delivery)
                if note is not None and isinstance(note.get('text'), str):
                    notes.append(note['text'])
                    reported_ids.add(tool_id)
                    delivery.unlink(missing_ok=True)
            if ctx.env.get('ORK_EXPERIMENT_POLLING_HANDOFF_V1') == '1':
                from polling_handoff import collect
                deadline = time.monotonic()+max(0, min(WINDOW-1, ctx.remaining()-1))
                collected = collect(tasks(root), current_ids-reported_ids, read, completed,
                                    deadline, clock=time.monotonic, sleep=time.sleep)
                for row in collected:
                    polling_receipt.write(root/('wait-handoff-'+identity(row['source_tool_use_id'])+'.json'), row)
                    if row['status'] == 'completed':
                        notes.append('Polling collector: native background task '+row['background_id']+
                                     ' completed with exit '+str(row['exit_code'])+
                                     '. Original native output and cancellation handle remain unchanged.')
                # This is still the same PostToolBatch boundary. Per-tool
                # callbacks never renew it; no provider cache-hit claim follows.
                polling_receipt.write(root/'deadline.json', dict(origin_monotonic=time.monotonic(), boundary=event))
        return None
    tool_id = payload.get('tool_use_id')
    if not isinstance(tool_id, str) or not tool_id:
        return None
    note_path = root/('note-'+identity(tool_id)+'.json')
    if event in ('PostToolUse', 'PostToolUseFailure'):
        notes = []
        for path, row in tasks(root):
            if row.get('tool_use_id') == tool_id:
                response = payload.get('tool_response')
                background = response.get('backgroundTaskId') if isinstance(response, dict) else None
                if isinstance(background, str) and background:
                    row['background_id'] = background
                    polling_receipt.write(path, row)
                done = completed(row)
                if done is not None and row.get('announce_completed'):
                    notes.append('Polling collector: original native command completed with exit '+str(done['exit_code'])+'. Native output and exit status are unchanged.')
            if (event == 'PostToolUse' and payload.get('tool_name') == 'TaskStop' and
                    isinstance(row.get('background_id'), str) and row['background_id'] and
                    payload.get('tool_input', {}).get('task_id') == row.get('background_id')):
                row['cancelled'] = True
                polling_receipt.write(path, row)
        note = read(note_path)
        if note is not None and isinstance(note.get('text'), str):
            notes.append(note['text'])
            note_path.unlink(missing_ok=True)
        if notes:
            # Coalesce parallel notes once, after every native result in the
            # batch, before the next model request.
            polling_receipt.write(root/('delivery-'+identity(tool_id)+'.json'), dict(text=' '.join(notes)))
        return None
    if event != 'PreToolUse' or read(root/'deadline.json') is None:
        return None
    collected = poll(payload, root, getattr(ctx, 'telemetry_effect', None))
    if collected is not None:
        updated, note = collected
        polling_receipt.write(note_path, dict(text=note))
    else:
        updated = launch(payload, root)
    return {'hookSpecificOutput': dict(hookEventName=event, updatedInput=updated)} if updated else None
