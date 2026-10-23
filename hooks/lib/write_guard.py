"""Records of compacted Reads for the Write guard (hooks/write-guard.py), #319.

A compacted Read (hooks/dedupe-reads.py) shows every line number, with the
elided ranges as a marker and empty lines; Claude Code's read-before-edit
state holds the whole file, so it accepts a Write rebuilt from the view, and
that Write deletes the elided code. A record says which lines of the file
each session has not seen, so the guard can refuse a Write that would
delete them.

**Storage.** One record per guarded file in ``<state>/ork-meter/read-guard/``
(0700): ``<name>.json`` (0600, written atomically, only when it changed),
where ``<name>`` is the SHA-256 of the file's resolved path, and one pointer
file per alias, ``<alias>.alias``, holding that name, so a lookup by any
spelling is one file read. A record:

* ``aliases``: SHA-256 of the file's ``(st_dev, st_ino)``, of its resolved
  path and of that path case-folded, so ``MODULE.PY`` on a case-insensitive
  disk, or the file after an Edit replaced it by renaming, finds it;
* ``lines``: the per-line hashes of the file as last synced (the first 16
  hex digits of each line's SHA-256, trailing CR dropped; blank lines are
  ``BLANK``). No path and no line text; nothing depends on the snapshot;
* ``views``: per session view (``view_key``: a session, or a subagent of
  it), the unread lines as ``[first, last, label]`` intervals, 0-based, in
  ``lines``' coordinates.

Each record has its own lock file. A Write check that can't take the lock
in ``WRITE_LOCK_WAIT`` refuses the Write; marking lines seen skips instead.

**Following the file.** Every look re-syncs the record to the disk: a line
diff of the stored hashes against the disk's moves unread lines with the
lines around them. A line inside a block an Edit replaced stays unread (its
new text included, conservatively); a deleted line is gone; an inserted line
is read. Lines count as seen in a view only when output shown to that view
carries their current text.

**The Write check.** The unread lines are the writing view's, or, for a
session with no view of its own (a fork, a resume in a new process, a
subagent), the union of every view (the strictest). The old file's line
hashes are aligned with the Write's (``difflib.SequenceMatcher``); an unread
line the alignment doesn't match is deleted by the Write. A copy of the text
elsewhere doesn't keep it: two identical function bodies, one shown and one
elided, count as two.

**Lifetimes.** A view empties when its session is delivered the whole file;
the record goes when every view is empty, or ``MAX_AGE_SECONDS`` after its last
look by any session, never sooner. A record that can't be parsed becomes a *tombstone*
(its aliases): every Write to that file is refused until the file is
delivered whole to the writing session (``cleared`` lists those views), or
the override is set. Neither an Edit nor any other change to the file clears
it: the model's own Edit is what the refusal suggests.
"""

import collections
import difflib
import hashlib
import json
import os
import re
import shlex
import time

import features
import session

DIR_NAME = "read-guard"
BLANK = "-"
# A record that no session has touched for this long goes. Every look at the
# file by any session (a Write check, a read it marks, a compaction) touches
# it, so only a file nobody has worked on for a month loses its guard; a
# session resumed after that has to Read the file again before Claude Code
# lets it Write (its read-before-edit check), and that Read is compacted anew
# or delivered whole.
MAX_AGE_SECONDS = 30 * 24 * 3600
MAX_FILE_BYTES = 4 * 1024 * 1024
WRITE_LOCK_WAIT = 1.0
SEEN_LOCK_WAIT = 0.2
LOCK_STALE = 10.0
# The diff is quadratic in the worst case; past this many lines a file that
# changed is compared as a whole instead (every unread line is looked up by hash).
MAX_DIFF_LINES = 20000
# The Write check's alignment is bounded the same way, but must not fail open:
# past this many lines in the file or the Write, after the counting pre-check,
# the Write is refused with a recoverable reason (about 0.4 s of alignment at
# this size on a laptop; 20,000 lines took 1.5 s and 40,000 8.9 s, past the
# hook's 5 s limit).
MAX_ALIGN_LINES = 10000


class Corrupt(Exception):
    """A record can't be trusted."""


class Busy(Exception):
    """A record's lock wasn't obtained in time."""


def line_hash(line):
    line = line[:-1] if line.endswith("\r") else line
    if not line.strip():
        return BLANK
    return hashlib.sha256(line.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def line_hashes(text):
    return [line_hash(line) for line in text.split("\n")]


def _sha(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def aliases(path):
    """The keys a file is known by: its identity on disk (when it exists),
    its resolved path and that path case-folded, each a SHA-256."""
    resolved = os.path.realpath(path)
    keys = []
    try:
        info = os.stat(session.fs_path(resolved))
        keys.append(_sha("ino:%d:%d" % (info.st_dev, info.st_ino)))
    except OSError:
        pass
    keys.append(_sha("path:" + os.path.normcase(resolved)))
    keys.append(_sha("fold:" + os.path.normcase(resolved).casefold()))
    return keys


def record_name(path):
    return _sha("path:" + os.path.normcase(os.path.realpath(path)))


def view_key(payload):
    """One view per session, and per subagent within it (``agent_id``)."""
    agent = payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else ""
    return _sha("view:%s\0%s" % (payload.get("session_id") or "", agent))[:32]


# --- storage ------------------------------------------------------------------

def directory(env, create=True):
    base = os.path.join(features.state_dir(env), DIR_NAME)
    return session.private_dir(base) if create else base


def exists(env):
    try:
        folder = directory(env, create=False)
        return os.path.isdir(session.fs_path(folder)) and any(
            name.endswith(".json") for name in os.listdir(session.fs_path(folder)))
    except (OSError, ValueError):
        return False


def _path(env, name, suffix):
    return os.path.join(directory(env), name + suffix)


def _read(path):
    """A record file: its dict, None when absent; ``Corrupt`` when unusable."""
    try:
        with open(session.fs_path(path), "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as error:
        raise Corrupt(str(error)) from error
    if not isinstance(data, dict) or not isinstance(data.get("aliases"), list):
        raise Corrupt("not a record")
    if data.get("tombstone"):
        return data
    views = data.get("views")
    if (not isinstance(data.get("lines"), list) or not isinstance(views, dict)
            or any(not isinstance(v, list) for v in views.values())):
        raise Corrupt("not a record")
    return data


def _write(env, name, record):
    session.write_json(_path(env, name, ".json"), record)
    for alias in record.get("aliases", ()):
        pointer = _path(env, alias, ".alias")
        try:
            with open(session.fs_path(pointer), "r", encoding="utf-8") as stream:
                if stream.read() == name:
                    continue
        except OSError:
            pass
        session.write_private(pointer, name)


def _remove(env, name):
    try:
        os.unlink(session.fs_path(_path(env, name, ".json")))
    except OSError:
        pass


def locate(env, path):
    """The record name for ``path``: its own, or one an alias points at."""
    own = record_name(path)
    if os.path.isfile(session.fs_path(_path(env, own, ".json"))):
        return own
    for alias in aliases(path):
        try:
            with open(session.fs_path(_path(env, alias, ".alias")), "r", encoding="utf-8") as stream:
                name = stream.read().strip()
        except OSError:
            continue
        if re.fullmatch(r"[0-9a-f]{64}", name) and os.path.isfile(session.fs_path(_path(env, name, ".json"))):
            return name
    return None


class Locked:
    """``with Locked(env, name, wait) as held``: a record's lock."""

    def __init__(self, env, name, wait):
        self.manager = session._lock(_path(env, name, ".lock"), wait=wait, stale=LOCK_STALE)

    def __enter__(self):
        return self.manager.__enter__()

    def __exit__(self, *exc):
        return self.manager.__exit__(*exc)


def expire(env, now=None):
    """Delete records no session has looked at for ``MAX_AGE_SECONDS``
    (their file's mtime, which every look touches), then pointers to records
    that are gone and stale locks: never a younger record, however many."""
    now = time.time() if now is None else now
    try:
        folder = session.fs_path(directory(env, create=False))
        entries = list(os.scandir(folder))
    except OSError:
        return
    live = set()
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        try:
            if now - entry.stat(follow_symlinks=False).st_mtime > MAX_AGE_SECONDS:
                os.unlink(entry.path)
            else:
                live.add(entry.name[:-len(".json")])
        except OSError:
            continue
    for entry in entries:
        try:
            if entry.name.endswith(".alias"):
                with open(entry.path, "r", encoding="utf-8") as stream:
                    target = stream.read().strip()
                if target not in live:   # closed first: Windows won't delete an open file
                    os.unlink(entry.path)
            elif entry.name.endswith(".lock") and now - entry.stat(follow_symlinks=False).st_mtime > LOCK_STALE:
                os.unlink(entry.path)
        except OSError:
            continue


# --- re-syncing to the file on disk ---------------------------------------------

def _expand(unread):
    lines = {}
    for first, last, label in unread:
        for index in range(int(first), int(last) + 1):
            lines[index] = label
    return lines


def _compress(lines):
    out = []
    for index in sorted(lines):
        label = lines[index]
        if out and out[-1][1] == index - 1 and out[-1][2] == label:
            out[-1][1] = index
        else:
            out.append([index, index, label])
    return out


def _mapping(old, new):
    """Old line index -> new index for the lines a diff keeps in place, and
    (i1, i2, j1, j2) for each block replaced."""
    moved, replaced = {}, []
    if len(old) > MAX_DIFF_LINES or len(new) > MAX_DIFF_LINES:
        return None, None
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                moved[i1 + offset] = j1 + offset
        elif tag == "replace":
            replaced.append((i1, i2, j1, j2))
    return moved, replaced


def sync(record, disk):
    """Move every view's unread lines onto ``disk`` (the file's line hashes now)."""
    old = record["lines"]
    if old == disk:
        return False
    moved, replaced = _mapping(old, disk)
    views = {}
    for key, unread in record["views"].items():
        lines = _expand(unread)
        out = {}
        if moved is None:
            where = collections.defaultdict(list)
            for index, value in enumerate(disk):
                where[value].append(index)
            for index, label in lines.items():
                for target in where.get(old[index] if index < len(old) else None, ()):
                    out.setdefault(target, label)
        else:
            for index, label in lines.items():
                if index in moved:
                    out[moved[index]] = label
            for i1, i2, j1, j2 in replaced:
                labels = [lines[i] for i in range(i1, i2) if i in lines]
                if labels:
                    for target in range(j1, j2):   # an Edit inside unread lines: what replaced them is unread
                        out[target] = labels[0]
        views[key] = _compress({i: label for i, label in out.items() if disk[i] != BLANK})
    record["lines"] = disk
    record["views"] = views
    return True


def strictest(record):
    """Every view's unread lines together."""
    lines = {}
    for unread in record["views"].values():
        for index, label in _expand(unread).items():
            lines.setdefault(index, label)
    return lines


def unread_for(record, view):
    return _expand(record["views"][view]) if view in record["views"] else strictest(record)


def _seen(record, view, read):
    """Remove the 0-based lines ``read`` from ``view``'s unread lines (a view
    the session doesn't have yet starts from the strictest)."""
    unread = unread_for(record, view)
    before = len(unread)
    for index in read:
        unread.pop(index, None)
    if view in record["views"] and len(unread) == before:
        return False
    record["views"][view] = _compress(unread)
    return True


def seen_ranged(record, view, start, shown):
    """A ranged Read: the lines ``shown`` from line ``start`` (1-based) on,
    each read only if it carries that line's current text."""
    lines = record["lines"]
    read = [start - 1 + k for k, text in enumerate(shown)
            if 0 <= start - 1 + k < len(lines) and line_hash(text) == lines[start - 1 + k]]
    return _seen(record, view, read)


def seen_numbered(record, view, numbered):
    """``(line number, text)`` pairs (Grep -n, cat -n)."""
    lines = record["lines"]
    return _seen(record, view, [n - 1 for n, text in numbered if 0 < n <= len(lines) and line_hash(text) == lines[n - 1]])


def seen_block(record, view, shown, minimum=3):
    """Unnumbered output: runs of at least ``minimum`` lines matching the file in order."""
    out = [line_hash(text) for text in shown]
    matcher = difflib.SequenceMatcher(None, record["lines"], out, autojunk=False)
    read = []
    for block in matcher.get_matching_blocks():
        if block.size >= minimum or (block.size and block.size == len(out)):
            read.extend(range(block.a, block.a + block.size))
    return _seen(record, view, read)


# --- the Write decision ---------------------------------------------------------

class TooLarge(Exception):
    """The Write check would take too long to align: refuse, don't guess."""


def deleted(record, unread, new_text):
    """The unread lines (0-based -> label) a Write of ``new_text`` would
    delete: those the alignment of the file's lines with the Write's doesn't
    match. Position and multiplicity count, not mere presence.

    A counting pre-check runs first: an unread line whose text the Write
    holds fewer times than the file's unread lines do is deleted whatever the
    alignment says, so those are reported without aligning. Only when every
    count is met does the alignment run, and past ``MAX_ALIGN_LINES`` it
    raises ``TooLarge`` instead (the caller refuses the Write)."""
    unread = {index: label for index, label in unread.items() if record["lines"][index] != BLANK}
    if not unread:
        return {}
    new = line_hashes(new_text)
    available = collections.Counter(new)
    needed = collections.Counter(record["lines"][index] for index in unread)
    short = {value for value, count in needed.items() if available[value] < count}
    if short:
        return {index: label for index, label in unread.items() if record["lines"][index] in short}
    if len(record["lines"]) > MAX_ALIGN_LINES or len(new) > MAX_ALIGN_LINES:
        raise TooLarge(max(len(record["lines"]), len(new)))
    matcher = difflib.SequenceMatcher(None, record["lines"], new, autojunk=False)
    kept = set()
    for block in matcher.get_matching_blocks():
        kept.update(range(block.a, block.a + block.size))
    return {index: label for index, label in unread.items() if index not in kept}


def touch(env, name):
    """A session looked at this record's file: keep it from expiring."""
    try:
        os.utime(session.fs_path(_path(env, name, ".json")))
    except OSError:
        pass


def describe(lines, limit=10):
    """``A–B label`` for runs of the 0-based ``lines``, 1-based, at most ``limit``."""
    runs = _compress(lines)
    shown = ["%d–%d %s" % (first + 1, last + 1, label) for first, last, label in runs[:limit]]
    if len(runs) > limit:
        shown.append("and %d more" % (len(runs) - limit))
    return ", ".join(shown)


def read_disk(path):
    """The file's text, or None when it is gone, unreadable, not UTF-8 or too large."""
    try:
        with open(session.fs_path(path), "rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None


def file_sha(text):
    return None if text is None else _sha(text)


# --- operations -------------------------------------------------------------------

def remember(env, path, text, elided, view):
    """A compacted Read of ``path`` (content ``text``) in ``view``, with
    ``elided`` ranges ``(first, last, label)``, 1-based. Other views of the
    file are kept, re-synced to it."""
    expire(env)
    name = locate(env, path) or record_name(path)
    with Locked(env, name, WRITE_LOCK_WAIT) as held:
        if not held:
            raise Busy(name)
        try:
            record = _read(_path(env, name, ".json"))
        except Corrupt:
            record = None
        disk = line_hashes(text)
        if record is None or record.get("tombstone"):
            record = {"version": 2, "aliases": [], "lines": disk, "views": {}}
        else:
            sync(record, disk)
        record["aliases"] = sorted(set(record["aliases"]) | set(aliases(path)))
        record["views"][view] = [[first - 1, last - 1, str(label)] for first, last, label in elided]
        record["ts"] = int(time.time())
        _write(env, name, record)


def forget(env, path, view):
    """``view`` was delivered the whole file: drop its view (and a tombstone);
    the record goes with its last view."""
    name = locate(env, path)
    if name is None:
        return
    with Locked(env, name, WRITE_LOCK_WAIT) as held:
        if not held:
            return   # the view stays: stricter, never looser
        try:
            record = _read(_path(env, name, ".json"))
        except Corrupt:
            record = {"version": 2, "tombstone": True, "aliases": sorted(set(aliases(path))), "cleared": []}
        if record is None:
            return
        if record.get("tombstone"):
            # A whole delivery clears a tombstone for this view only: another
            # session's view of the file is unknown, so it stays refused.
            cleared = sorted(set(record.get("cleared") or []) | {view})
            if cleared != record.get("cleared"):
                record["cleared"] = cleared
                record["ts"] = int(time.time())
                _write(env, name, record)
            return
        # This view holds the whole file now; the others still hold what they held.
        # An empty view says so, so this session isn't checked against the others.
        record["views"][view] = []
        if not any(record["views"].values()):
            _remove(env, name)
            return
        record["ts"] = int(time.time())
        _write(env, name, record)


def observe(env, path, view, shown=None, start=None, numbered=None):
    """Mark what output shown to ``view`` showed of ``path`` as read, after
    re-syncing the record to the disk: ``start`` and ``shown`` for a ranged
    Read, ``numbered`` for numbered output, ``shown`` alone for unnumbered
    output. Best effort: a busy lock, a tombstone or any failure skips it."""
    try:
        name = locate(env, path)
        if name is None:
            return
        text = read_disk(path)
        if text is None:
            return
        with Locked(env, name, SEEN_LOCK_WAIT) as held:
            if not held:
                return
            record = _read(_path(env, name, ".json"))
            if record is None or record.get("tombstone"):
                return
            changed = sync(record, line_hashes(text))
            keys = set(aliases(path))
            if not keys <= set(record["aliases"]):
                record["aliases"] = sorted(set(record["aliases"]) | keys)
                changed = True
            if numbered is not None:
                changed = seen_numbered(record, view, numbered) or changed
            elif start is not None:
                changed = seen_ranged(record, view, start, shown) or changed
            else:
                changed = seen_block(record, view, shown) or changed
            if changed:
                record["ts"] = int(time.time())
                _write(env, name, record)
            else:
                touch(env, name)
    except (Corrupt, OSError, ValueError, TypeError, KeyError, IndexError):
        pass


def check(env, path, view, new_text):
    """``None`` when a Write of ``new_text`` to ``path`` deletes no unread
    line, else ``(kind, detail)``: ``("lines", {index: label})``, or
    ``("unavailable", why)`` when the record can't be checked. Raises
    nothing for a file without a record."""
    name = locate(env, path)
    if name is None:
        return None
    text = read_disk(path)
    with Locked(env, name, WRITE_LOCK_WAIT) as held:
        if not held:
            return ("unavailable", "its record is busy")
        try:
            record = _read(_path(env, name, ".json"))
        except Corrupt:
            # A tombstone: refuse until the file is delivered whole to this
            # session, or the override. An edit to the file never clears it:
            # the model's own Edit is exactly what the refusal suggests.
            _write(env, name, {"version": 2, "tombstone": True, "aliases": sorted(set(aliases(path))),
                               "cleared": [], "ts": int(time.time())})
            return ("unavailable", "its record was unreadable")
        if record is None:
            return None
        touch(env, name)
        if record.get("tombstone"):
            if view in (record.get("cleared") or []):
                return None   # this session has been delivered the whole file since
            return ("unavailable", "its record was unreadable")
        if text is None:
            return None   # gone, or not text we compacted: nothing of it this Write could delete
        changed = sync(record, line_hashes(text))
        try:
            lost = deleted(record, unread_for(record, view), new_text)
        except TooLarge as error:
            return ("unavailable", "the file or the Write is too long to compare (%s lines)" % error)
        finally:
            if changed:
                record["ts"] = int(time.time())
                _write(env, name, record)
        return ("lines", lost) if lost else None


# --- tool output that shows lines of a file ---------------------------------------

GREP_NUMBERED = re.compile(r"^(?:(?P<path>.+?)[:-])?(?P<number>\d+)[:-](?P<text>.*)$")
ONE_FILE_NUMBERED = re.compile(r"^(?P<number>\d+)[:-](?P<text>.*)$")
CAT_NUMBERED = re.compile(r"^\s*(?P<number>\d+)\t(?P<text>.*)$")
SHOWING = frozenset(("cat", "head", "tail", "sed", "nl", "awk", "grep", "rg", "less", "more", "bat"))
MAX_OPERANDS = 4


def observe_output(env, view, tool, tool_input, response, cwd):
    """Lines of guarded files shown by a Grep (content mode) or a simple Bash
    command (``cat``, ``cat -n``, ``nl``, ``head``, ``tail``, ``sed -n``,
    ``grep -n`` and the like, on one file): each line that carries its file's
    current text is read in ``view``. Unnumbered output counts only in runs of
    three or more lines matching the file in order. Pipes, compound commands,
    scripts and editors are not tracked."""
    if not exists(env) or not isinstance(tool_input, dict) or not isinstance(response, dict):
        return
    if tool == "Grep":
        text = response.get("content")
        target = tool_input.get("path")
        if not isinstance(text, str) or not text:
            return
        single = isinstance(target, str) and os.path.isfile(session.fs_path(_resolve(target, cwd)))
        by_path = collections.defaultdict(list)
        for line in text.split("\n"):
            if single:
                match = ONE_FILE_NUMBERED.match(line)   # "N:text" a match, "N-text" context (-A/-B/-C)
                if match:
                    by_path[_resolve(target, cwd)].append((int(match.group("number")), match.group("text")))
                continue
            match = GREP_NUMBERED.match(line)
            if match and match.group("path"):
                by_path[_resolve(match.group("path"), cwd)].append((int(match.group("number")), match.group("text")))
        for path, numbered in list(by_path.items())[:MAX_OPERANDS]:
            observe(env, path, view, numbered=numbered)
        return
    if tool != "Bash":
        return
    command, stdout = tool_input.get("command"), response.get("stdout")
    if not isinstance(command, str) or not isinstance(stdout, str) or not stdout:
        return
    try:
        words = shlex.split(command)
    except ValueError:
        return
    if not words or any(word in ("|", ";", "&&", "||", ">", ">>") for word in words):
        return
    if os.path.basename(words[0]) not in SHOWING:
        return
    files = [_resolve(word, cwd) for word in words[1:]
             if not word.startswith("-") and os.path.isfile(session.fs_path(_resolve(word, cwd)))]
    if len(files) != 1:
        return   # one file: output lines can be told apart
    lines = stdout.split("\n")
    numbered = [(int(m.group("number")), m.group("text")) for m in map(CAT_NUMBERED.match, lines) if m]
    if not numbered and os.path.basename(words[0]) == "grep":
        numbered = [(int(m.group("number")), m.group("text")) for m in map(ONE_FILE_NUMBERED.match, lines) if m]
    if numbered:
        observe(env, files[0], view, numbered=numbered)
    else:
        observe(env, files[0], view, shown=lines)


def _resolve(path, cwd):
    if os.path.isabs(path) or not isinstance(cwd, str) or not cwd:
        return path
    return os.path.join(cwd, path)
