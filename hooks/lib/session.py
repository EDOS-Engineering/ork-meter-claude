"""Where a session's files live and what they are called.

This is the only module that knows the per-session layout. A ``Store`` is
one session's ``ork-meter/`` directory; the hooks reach it through
``ctx.store()`` (hooks/lib/hook.py) and ``scripts/savings.py`` finds stores
with ``Store.find`` and ``Store.all``. Every file name below is a named
accessor; nothing outside this module spells one.

Two layouts exist:

* ``${scratchpad_dir}/ork-meter`` when Claude Code sends ``scratchpad_dir``
  (2.1.257 and later). Claude Code keeps the scratchpad at
  ``<tmp>/claude-<uid>/<project>/<session_id>/scratchpad``;
* ``<tmp>/claude-ork-meter-<uid>/<session_id>/ork-meter`` otherwise (the
  tempdir fallback), where ``<tmp>`` is ``tempfile.gettempdir()``.

Files are owner-only: directories 0700, files 0600, written atomically.
Nothing here reads tool content; it only stores what a hook hands it.
"""

import contextlib
import errno
import glob
import hashlib
import json
import os
import stat
import time

ORK_DIR = "ork-meter"
LEDGER_NAME = "savings.jsonl"
SESSION_NAME = "session.json"
READS_NAME = "reads.json"
# Files Ork Meter showed partially (a compacted, head-trimmed or guard-paged
# whole-file Read) and files whose unshown lines the model then went back for
# with a ranged Read: SHA-256 of the path -> {"partial": ts, "wanted": ts}.
# A later whole-file Read of a wanted file is delivered whole
# (hooks/dedupe-reads.py, hooks/read-guard.py). Kept across compaction and
# resume: it says what the work needs, not what the context holds.
READ_DEMAND_NAME = "read-demand.json"
FAILURES_NAME = "failures.json"
SUMMARY_NAME = "session-summary.json"
POLICY_NAME = "session-policy.json"
REDUCER_HEALTH_DIR = "reducer-health"
REDUCER_DISABLED_NAME = "disabled.json"
TOOL_OUTPUT_DIR = "tool-output"
DELTA_DIR = "delta-bases"
TRACE_DIR = "shadow-traces"
ACCOUNTING_GAP_DIR = "accounting-gaps"
FALLBACK_PREFIX = "claude-ork-meter-"


def is_recovery_path(path):
    """Recognize plugin artifact files, including cross-platform path spelling."""
    if not isinstance(path, str):
        return False
    parts = path.replace("\\", "/").split("/")
    return any(parts[i:i + 2] == [ORK_DIR, TOOL_OUTPUT_DIR] for i in range(len(parts) - 1))


# Claude Code saves a tool result too large to return inline as
# ``<projects>/<project>/<session_id>/tool-results/<id>.txt`` and tells the
# model to Read it (a ``<persisted-output>`` result, observed with 2.1.308).
HOST_TOOL_RESULTS_DIR = "tool-results"


def is_host_tool_result(path, session_id):
    """A file Claude Code itself saved for this session because a tool result
    was too large to return. The model Reads it because it needs that output;
    shortening the Read only makes it Read the rest (#329)."""
    if not isinstance(path, str) or not isinstance(session_id, str) or not session_id:
        return False
    parts = path.replace("\\", "/").split("/")
    return len(parts) >= 3 and parts[-2] == HOST_TOOL_RESULTS_DIR and parts[-3] == session_id


def path_key(path):
    """A file's key in the read-demand index: the SHA-256 of its resolved,
    case-normalised path, so different spellings of one file meet and no path
    is stored."""
    resolved = os.path.normcase(os.path.realpath(path))
    return hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()


def _uid():
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid else 0


def safe_name(value, limit=80):
    """A file-name-safe form of an ID; anything odd becomes its hash."""
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= limit and all(c.isalnum() or c in "-_." for c in value) and not value.startswith("."):
        return value
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:32]


def check_private(info, directory=False, owner_only=True):
    """Why ``info`` (an ``os.lstat`` result) is not this user's own regular
    file (or, with ``directory``, directory): ``"type"`` for anything else,
    a link or a Windows reparse point included; ``"owner"`` when another
    user owns it or, with ``owner_only``, group or others have any
    permission bit. None when it is. Ownership and modes are POSIX only."""
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if not (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode) or reparse:
        return "type"   # under lstat a link is neither a regular file nor a directory
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or (owner_only and info.st_mode & 0o077)):
        return "owner"
    return None


NOT_REGULAR = "file must be a regular file, not a link"


class FileChanged(ValueError):
    """The file opened is not the one ``os.lstat`` checked: it was replaced
    in between, or this file system reports different ids to lstat and fstat
    (not seen on NTFS; ReFS is unverified)."""


def read_bounded(path, limit, follow=True):
    """At most ``limit`` bytes of ``path`` (None: no limit); ValueError when
    it holds more.

    With ``follow=False`` no link is followed: ``path`` must be a regular
    file under ``os.lstat`` (FileNotFoundError when it is missing,
    ValueError ``NOT_REGULAR`` for a link or anything else). It is opened
    with ``O_NOFOLLOW`` and ``O_NONBLOCK`` where they exist (a link or a FIFO
    swapped in neither is followed nor hangs), and before any read ``fstat``
    must show a regular file (else ``NOT_REGULAR``) that is the one checked
    (else ``FileChanged``)."""
    if follow:
        with open(path, "rb") as stream:
            raw = stream.read() if limit is None else stream.read(limit + 1)
    else:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(NOT_REGULAR)
        nonblock = getattr(os, "O_NONBLOCK", 0)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | nonblock | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:   # O_NOFOLLOW met a link swapped in after the lstat
                raise ValueError(NOT_REGULAR) from None
            raise
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(NOT_REGULAR)
            if not os.path.samestat(opened, info):
                raise FileChanged("file changed between its check and its open")
            if nonblock:
                os.set_blocking(fd, True)
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            raw = stream.read() if limit is None else stream.read(limit + 1)
    if limit is not None and len(raw) > limit:
        raise ValueError("file exceeded its size allowance")
    return raw


def private_dir(path):
    """Create a private directory; refuse links and foreign-owned directories."""
    os.makedirs(fs_path(path), mode=0o700, exist_ok=True)
    problem = check_private(os.lstat(fs_path(path)), directory=True, owner_only=False)
    if problem == "type":
        raise OSError("private directory must not be a link")
    if problem:
        raise PermissionError("private directory belongs to another user")
    os.chmod(fs_path(path), 0o700)
    return path


REPLACE_RETRY_SECONDS = 0.5


def _replace(source, target):
    """``os.replace``, retried briefly on Windows.

    Windows refuses to replace a file another process has open (Python's
    ``open`` does not share delete access), e.g. a hook reading the same
    state file or a virus scanner. That holder lets go within milliseconds,
    so retry for a bounded time instead of failing the hook."""
    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def write_private(path, text):
    """Write a file nobody else can read (0600), atomically: the text goes
    to a temporary file beside ``path`` which then replaces it. Returns the
    written file's ``(st_dev, st_ino)``, taken before it replaces ``path``."""
    import tempfile   # imported where used: ~6 ms that most hook processes never need
    parent = private_dir(os.path.dirname(os.path.abspath(path)))
    fd, tmp = tempfile.mkstemp(prefix=".ork-meter-", suffix=".tmp", dir=fs_path(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="") as fh:
            fh.write(text)
            fh.flush()
            info = os.fstat(fh.fileno())
        _replace(tmp, fs_path(path))
        return (info.st_dev, info.st_ino)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read_json(path, default=None):
    """The JSON in ``path``, or ``default`` when it is missing or malformed."""
    try:
        with open(fs_path(path), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    """``obj`` as one sorted-key JSON document, written with ``write_private``."""
    write_private(path, json.dumps(obj, sort_keys=True) + "\n")


LOCK_WAIT_SECONDS = 0.5
LOCK_STALE_SECONDS = 5.0


@contextlib.contextmanager
def _lock(path, wait=None, stale=None):
    """Hold ``path`` as an exclusive lock file (``O_CREAT | O_EXCL``).

    Waits at most ``wait`` seconds. A lock older than ``stale`` seconds was
    left by a killed hook and is broken. When the lock is still held after
    the wait the body runs anyway: an atomic write that may lose a parallel
    update, the behaviour before the lock existed, beats a slow hook."""
    wait = LOCK_WAIT_SECONDS if wait is None else wait
    stale = LOCK_STALE_SECONDS if stale is None else stale
    deadline = time.monotonic() + wait
    held = False
    while True:
        try:
            os.close(os.open(fs_path(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            held = True
            break
        except FileExistsError:
            try:
                if time.time() - os.stat(fs_path(path)).st_mtime > stale:
                    os.unlink(fs_path(path))
            except OSError:
                pass   # released or broken meanwhile: retry below
        except PermissionError:
            # Windows refuses to create a file whose deletion is pending.
            if os.name != "nt":
                break
        except OSError:
            break   # e.g. a read-only store: fall back to an unlocked write
        if time.monotonic() >= deadline:
            break
        time.sleep(0.002)
    try:
        yield held
    finally:
        if held:
            try:
                os.unlink(fs_path(path))
            except OSError:
                pass


def fs_path(path):
    """Explicit Windows Unicode namespace at the filesystem boundary.

    CreateHardLinkW can retain MAX_PATH limits even when ordinary Python file
    I/O at that same path succeeds. Reads, checks and both link endpoints need
    the namespace. Callers retain ordinary display/recovery paths; publication
    still uses the same volume, private directories and first-writer semantics.
    """
    if os.name != "nt":
        return path
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def display_path(path):
    """Keep internal Windows namespace spelling out of recovery interfaces."""
    if os.name == "nt" and path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if os.name == "nt" and path.startswith("\\\\?\\"):
        return path[4:]
    return path


def comparable_path(path):
    """One spelling of ``path`` for comparing two names of a file: without
    the Windows ``\\\\?\\`` namespace (``\\\\?\\UNC\\`` becomes ``\\\\``), links
    resolved, then case- and separator-normalised."""
    resolved = os.path.realpath(display_path(os.fspath(path)))
    return os.path.normcase(os.path.normpath(display_path(resolved)))


def glob_paths(pattern):
    return [display_path(path) for path in glob.glob(fs_path(pattern))]


def initialize_json(path, obj):
    """Publish a complete private document once, across processes.

    Link an already-written sibling into place without replacing a winner.
    Unlike O_EXCL followed by a write, readers never see an empty/partial
    document; unlike os.replace, Windows writers never compete to replace
    session metadata held open by another hook. Unsupported filesystems fail
    open at the hook boundary rather than weakening atomic publication.
    """
    import tempfile
    parent = private_dir(os.path.dirname(os.path.abspath(path)))
    fd, temporary = tempfile.mkstemp(prefix=".ork-meter-", suffix=".tmp", dir=fs_path(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(obj, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(fs_path(temporary), fs_path(path))
            return True
        except FileExistsError:
            problem = check_private(os.lstat(fs_path(path)), owner_only=False)
            if problem == "type":
                raise OSError("session metadata must be a regular file")
            if problem:
                raise PermissionError("session metadata belongs to another user")
            winner = _read_winner(path)
            if not isinstance(winner, dict) or winner.get("session_id") != obj.get("session_id"):
                raise ValueError("concurrent session initialization identity mismatch")
            return False
    finally:
        _unlink_temporary(temporary)


def _read_winner(path):
    """The published document at ``path``, retried briefly on Windows.

    Windows applies sharing per file, not per name: while the winner deletes
    its temporary name (a second link to this same file) a reader's open
    fails, so ``read_json`` returns None for a complete document. Retry for
    the bound ``_replace`` uses before calling it a mismatch."""
    winner = read_json(path)
    if os.name != "nt":
        return winner
    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
    while winner is None and time.monotonic() < deadline:
        time.sleep(0.005)
        winner = read_json(path)
    return winner


def _unlink_temporary(temporary):
    """Remove ``initialize_json``'s temporary name.

    On Windows deleting it can fail with a sharing violation while another
    hook reads the published file (the same file under its other name).
    Retry for the bound ``_replace`` uses, then leave it: the document is
    already published, and a leftover ``.ork-meter-*.tmp`` is a complete
    private copy that nothing reads. POSIX: a plain unlink, as before."""
    if os.name != "nt":
        os.unlink(temporary)
        return
    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
    while True:
        try:
            os.unlink(temporary)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                return
            time.sleep(0.005)
        except OSError:
            return


def _distinct(bases):
    """``bases`` without empties and without a second path to the same
    directory (``os.path.realpath``: on macOS ``/tmp`` is ``/private/tmp``),
    keeping the first spelling seen."""
    out = []
    seen = set()
    for base in bases:
        if not base:
            continue
        real = os.path.realpath(base)
        if real not in seen:
            seen.add(real)
            out.append(base)
    return out


# Claude Code's scratchpads can sit under the system /tmp even when TMPDIR
# points elsewhere (on macOS it is a per-user /var/folders path), so both are
# searched. Tests replace this with their own directories: on a machine with
# live Claude Code sessions the real /tmp holds real stores.
SYSTEM_TEMP_BASES = ("/private/tmp", "/tmp")


def _temp_bases(env):
    import tempfile
    return _distinct((tempfile.gettempdir(),) + tuple(SYSTEM_TEMP_BASES))


def scratchpad_roots(env, bases=None):
    """Directories under which Claude Code keeps ``<project>/<session>/scratchpad``.
    ``bases`` replaces the temp directories searched."""
    bases = _distinct([env.get("CLAUDE_CODE_TMPDIR")] + _temp_bases(env) if bases is None else bases)
    out = []
    for base in bases:
        out.append(os.path.join(base, "claude-%d" % _uid()))
        out.append(os.path.join(base, "claude"))
    return out


def fallback_roots(env, bases=None):
    """Directories under which the tempdir fallback keeps ``<session>/ork-meter``.
    ``bases`` replaces the temp directories searched."""
    bases = _temp_bases(env) if bases is None else _distinct(bases)
    return [os.path.join(base, FALLBACK_PREFIX + str(_uid())) for base in bases]


def _file(name):
    """A ``Store`` property: the path of one named file in the store."""
    return property(lambda self: os.path.join(self.path, name))


class Store:
    """One session's ``ork-meter/`` directory."""

    def __init__(self, path):
        self.path = path

    def __repr__(self):
        return "Store(%r)" % self.path

    def __eq__(self, other):
        return isinstance(other, Store) and other.path == self.path

    def __hash__(self):
        return hash(self.path)

    # --- finding a store -------------------------------------------------

    @classmethod
    def for_scratchpad(cls, scratchpad):
        """The store inside a known scratchpad directory (not created)."""
        return cls(os.path.join(scratchpad, ORK_DIR))

    @classmethod
    def fallback_dir(cls, session_id):
        """Where the tempdir fallback keeps this session's scratchpad stand-in."""
        import tempfile
        sid = safe_name(session_id) or "unknown-session"
        return os.path.join(tempfile.gettempdir(), FALLBACK_PREFIX + str(_uid()), sid)

    @classmethod
    def for_payload(cls, payload):
        """The store for a hook payload, its directory created 0700.

        Reuse an existing store when the scratchpad field changes. For a
        new session, use ``${scratchpad_dir}/ork-meter`` when supplied,
        or the tempdir fallback otherwise.
        """
        payload = payload if isinstance(payload, dict) else {}
        sid = payload.get("session_id")
        base = payload.get("scratchpad_dir")
        fallback = cls(os.path.join(cls.fallback_dir(sid), ORK_DIR))
        # Keep the original fallback when a scratchpad appears later. State,
        # summaries and the ledger must continue to describe one session.
        if sid and os.path.isdir(fs_path(fallback.path)):
            base = os.path.dirname(fallback.path)
        if not isinstance(base, str) or not base:
            existing = cls.find(sid, os.environ)
            base = os.path.dirname(existing.path) if existing else cls.fallback_dir(sid)
            private_dir(os.path.dirname(base))
        private_dir(base)
        store = cls(os.path.join(base, ORK_DIR))
        private_dir(store.path)
        return store

    @classmethod
    def existing_for_payload(cls, payload, env):
        """Locate an existing store using for_payload precedence, without writes."""
        sid = payload.get("session_id")
        fallback = cls(os.path.join(cls.fallback_dir(sid), ORK_DIR))
        if sid and os.path.isdir(fs_path(fallback.path)):
            return fallback
        base = payload.get("scratchpad_dir")
        if isinstance(base, str) and base:
            store = cls.for_scratchpad(base)
            return store if os.path.isdir(fs_path(store.path)) else None
        return cls.find(sid, env)

    @classmethod
    def find(cls, session_id, env):
        """The store for ``session_id`` in either layout, or None.

        Prefer an existing fallback store, then an existing scratchpad store.
        An empty scratchpad is a last-resort candidate, so it never hides an
        existing ledger in either layout.
        """
        if not isinstance(session_id, str) or not session_id:
            return None
        sid = safe_name(session_id)
        for root in fallback_roots(env):
            path = os.path.join(root, sid, ORK_DIR)
            if os.path.isdir(fs_path(path)):
                return cls(path)
        if sid != session_id:
            # Only a plain id names a directory: "../x" or an absolute id in
            # the glob below could reach outside the scratchpad roots.
            return None
        candidate = None
        for root in scratchpad_roots(env):
            for path in sorted(glob_paths(os.path.join(root, "*", glob.escape(session_id), "scratchpad"))):
                if os.path.isdir(fs_path(path)):
                    store = cls.for_scratchpad(path)
                    if os.path.isdir(fs_path(store.path)):
                        return store
                    candidate = candidate or store
        return candidate

    @classmethod
    def all(cls, env, roots=None, bases=None, every=False):
        """Stores with a ledger or accounting-gap evidence, sorted by path.

        ``roots`` limits the search to those scratchpad roots (no fallback).
        ``bases`` replaces the temp directories searched in both layouts.
        ``every`` keeps every store directory, ledger or not (a session the
        plugin ran in without a firing; scripts/activation_report.py).
        """
        found = []
        if roots is None:
            scratch_roots = scratchpad_roots(env, bases)
            fb_roots = fallback_roots(env, bases)
        else:
            scratch_roots = list(roots)
            fb_roots = []
        for root in scratch_roots:
            found.extend(glob_paths(os.path.join(root, "*", "*", "scratchpad", ORK_DIR)))
        for root in fb_roots:
            found.extend(glob_paths(os.path.join(root, "*", ORK_DIR)))
        if every:
            return [cls(p) for p in sorted(set(found)) if os.path.isdir(fs_path(p))]
        return [cls(p) for p in sorted(set(found)) if os.path.isfile(fs_path(os.path.join(p, LEDGER_NAME)))
                or os.path.isdir(fs_path(os.path.join(p, ACCOUNTING_GAP_DIR)))]

    # --- what is in it ---------------------------------------------------

    ledger_path = _file(LEDGER_NAME)
    session_path = _file(SESSION_NAME)
    policy_path = _file(POLICY_NAME)
    reads_path = _file(READS_NAME)
    read_demand_path = _file(READ_DEMAND_NAME)
    failures_path = _file(FAILURES_NAME)
    summary_path = _file(SUMMARY_NAME)

    def tool_output_dir(self):
        """The directory for full copies of trimmed tool output, created 0700."""
        return private_dir(os.path.join(self.path, TOOL_OUTPUT_DIR))

    def tool_output_path(self, name, extension=".txt"):
        if safe_name(name) != name or extension not in (".txt", ".json"):
            raise ValueError("invalid artifact name")
        return os.path.join(self.tool_output_dir(), name + extension)

    def _named(self, directory, name, extension, what):
        """``<store>/<directory>/<name><extension>``, the directory created
        0700; ``name`` must already be a ``safe_name``."""
        if safe_name(name) != name:
            raise ValueError("invalid " + what)
        return os.path.join(private_dir(os.path.join(self.path, directory)), name + extension)

    def delta_base_path(self, name):
        """The text the model already holds for one delta key (hooks/lib/delta.py)."""
        return self._named(DELTA_DIR, name, ".txt", "delta key")

    def delta_meta_path(self, name):
        return self._named(DELTA_DIR, name, ".json", "delta key")

    def forget_delta(self, name):
        for path in (self.delta_base_path(name), self.delta_meta_path(name)):
            try:
                os.unlink(fs_path(path))
            except OSError:
                pass

    def trace_path(self, identity):
        return self._named(TRACE_DIR, identity, ".json", "trace identity")

    def accounting_gap_path(self, identity):
        return self._named(ACCOUNTING_GAP_DIR, identity, ".json", "accounting gap identity")

    def record_gap(self, event, tool, planned, written, error, emitted=True, **extra):
        """Leave evidence (``accounting_gaps``) that emitted work could not be
        fully recorded in the ledger: ``written`` of ``planned`` rows, and
        why (``error``'s class name). Never raises; False when even this
        could not be written."""
        gap = {"schema_version": 1, "event": event, "tool": tool, **extra, "reply_emitted": emitted,
               "rows_planned": planned, "rows_written": written, "reason": error.__class__.__name__}
        try:
            write_json(self.accounting_gap_path(os.urandom(16).hex()), gap)
            return True
        except Exception:
            return False

    def reducer_health_path(self, scope_hash, create=False):
        if not isinstance(scope_hash, str) or len(scope_hash) != 64 or any(c not in "0123456789abcdef" for c in scope_hash):
            raise ValueError("invalid reducer scope")
        directory = os.path.join(self.path, REDUCER_HEALTH_DIR)
        if create:
            private_dir(directory)
        # Full identities remain stable; fs_path handles long Windows paths at
        # the filesystem boundary without changing display/recovery paths.
        path = os.path.join(directory, scope_hash)
        return private_dir(path) if create else path

    def reducer_disabled_path(self, scope_hash):
        return os.path.join(self.reducer_health_path(scope_hash), REDUCER_DISABLED_NAME)

    def accounting_gaps(self):
        """Local evidence that emitted hook work could not be fully recorded."""
        result = []
        for path in sorted(glob_paths(os.path.join(self.path, ACCOUNTING_GAP_DIR, "*.json"))):
            value = read_json(path)
            if isinstance(value, dict):
                result.append(value)
        return result

    def has_ledger(self):
        return os.path.isfile(fs_path(self.ledger_path))

    # --- session record and per-session state ----------------------------

    def remember(self, payload):
        """Record the session id and transcript path once, so
        ``scripts/savings.py`` can measure the transcript later."""
        if os.path.exists(fs_path(self.session_path)) or not isinstance(payload, dict):
            return
        initialize_json(self.session_path, {
            "session_id": payload.get("session_id"),
            "transcript_path": payload.get("transcript_path"),
        })

    def session_info(self):
        """What ``remember`` wrote, or an empty dict."""
        info = read_json(self.session_path, {})
        return info if isinstance(info, dict) else {}

    def reads(self):
        """The repeat-read index: key -> entry dict. Empty when absent.

        Read under the index lock: on Windows a reader holding the file open
        makes a parallel writer's replace fail with a sharing violation."""
        with _lock(self.reads_path + ".lock"):
            return self._reads_unlocked()

    def _reads_unlocked(self):
        data = read_json(self.reads_path, {})
        return data if isinstance(data, dict) else {}

    def save_reads(self, index):
        write_json(self.reads_path, index)

    def update_reads(self, key, entry):
        """Set one reads-index entry. Parallel Read hooks each rewrite the
        whole index, so the read-modify-write runs under a short lock or
        they drop each other's entries (#131)."""
        with _lock(self.reads_path + ".lock"):
            index = self._reads_unlocked()
            index[key] = entry
            self.save_reads(index)

    def _demand(self, key):
        data = read_json(self.read_demand_path, {})
        entry = data.get(key) if isinstance(data, dict) else None
        return entry if isinstance(entry, dict) else {}

    def _mark_demand(self, key, field):
        with _lock(self.read_demand_path + ".lock"):
            data = read_json(self.read_demand_path, {})
            index = data if isinstance(data, dict) else {}
            entry = index.get(key) if isinstance(index.get(key), dict) else {}
            if field not in entry:
                index[key] = dict(entry, **{field: int(time.time())})
                write_json(self.read_demand_path, index)

    def mark_partial_view(self, key):
        """Ork Meter delivered a whole-file Read of this file (``path_key``) partially."""
        self._mark_demand(key, "partial")

    def had_partial_view(self, key):
        return "partial" in self._demand(key)

    def mark_wants_whole(self, key):
        """The model went back for lines a partial view of this file left out."""
        self._mark_demand(key, "wanted")

    def wants_whole(self, key):
        return "wanted" in self._demand(key)

    def reset(self):
        """Forget per-conversation state (the reads index, delta bases and
        loop-guard counters): the model's view of the conversation changed."""
        for path in (self.reads_path, self.failures_path):
            try:
                os.unlink(fs_path(path))
            except OSError:
                pass
        # Delta bases describe what the model held before the reset.
        self.forget_delta_bases()

    def forget_delta_bases(self):
        """Delete every stored delta base: full copies of tool output that
        are only useful while the conversation that saw them is live."""
        for path in glob_paths(os.path.join(self.path, DELTA_DIR, "*")):
            try:
                os.unlink(fs_path(path))
            except OSError:
                pass
