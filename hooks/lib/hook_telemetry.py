"""Bounded local scalar diagnostics. Emission is not model-delivery proof."""
import hashlib
import json
import os
import stat
import time
import uuid
import session

MAX_BYTES = 8 * 1024 * 1024


def digest(value):
    return hashlib.sha256(str(value).encode('utf-8', 'replace')).hexdigest() if value else None


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8')) if value is not None else None


class Invocation:
    def __init__(self, name, payload, started):
        self.payload = payload
        self.started = started
        self.row = dict(schema='claude-hook-telemetry-v1', invocation_id=uuid.uuid4().hex,
            timestamp=time.time(), hook=name, event=payload.get('hook_event_name'),
            tool_sha256=digest(payload.get('tool_name')),
            tool=payload.get('tool_name') if payload.get('tool_name') in
                ('Bash','Read','Write','Edit','Grep','Glob','WebFetch','TaskStop') else None,
            session_sha256=digest(payload.get('session_id')),
            tool_call_sha256=digest(payload.get('tool_use_id')),
            decision='skipped', reason='no-effect-reported', stdout_emitted=False,
            model_delivery_confirmed=None, bytes_before=size(payload.get('tool_response')),
            bytes_after=None, effects={})
        self.row['phase'] = 'started'
        self.write()

    def finish(self):
        """Never let diagnostics change the hook's reply or exit status."""
        self.row['phase'] = 'finished'
        self.row['elapsed_ms'] = round((time.monotonic()-self.started)*1000, 3)
        self.write()

    def write(self):
        try:
            target = os.environ.get('ORK_HOOK_TELEMETRY_DIR')
            directory = target if target else session.Store.for_payload(self.payload).path
            session.private_dir(directory)
            path = os.path.join(directory, 'hook-invocations.jsonl')
            fd = os.open(session.fs_path(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                         getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
                    return
                if hasattr(os, 'getuid') and stat.S_IMODE(info.st_mode) & 0o077:   # no POSIX mode bits on Windows
                    return
                raw = (json.dumps(self.row, separators=(',', ':'))+'\n').encode()
                if info.st_size + len(raw) > MAX_BYTES:
                    return
                os.write(fd, raw)
            finally:
                os.close(fd)
        except Exception:
            pass
