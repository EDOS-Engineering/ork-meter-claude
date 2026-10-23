"""How long saved tool output (``tool-output/``) is kept (#143).

Trimmed Bash output, whole-file Reads and MCP record handles save the full
original as ``tool-output/<sha256>.txt``/``.json`` in the session's store, so
a "Full output: <path>" note or a record handle can be followed later. The
policy, as invariants:

* The pruning session never prunes its own store. No pruner deletes another
  session's file modified within ``LIVE_GRACE_SECONDS``, by age or by the cap.
* Age: other stores lose files older than the age limit by mtime:
  ``ORK_METER_RETENTION_DAYS`` from the trusted env (hooks/lib/project_env.py
  drops a project's), else ``MAX_AGE_DAYS``. ``0`` ends only the session's
  own files, at its end; other stores keep ``MAX_AGE_DAYS``.
* Project pins: at a store's first SessionStart the project's own lower value
  (``project_env.project_retention``), or ``null``, is pinned in that store's
  ``MARKER_NAME``, created exclusively and never changed. A store's limit is
  the lower of the usual one and its own pin. A pin read through a link, not
  the user's own regular file, too large, malformed or out of range counts
  as none. A pin never lengthens retention and never reaches another store.
* Size cap: while the measured size of all stores is over ``SIZE_CAP_BYTES``,
  a run deletes the oldest of the files it examined in other stores (the
  oldest seen, not the oldest on disk) until it is under. The size is a lower
  bound: last known sizes in ``STATE_NAME`` (hashed keys, no names) count
  only for stores listed in this run and measured in this pass or the
  previous one, so a forged entry counts for nothing (a forged size for a
  real store lasts until that store is next scanned). Past
  ``MAX_STATE_ENTRIES`` stores the cap undercounts: it fires late, never early.
* SessionEnd with reason ``clear`` or ``logout``, or a 0 limit, deletes the
  session's own files; other reasons keep them for resume.
* Bounds: a run stops at ``TIME_BUDGET_SECONDS`` (state read and write
  included), ``MAX_ENTRIES`` entries examined or ``MAX_LISTED`` names listed,
  and gives one store at most ``STORE_ENTRIES``. Stores are visited in the
  order of their names' hashes; the next run resumes after the last store
  visited, and inside a store after its last artifact, so a listing in
  another order never makes a run skip one. A ``.txt``/``.json`` pair is
  examined and deleted whole or not at all.
* Only artifact-named files (or a writer's temp file) directly inside an
  ``ork-meter/tool-output`` directory under this session's own root are
  deleted; directories never are. Pruning runs only at SessionStart and
  SessionEnd, and every error is swallowed: it never changes a hook's reply.
"""

import hashlib
import heapq
import json
import os
import re
import stat
import time

import features
import project_env
import session

MAX_AGE_DAYS = features.RETENTION_DEFAULT_DAYS
SIZE_CAP_BYTES = 256 * 1024 * 1024
TIME_BUDGET_SECONDS = 0.2
MAX_ENTRIES = 5000            # entries examined per run: session directories and artifact files
STORE_ENTRIES = 1000          # of those, at most this many in any one store per run
MAX_LISTED = 200000           # names listed per run
STATE_NAME = "retention-state.json"   # per-root cursor and last known sizes, hashed keys
STATE_VERSION = 3
MAX_STATE_ENTRIES = 10000     # stores remembered per root, most recently seen kept
KEY_DIGITS = 32               # hash digits in a state key (128 bits)
MAX_RESUMES = 1024            # stores remembered part-way through their artifacts
MAX_STATE_ROOTS = 8
MAX_STORE_BYTES = 4 * 1024 ** 3   # a remembered store size is clamped to this
RETENTION_ENV = features.RETENTION_ENV
MARKER_NAME = "retention.json"   # in a session's store, beside tool-output/
MAX_MARKER_BYTES = 256
MAX_RETENTION_DAYS = features.RETENTION_MAX_DAYS
LIVE_GRACE_SECONDS = 24 * 3600
DISCARD_REASONS = ("clear", "logout")
DAY = 86400

_ARTIFACT = re.compile(r"^[a-f0-9]{64}\.(txt|json)$")
_HEX = re.compile(r"^[a-f0-9]{64}$")
_STATE_KEY = re.compile(r"^[a-f0-9]{%d}$" % KEY_DIGITS)
_TEMPORARY = re.compile(r"^\.ork-meter-[^/\\]*\.tmp$")


def retention_days(env):
    """The trusted ``env``'s age limit in days (the tunables' grammar)."""
    return features.parse_int((env or {}).get(RETENTION_ENV), MAX_AGE_DAYS, 0, MAX_RETENTION_DAYS)


def _marker_days(store):
    """The days pinned in ``store``'s own marker, or None. Read without
    following links, from the same file ``check_private`` passed: its own
    open, because ``session.read_bounded(follow=False)`` takes its own lstat."""
    path = session.fs_path(os.path.join(store, MARKER_NAME))
    try:
        info = os.lstat(path)
        if session.check_private(info, owner_only=False) or info.st_size > MAX_MARKER_BYTES:
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not os.path.samestat(os.fstat(stream.fileno()), info):
                return None
            data = json.loads(stream.read(MAX_MARKER_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    days = data.get("days") if isinstance(data, dict) else None
    return days if type(days) is int and 0 <= days <= MAX_RETENTION_DAYS else None


def pin(store, days):
    """Pin ``days`` (or None) in ``store`` unless it has a marker of any kind
    already. Created exclusively (0600, no links followed). Best effort."""
    path = session.fs_path(os.path.join(store, MARKER_NAME))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        return   # already pinned (or the store is gone)
    try:
        created = os.fstat(fd)
    except OSError:
        created = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"days": days}) + "\n")
    except OSError:
        # A partial marker would read as "none" for good: take it back.
        try:
            if created is not None and os.path.samestat(os.lstat(path), created):
                os.unlink(path)
        except OSError:
            pass


def _is_artifact(name):
    return bool(_ARTIFACT.match(name) or _TEMPORARY.match(name))


def _group(name):
    """``.txt`` and ``.json`` of one artifact live and die together."""
    return name.rsplit(".", 1)[0] if _ARTIFACT.match(name) else name


class _Budget:
    def __init__(self, deadline, max_entries):
        self.deadline = deadline
        self.left = max_entries
        self.listed = MAX_LISTED

    def spent(self):
        return self.left <= 0 or time.monotonic() >= self.deadline

    def take(self):
        """Count one entry examined; False once the budget is gone."""
        if self.spent():
            return False
        self.left -= 1
        return True

    def list(self):
        """Count one name listed; False once out of budget. The clock is
        read every 256 names only."""
        if self.listed <= 0:
            return False
        self.listed -= 1
        return self.listed % 256 or time.monotonic() < self.deadline


def _uid():
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid else 0


def _directory(path):
    """``lstat`` of a real directory (not a link), or None."""
    try:
        info = os.lstat(session.fs_path(path))
    except OSError:
        return None
    return info if stat.S_ISDIR(info.st_mode) else None


def _root(current):
    """(root, levels) for the stores beside ``current``, or (None, None).

    Only ``<root>/<project>/<session>/scratchpad/ork-meter`` under this
    session's own ``claude-<uid>`` (or ``claude``) root, or the tempdir
    fallback's ``claude-ork-meter-<uid>/<session>/ork-meter``, has siblings.
    ``levels`` leads from a session directory to its ``tool-output``."""
    store = os.path.dirname(os.path.abspath(current))
    parts = []
    path = store
    for _ in range(5):
        path, name = os.path.split(path)
        parts.append(name)
    # parts: ork-meter, scratchpad, <session>, <project>, <root name>
    if parts[0] != session.ORK_DIR:
        return None, None
    if parts[1] == "scratchpad" and parts[4] in ("claude-%d" % _uid(), "claude"):
        root = store
        for _ in range(4):
            root = os.path.dirname(root)
        return (root, ("scratchpad", session.ORK_DIR, session.TOOL_OUTPUT_DIR)) if _directory(root) else (None, None)
    if parts[2] == session.FALLBACK_PREFIX + str(_uid()):
        root = os.path.dirname(os.path.dirname(store))
        return (root, (session.ORK_DIR, session.TOOL_OUTPUT_DIR)) if _directory(root) else (None, None)
    return None, None


def _tool_output(session_dir, levels):
    """(path, lstat) of the session's ``tool-output`` if every level is a
    real directory (no links), else (None, None)."""
    path, info = session_dir, None
    for name in levels:
        path = os.path.join(path, name)
        info = _directory(path)
        if info is None:
            return None, None
    return path, info


def _order(name):
    """A directory's place in a pass: its name's hash, so the cursor names
    no directory and a reordered listing moves nothing."""
    return hashlib.sha256(os.fsencode(name)).hexdigest()[:KEY_DIGITS]


def _key(path):
    """A store's state key: the hash of its normalised absolute path, so the
    state file names no directory and every spelling of a store agrees."""
    return hashlib.sha256(os.fsencode(os.path.normcase(os.path.abspath(path)))).hexdigest()[:KEY_DIGITS]


def _listing(directory, budget):
    """``[(order, name)]`` of the real subdirectories of ``directory`` in
    pass order, ``[]`` when it can't be listed, None when out of budget."""
    found = []
    try:
        with os.scandir(session.fs_path(directory)) as entries:
            for entry in entries:
                if not budget.list():
                    return None
                if entry.is_dir(follow_symlinks=False):
                    found.append((_order(entry.name), entry.name))
    except OSError:
        return []
    found.sort()
    return found


def _projects(root, levels, budget):
    """``[(order, directory)]`` of the directories holding sessions, or None."""
    if levels[0] != "scratchpad":
        return [("", root)]
    found = _listing(root, budget)
    return None if found is None else [(order, os.path.join(root, name)) for order, name in found]


def _sessions(directory, levels, budget):
    """``[(order, session directory, key)]`` in ``directory``, or None."""
    found = _listing(directory, budget)
    if found is None:
        return None
    stores = []
    for order, name in found:
        path = os.path.join(directory, name)
        stores.append((order, path, _key(os.path.join(path, *levels))))
    return stores


def _list(root, levels, budget):
    """``(projects, listings, complete)``: every store under ``root``, by
    name only, in at most half the time left. ``listings`` holds each
    project's ``_sessions``; ``complete`` is False when the budget ran out."""
    saved, now = budget.deadline, time.monotonic()
    budget.deadline = min(saved, now + max(0.0, saved - now) * 0.5)
    listings = {}
    try:
        projects = _projects(root, levels, budget)
        for _, directory in projects or ():
            if time.monotonic() >= budget.deadline:
                return projects, listings, False
            found = _sessions(directory, levels, budget)
            if found is None:
                return projects, listings, False
            listings[directory] = found
        return projects, listings, projects is not None
    finally:
        budget.deadline = saved


def _walk(root, levels, cursor, budget, projects, listings):
    """Yield ``(position, session directory, key)`` for the stores after
    ``cursor`` in pass order, one budget entry each, listing what ``_list``
    didn't reach; yield None when the budget is gone. ``position`` is
    ``[project order, session order]`` (``""`` for the fallback's one level)."""
    if projects is None:
        projects = _projects(root, levels, budget)
        if projects is None:
            yield None
            return
    for project_order, directory in projects:
        if cursor and project_order < cursor[0]:
            continue
        found = listings.get(directory)
        if found is None:
            found = _sessions(directory, levels, budget)
            if found is None:
                yield None
                return
        for session_order, path, key in found:
            position = [project_order, session_order]
            if cursor and position <= cursor:
                continue
            if not budget.take():
                yield None
                return
            yield position, path, key


def _scan(directory, budget, after=None, limit=None):
    """``(groups, resume, complete)``: ``{group: [mtime, size, [paths]]}``
    for the artifact groups after ``after`` in name order, at most ``limit``
    files; ``resume`` is the last group examined when others remain, else
    None; ``complete`` is False when the run's budget ran out."""
    names = {}
    try:
        with os.scandir(session.fs_path(directory)) as entries:
            for entry in entries:
                if not budget.list():
                    return {}, after, False
                if not _is_artifact(entry.name):
                    continue
                group = _group(entry.name)
                if after is None or group > after:
                    names.setdefault(group, []).append(entry)
    except OSError:
        return {}, None, True
    chosen = sorted(names) if limit is None else heapq.nsmallest(limit, names)
    groups, used, resume = {}, 0, None
    for group in chosen:
        files = names[group]
        if limit is not None and used and used + len(files) > limit:
            return groups, resume, True   # a pair is never split across runs
        if budget.left < len(files):
            return groups, resume, False   # nor across the entry budget
        for entry in files:
            if not budget.take():
                # Out of time mid-pair: drop the half examined (#190).
                groups.pop(group, None)
                return groups, resume, False
            used += 1
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            item = groups.setdefault(group, [0.0, 0, []])
            item[0] = max(item[0], st.st_mtime)
            item[1] += st.st_size
            item[2].append(os.path.join(directory, entry.name))
        resume = group
    return groups, (resume if len(chosen) < len(names) else None), True


def _delete(paths):
    """Unlink ``paths`` (files only); a failure waits for the next run."""
    gone = 0
    for path in paths:
        try:
            os.unlink(session.fs_path(path))
            gone += 1
        except OSError:
            pass
    return gone


def _deadline(deadline):
    limit = time.monotonic() + TIME_BUDGET_SECONDS
    return limit if deadline is None else min(deadline, limit)


def _state_path(env):
    return os.path.join(features.state_dir(env), STATE_NAME)


def _count(value, ceiling):
    return value if type(value) is int and 0 <= value <= ceiling else None


def _resume(value):
    """A store's place in the state file, ``[group, bytes so far]``, or None."""
    if not (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)):
        return None
    group, kept = value
    if not (_HEX.match(group) or (len(group) <= 255 and _TEMPORARY.match(group))):
        return None
    kept = _count(kept, 2 ** 62)
    return None if kept is None else [group, min(kept, MAX_STORE_BYTES)]


def _load_state(env, root):
    """The state file, cleaned: anything malformed or out of range is
    dropped, sizes are clamped, and a size from a future pass or from before
    the previous one is dropped."""
    state = session.read_json(_state_path(env), {})
    roots = state.get("roots") if isinstance(state, dict) and state.get("version") == STATE_VERSION else None
    roots = {k: v for k, v in roots.items() if _STATE_KEY.match(k) and isinstance(v, dict)} if isinstance(roots, dict) else {}
    known = roots.pop(root, {})
    roots = dict(list(roots.items())[:MAX_STATE_ROOTS - 1])
    current = _count(known.get("pass"), 2 ** 53)
    current = 0 if current is None else current
    raw = known.get("bytes") if isinstance(known.get("bytes"), dict) else {}
    sizes = {}
    for key, value in raw.items():
        if not (isinstance(key, str) and _STATE_KEY.match(key) and isinstance(value, list) and len(value) == 2):
            continue
        seen = _count(value[1], current)
        if seen is None or seen < current - 1 or type(value[0]) is not int:
            continue
        sizes[key] = [min(max(value[0], 0), MAX_STORE_BYTES), seen]
    raw = known.get("resume") if isinstance(known.get("resume"), dict) else {}
    resumes = {}
    for key, value in list(raw.items())[:MAX_RESUMES]:
        place = _resume(value)
        if isinstance(key, str) and _STATE_KEY.match(key) and place:
            resumes[key] = place
    cursor = known.get("cursor")
    if not (isinstance(cursor, list) and len(cursor) == 2
            and all(isinstance(x, str) and (x == "" or _STATE_KEY.match(x)) for x in cursor)):
        cursor = None
    mine = {"cursor": cursor, "pass": current, "bytes": _bounded(sizes), "resume": resumes}
    roots[root] = mine
    return {"version": STATE_VERSION, "roots": roots}, mine


def _bounded(sizes):
    """At most ``MAX_STATE_ENTRIES`` stores, the most recently seen first."""
    if len(sizes) <= MAX_STATE_ENTRIES:
        return sizes
    return dict(sorted(sizes.items(), key=lambda item: -item[1][1])[:MAX_STATE_ENTRIES])


def _measure(known, key, after, running, more, complete):
    """Remember a store's size after a scan that resumed after ``after``
    with ``running`` bytes counted so far, and return it. Seen in part, it
    resumes after ``more`` and counts at least what it held before."""
    running = min(running, MAX_STORE_BYTES)
    size = running
    if more is not None or not complete:
        place = more if more is not None else after
        if place is not None:
            known["resume"][key] = [place, running]
        previous = known["bytes"].get(key)
        size = max(running, previous[0] if previous else 0)
    known["bytes"][key] = [size, known["pass"]]
    return size


def prune(env, current=None, now=None, deadline=None, max_entries=MAX_ENTRIES, size_cap=SIZE_CAP_BYTES):
    """Apply the age limit and the size cap to the stores beside
    ``current``, this session's ``tool-output`` (which need not exist:
    counted toward the cap, never pruned, never created). Returns counts."""
    now = time.time() if now is None else now
    budget = _Budget(_deadline(deadline), max_entries)
    days = retention_days(env)
    others = days if days > 0 else MAX_AGE_DAYS
    fresh = now - LIVE_GRACE_SECONDS
    stats = {"scanned_dirs": 0, "deleted_files": 0, "pass_complete": False, "estimated_bytes": None}
    if not current:
        return stats
    mine = _directory(current)   # None for a session that hasn't saved anything yet
    root, levels = _root(current)
    if root is None:
        if mine is not None:
            groups = _scan(current, budget, limit=STORE_ENTRIES)[0]
            stats["estimated_bytes"] = sum(size for _, size, _ in groups.values())
        return stats
    started = time.monotonic()
    state, known = _load_state(env, _key(root))
    budget.deadline -= max(0.0, time.monotonic() - started)   # keep as long back for the write
    resumes, own_key = known["resume"], _key(current)
    after, running = resumes.pop(own_key, (None, 0))
    current_bytes = 0
    if mine is not None:
        groups, more, complete = _scan(current, budget, after, STORE_ENTRIES)
        current_bytes = _measure(known, own_key, after, running + sum(g[1] for g in groups.values()),
                                 more, complete)
    known["bytes"].pop(own_key, None)
    projects, listings, listed_all = _list(root, levels, budget)
    existing = {key for found in listings.values() for _, _, key in found}
    measured = set()   # stores a scan measured in this run
    candidates = []    # (mtime, size, paths, store key) examined this run, outside the grace
    position = known["cursor"]
    finished_pass = True
    for item in _walk(root, levels, known["cursor"], budget, projects, listings):
        if item is None:
            finished_pass = False
            break
        before, (position, path, key) = position, item
        directory, info = _tool_output(path, levels)
        if info is None or key == own_key or (mine is not None and os.path.samestat(info, mine)):
            continue
        after, running = resumes.pop(key, (None, 0))
        groups, more, complete = _scan(directory, budget, after, STORE_ENTRIES)
        stats["scanned_dirs"] += 1
        marked = _marker_days(os.path.dirname(directory))
        limit = others if marked is None else min(others, marked)
        cutoff = now - max(limit * DAY, LIVE_GRACE_SECONDS)
        kept = 0
        for mtime, size, paths in groups.values():
            if mtime < cutoff:
                stats["deleted_files"] += _delete(paths)
                continue
            kept += size
            if mtime < fresh:
                candidates.append((mtime, size, paths, key))
        _measure(known, key, after, running + kept, more, complete)
        measured.add(key)
        if not complete:   # out of budget inside this store: come back to it next run
            position = before
            finished_pass = False
            break
    sizes = known["bytes"]
    if finished_pass:
        known["cursor"] = None
        sizes = {d: v for d, v in sizes.items() if v[1] >= known["pass"]}   # unseen in the pass
        known["pass"] += 1
        stats["pass_complete"] = True
    else:
        known["cursor"] = position
    counted = {d: v for d, v in sizes.items() if d in measured or d in existing}
    if listed_all:   # a store that isn't there any more is forgotten
        sizes = counted
    estimate = current_bytes + sum(v[0] for v in counted.values())
    if estimate > size_cap:
        for mtime, size, paths, key in sorted(candidates, key=lambda c: c[0]):
            if estimate <= size_cap or time.monotonic() >= budget.deadline:
                break
            stats["deleted_files"] += _delete(paths)
            estimate -= size
            for entry, index in ((sizes.get(key), 0), (resumes.get(key), 1)):
                if entry:
                    entry[index] = max(0, entry[index] - size)
    if current_bytes:
        sizes[own_key] = [min(current_bytes, MAX_STORE_BYTES), known["pass"]]
    known["bytes"] = _bounded({d: v for d, v in sizes.items() if v[0] > 0})
    known["resume"] = dict(list(((d, v) for d, v in resumes.items() if d in known["bytes"]))[-MAX_RESUMES:])
    try:
        session.write_json(_state_path(env), state)
    except OSError:
        pass
    stats["estimated_bytes"] = estimate
    return stats


def discard(directory, deadline=None):
    """Delete every artifact in one session's ``tool-output`` directory."""
    if _directory(directory) is None:
        return 0
    groups = _scan(directory, _Budget(_deadline(deadline), MAX_ENTRIES))[0]
    return sum(_delete(paths) for _, _, paths in groups.values())


def missing_message(identity, env=None, store=None):
    """What scripts/artifact.py says when an artifact's files are gone:
    the limit in force for ``store``, from the trusted ``env``."""
    days = retention_days(os.environ if env is None else env)
    marked = _marker_days(store) if store else None
    days = days if marked is None else min(days, marked)
    return ("saved output %s is no longer on disk. Ork Meter keeps saved tool output for %d days "
            "(ORK_METER_RETENTION_DAYS), drops the oldest when all of it passes %d MiB, and deletes a "
            "session's copies when it ends with /clear or logout. Run the tool again to see its output."
            % (identity, days, SIZE_CAP_BYTES // (1024 * 1024)))


def _current_dir(ctx):
    return os.path.join(ctx.store().path, session.TOOL_OUTPUT_DIR)


def _pin_own_days(ctx):
    """At the store's first SessionStart, pin its project's lowered limit
    (or none); later starts keep it, their cwd may be another project's."""
    store = ctx.store().path
    if os.path.lexists(session.fs_path(os.path.join(store, MARKER_NAME))):
        return
    lowered = project_env.project_retention(ctx.env, ctx.payload)
    pin(store, lowered if lowered is not None and lowered < retention_days(ctx.env) else None)


def at_session_start(ctx):
    """SessionStart: pin, then prune other sessions. Never raises."""
    try:
        _pin_own_days(ctx)
        return prune(ctx.env, current=_current_dir(ctx), deadline=ctx.deadline)
    except Exception:
        return None


def at_session_end(ctx):
    """SessionEnd: drop this session's own files when it ends with
    ``clear``/``logout`` or a 0 limit (its own pin, never the cwd it ends
    in), then prune the others, in one budget. Never raises."""
    try:
        current = _current_dir(ctx)
        deadline = _deadline(ctx.deadline)
        own = _marker_days(ctx.store().path)
        if ctx.payload.get("reason") in DISCARD_REASONS or retention_days(ctx.env) == 0 or own == 0:
            discard(current, deadline=deadline)
        return prune(ctx.env, current=current, deadline=deadline)
    except Exception:
        return None
