"""What the model holds of each file it Read, shared by the two Read hooks.

The reads index (``reads.json``, hooks/lib/session.py) keeps one entry per
whole-file Read (``reads_key``): its hash, whether Ork Meter delivered it
partially, and then ``next``, the first line the model was not shown, with
the file's size and mtime at that delivery (``file_stamp``).

Claude Code records the whole file it read for its own repeat-read answer
before the Read result hook shortens it, so before 2.1.317 a whole-file
re-read of an unchanged file was answered "file unchanged" although the
model never saw the rest (F016, 0.5.8). The read guard (read-guard.py)
therefore sends such a re-read with ``offset=next`` (``recovery``), and the
Read result hook says so (``put_pending``/``take_pending`` carry the reason
from one hook to the other by ``tool_use_id``).
"""
import os

PENDING = "\x00pending\x00"
KINDS = ("page", "rest")


def reads_key(agent, file_path):
    import ledger
    return ledger.sha256_hex(agent) + "\x00" + file_path if isinstance(agent, str) and agent else file_path


def file_stamp(path):
    """``{"size", "mtime_ns"}`` of ``path`` now, or ``{}``."""
    try:
        st = os.stat(path)
    except (OSError, ValueError, TypeError):
        return {}
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def recovery(store, agent, file_path):
    """``(next, entry)`` when the model was shown only part of ``file_path``
    and the file hasn't changed since: a whole-file re-read should start at
    line ``next``. Otherwise None."""
    entry = store.reads().get(reads_key(agent, file_path))
    if not isinstance(entry, dict) or not entry.get("partial"):
        return None
    nxt, total = entry.get("next"), entry.get("total")
    if not (isinstance(nxt, int) and isinstance(total, int) and 1 < nxt <= total):
        return None
    stamp = file_stamp(file_path)
    if not stamp or stamp.get("size") != entry.get("size") or stamp.get("mtime_ns") != entry.get("mtime_ns"):
        return None
    return nxt, entry


def put_pending(store, tool_use_id, kind, path, **fields):
    if isinstance(tool_use_id, str) and tool_use_id and kind in KINDS:
        store.update_reads(PENDING + tool_use_id, dict(fields, kind=kind, path=path))


def take_pending(store, tool_use_id, path):
    """The guard's record for this tool call of ``path``, removed; or None."""
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    try:
        record = store.reads().get(PENDING + tool_use_id)
        if not isinstance(record, dict) or record.get("kind") not in KINDS or record.get("path") != path:
            return None
        store.update_reads(PENDING + tool_use_id, None)
        return record
    except (OSError, ValueError, TypeError):
        return None
