"""Read-only Python outlines and exact source retrieval with freshness checks.

Unsupported languages and invalid Python fall back to exact line selection.
No persistent index or source execution. Batches share one snapshot per file;
decorators and the entire symbol body are kept. Workspace search ranks files
and returns exact snippet lines with each file's hash, within stated bounds.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks/lib"))
# The sibling module by path, as scripts/audit.py does: tests load this file
# by path too, and putting scripts/ on sys.path would shadow hooks/lib's usage.
_spec = importlib.util.spec_from_file_location("ork_source_context", Path(__file__).with_name("source_context.py"))
source_context = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(source_context)
from read_tool import symbols  # noqa: E402  (shared with hooks/read-guard.py)


def _snapshot(path, raw, expected_hash):
    """``raw``'s digest and physical lines; ValueError when it isn't the
    ``expected_hash`` snapshot."""
    digest = hashlib.sha256(raw).hexdigest()
    if expected_hash and expected_hash != digest:
        raise ValueError("source changed; retrieve a fresh outline before selecting the symbol")
    return digest, source_context.physical_lines(raw, Path(path).suffix == ".py")


def retrieve(path, symbol=None, outline=False, start=1, limit=100, expected_hash=None, *, raw=None):
    raw = Path(path).read_bytes() if raw is None else raw
    digest, lines = _snapshot(path, raw, expected_hash)
    method = "exact lines"
    entries = None
    if Path(path).suffix == ".py":
        try:
            entries = symbols(ast.parse(raw))
            method = "Python AST"
        except SyntaxError:
            pass
    result = {"path": str(Path(path).resolve()), "sha256": digest, "total_lines": len(lines), "method": method}
    if outline and entries is not None:
        result.update({"symbols": entries, "source_included": False})
        return result
    if symbol:
        matches = [entry for entry in entries or [] if entry["name"] == symbol]
        if len(matches) != 1:
            raise ValueError("exact symbol not found or ambiguous; use a line range or lexical search")
        start, end = matches[0]["start"], matches[0]["end"]
    else:
        if start < 1 or limit < 1:
            raise ValueError("start and limit must be positive")
        end = min(len(lines), start + limit - 1)
    result.update({"start": start, "end": end, "omitted_lines": len(lines) - max(0, end - start + 1),
                   "source": "".join(lines[start - 1:end])})
    return result


MAX_BATCH_ITEMS = 16
MAX_SOURCE_BYTES = source_context.MAX_FILE_BYTES
MAX_BATCH_INPUT_BYTES = source_context.MAX_INPUT_BYTES
_serialized = source_context.serialized


def _request(request, index):
    if not isinstance(request, dict) or set(request) - {
            "id", "path", "action", "symbol", "start", "limit", "expected_hash", "query", "context"}:
        raise ValueError("request must be an object with known source-selection fields")
    identity = request.get("id", str(index))
    if not isinstance(identity, str) or not 0 < len(identity) <= 64:
        raise ValueError("request id must be a nonempty string of at most 64 characters")
    if not isinstance(request.get("path"), str) or not request["path"]:
        raise ValueError("request path must be a nonempty string")
    action = request.get("action", "lines")
    allowed = {"outline": set(), "symbol": {"symbol"}, "lines": {"start", "limit"},
               "search": {"query", "limit", "context"}}
    if not isinstance(action, str) or action not in allowed or set(request) - ({"id", "path", "action", "expected_hash"} | allowed[action]):
        raise ValueError("invalid action or fields for the selected action")
    if action == "symbol" and (not isinstance(request.get("symbol"), str) or not request["symbol"]):
        raise ValueError("symbol selection requires a nonempty exact symbol name")
    if action == "search" and (not isinstance(request.get("query"), str) or not 0 < len(request["query"]) <= 256):
        raise ValueError("literal search requires a nonempty query of at most 256 characters")
    for name, default, low, high in (("start", 1, 1, 10**9), ("limit", 100, 1, 10000), ("context", 0, 0, 5)):
        value = request.get(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError("invalid source selection " + name)
    expected = request.get("expected_hash")
    if expected is not None and (not isinstance(expected, str) or len(expected) != 64
                               or any(char not in "0123456789abcdef" for char in expected)):
        raise ValueError("expected_hash must be a lowercase SHA-256 digest")
    return identity, action


def search(path, raw, query, limit=100, context=0, expected_hash=None):
    """Literal, complete-file search with exact physical lines and honest omissions."""
    digest, lines = _snapshot(path, raw, expected_hash)
    hits = [index for index, line in enumerate(lines) if query in line]
    chosen = hits[:limit]
    selected = sorted({index for hit in chosen for index in range(max(0, hit - context), min(len(lines), hit + context + 1))})
    return {"path": str(Path(path).resolve()), "sha256": digest, "method": "literal search",
            "total_lines": len(lines), "scanned_lines": len(lines), "matching_lines": len(hits),
            "returned_matching_lines": len(chosen), "omitted_matching_lines": len(hits) - len(chosen),
            "context": context, "lines": [{"line": index + 1, "text": lines[index]} for index in selected]}


def batch(requests, workspace, budget=24 * 1024):
    """Pack independent read-only selections. Exact evidence is never cut to fit.

    One snapshot per resolved path keeps outlines/ranges/symbols consistent in
    this call. A later call must supply its expected hash to reject stale edits.
    No index, source execution, semantic dependency inference or editing occurs.
    """
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_BATCH_ITEMS:
        raise ValueError("a batch must contain between 1 and 16 source requests")
    if type(budget) is not int or not 1024 <= budget <= 1024 * 1024:
        raise ValueError("batch output budget must be between 1024 and 1048576 bytes")
    root = source_context.workspace_root(workspace)
    parsed = [_request(request, index) for index, request in enumerate(requests)]
    if len({identity for identity, _ in parsed}) != len(parsed):
        raise ValueError("batch request ids must be unique")
    snapshots, candidates = {}, []
    input_bytes = 0
    for request, (identity, action) in zip(requests, parsed):
        try:
            path = (root / request["path"]).resolve(strict=True)
            path.relative_to(root)
            _refuse_secret(path, root, request["path"])
            if not path.is_file():
                raise ValueError("source path must be a regular file in the workspace")
            if path not in snapshots:
                with path.open("rb") as stream:
                    raw = stream.read(MAX_SOURCE_BYTES + 1)
                if len(raw) > MAX_SOURCE_BYTES or input_bytes + len(raw) > MAX_BATCH_INPUT_BYTES:
                    raise ValueError("source exceeds the bounded batch input allowance; use exact single-file retrieval")
                input_bytes += len(raw)
                snapshots[path] = raw
            raw = snapshots[path]
            if action == "search":
                result = search(path, raw, request["query"], request.get("limit", 100),
                                request.get("context", 0), request.get("expected_hash"))
            else:
                result = retrieve(path, symbol=request.get("symbol"), outline=action == "outline",
                                  start=request.get("start", 1), limit=request.get("limit", 100),
                                  expected_hash=request.get("expected_hash"), raw=raw)
            candidates.append({"id": identity, "status": "ok", "result": result})
        except (OSError, ValueError, SyntaxError, RecursionError) as exc:
            candidates.append({"id": identity, "status": "error", "error": str(exc)})
    output = {"schema_version": 1, "kind": "source_batch", "workspace": str(root),
              "snapshot_scope": "one snapshot per resolved file in this invocation",
              "budget_bytes": budget, "input_bytes": input_bytes, "complete": False,
              "results": [{"id": entry["id"], "status": "output_budget_exceeded",
                  "path": entry["result"]["path"], "sha256": entry["result"]["sha256"],
                  "exact_evidence_included": False} if entry["status"] == "ok" else entry
                  for entry in candidates]}
    if len(_serialized(output).encode("utf-8")) > budget:
        raise ValueError("batch metadata exceeds output budget; use fewer requests or a larger budget")
    source_context.fill_budget(output, [(index, entry) for index, entry in enumerate(candidates)
                                        if entry["status"] == "ok"], budget)
    output["complete"] = all(entry["status"] == "ok" for entry in output["results"])
    return output


SEARCH_BUDGET = 12 * 1024  # the Bash trim budget, so a default search never needs trimming
MAX_SEARCH_BUDGET = 64 * 1024  # a search is for orientation; read more with --batch
MAX_SEARCH_QUERY = 256
MAX_SEARCH_FILES = 20000
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_INPUT_BYTES = 64 * 1024 * 1024
SEARCH_SECONDS = 2.0
MAX_ALSO_AT = 8
_clock = time.monotonic
_last_worker = None  # the latest regex scan child process, for tests
_project_map = None

# A hit defines the query when the text before it is a declaration keyword
# (optionally after modifiers) or when it is assigned at the start of a line.
_DEFINITION_PREFIX = re.compile(
    r"[ \t]*(?:(?:export|default|pub(?:\([\w:]*\))?|public|private|protected|internal|static|async|abstract"
    r"|final|override|virtual|unsafe|extern|inline|declare|open|data|sealed|readonly)[ \t]+)*"
    r"(?:def|class|function\*?|fn|func(?:[ \t]*\([^)]*\))?|type|interface|struct|enum|trait|impl|module"
    r"|const|let|var|val|macro|defn|defmacro|sub|record|typedef|object|protocol|extension|alias|namespace)"
    r"[ \t]+\*?[ \t]*")
_ASSIGNMENT_SUFFIX = re.compile(r"[ \t]*(?::=|(?::[^=]*)?=(?![=>]))")
_WORD = re.compile(r"\w")


class _Deadline(Exception):
    pass


def _workspace_rules():
    """scripts/project_map.py by path: its walker's ignore, vendored, generated
    and secret-name rules apply to search unchanged."""
    global _project_map
    if _project_map is None:
        spec = importlib.util.spec_from_file_location("ork_source_project_map", Path(__file__).with_name("project_map.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _project_map = module
    return _project_map


def _search_pattern(query, regex, ignore_case):
    if not isinstance(query, str) or not 0 < len(query) <= MAX_SEARCH_QUERY:
        raise ValueError("search requires a nonempty query of at most %d characters" % MAX_SEARCH_QUERY)
    flags = re.IGNORECASE if ignore_case else 0
    if not regex:
        return re.compile(re.escape(query), flags)
    # Any pattern is accepted: a regex scan runs in a child process that is
    # killed at the deadline, so no shape check is needed to bound it.
    try:
        return re.compile(query, flags)
    except (re.error, RecursionError, OverflowError) as exc:
        raise ValueError("invalid regular expression: %s" % exc)


def _definition(body, match):
    """Whether this hit is where the matched name is declared or assigned."""
    start, end = match.span()
    if start == end or _WORD.match(body, end):
        return False
    if _DEFINITION_PREFIX.fullmatch(body, 0, start):
        return True
    return not body[:start].strip() and _ASSIGNMENT_SUFFIX.match(body, end) is not None


def _glob_rules(globs):
    rules = []
    for value in globs:
        if not isinstance(value, str) or not 0 < len(value) <= 256:
            raise ValueError("each glob must be a nonempty pattern of at most 256 characters")
        anchored = "/" in value
        try:
            rules.append((anchored, _workspace_rules()._glob_regex(value.replace("\\", "/").lstrip("/"))))
        except re.error as exc:
            raise ValueError("invalid glob %r: %s" % (value, exc))
    return rules


def _scan(rel, raw, pattern, per_file, context, deadline=None):
    """One file's hits, ranking facts and chosen snippet blocks. With a
    ``deadline`` the clock is checked on every line (_Deadline)."""
    lines = source_context.physical_lines(raw, rel.endswith(".py"))
    hits, definitions = [], set()
    for index, line in enumerate(lines):
        if deadline is not None and _clock() > deadline:
            raise _Deadline()
        body = line.rstrip("\r\n")
        found = False
        for count, match in enumerate(pattern.finditer(body)):
            found = True
            if _definition(body, match):
                definitions.add(index)
                break
            if count >= 15:
                break
        if found:
            hits.append(index)
    if not hits:
        return None
    chosen = sorted(definitions)[:per_file]
    chosen = sorted(chosen + [index for index in hits if index not in definitions][:per_file - len(chosen)])
    selected = sorted({index for hit in chosen for index in range(max(0, hit - context), min(len(lines), hit + context + 1))})
    blocks = []
    for index in selected:
        if blocks and blocks[-1][-1] == index - 1:
            blocks[-1].append(index)
        else:
            blocks.append([index])
    name = rel.rsplit("/", 1)[-1]
    folder = rel[:-len(name)]
    score = (100 * min(len(definitions), 3) + (40 if pattern.search(name) else 20 if pattern.search(folder) else 0)
             + min(19, int(4 * math.log2(1 + len(hits)))))
    return {"score": score, "total_lines": len(lines), "hits": hits, "definitions": len(definitions), "chosen": chosen,
            "blocks": [[(index + 1, lines[index]) for index in block] for block in blocks]}


def _secret_file(root, rel):
    """project_map.secret_path on the workspace-relative path, plus its
    directory rules on the absolute one: a workspace that is itself
    ~/.docker or ~/.config/gh holds config.json or hosts.yml at its top."""
    rules = _workspace_rules()
    return rules.secret_path(rel) or rules.secret_location((Path(root) / rel).as_posix())


def _refuse_secret_root(root):
    if _workspace_rules().secret_location(Path(root).as_posix()):
        raise ValueError("refusing workspace %s: it is or lies in a credential or .git directory; use Claude's Read "
                         "tool so your permission rules apply" % root)


def _scan_files(root, candidates, pattern, per_file, context, deadline=None):
    """Records in order: ``{"open": path}`` just before a file is read, then
    ``{"path", "outcome", "bytes", "found"}`` where outcome is "scanned" or
    the skip reason, and a final ``{"stop": "input_limit"}`` when the input
    allowance runs out. With a ``deadline`` the clock is checked before each
    file and on every line (_Deadline). Shared by the in-process literal scan
    and the regex child, so both count alike."""
    project_map = _workspace_rules()
    input_bytes = 0
    for rel, size in candidates:
        if deadline is not None and _clock() > deadline:
            raise _Deadline()
        if input_bytes + size > MAX_SEARCH_INPUT_BYTES:
            yield {"stop": "input_limit"}
            return
        record = {"path": rel, "outcome": "scanned", "bytes": 0, "found": None}
        # The walk already skipped these; the child checks again because its
        # file list arrives on stdin.
        if _secret_file(root, rel):
            yield dict(record, outcome="secret_looking")
            continue
        yield {"open": rel}
        raw = project_map.read_bytes(root, rel, MAX_SEARCH_FILE_BYTES + 1)
        if raw is None:
            yield dict(record, outcome="unreadable")
            continue
        input_bytes += len(raw)
        record["bytes"] = len(raw)
        if len(raw) > MAX_SEARCH_FILE_BYTES:
            record["outcome"] = "oversized"
        elif b"\0" in raw[:8192]:
            record["outcome"] = "binary"
        else:
            try:
                record["found"] = _scan(rel, raw, pattern, per_file, context, deadline)
            except (UnicodeError, SyntaxError, LookupError):
                record["outcome"] = "undecodable"
            if record["found"]:
                record["found"].update(path=rel, sha256=hashlib.sha256(raw).hexdigest())
        yield record


WORKER_CPU_SECONDS = 5


def _scan_worker():
    """The regex scan child: one job line on stdin, one JSON record per line
    out. It caps itself so it can't outlive a parent that was killed before
    killing it: RLIMIT_CPU on POSIX ends even a single runaway match, and on
    every platform a thread exits the process once stdin, which the parent
    holds open until the scan ends, reaches EOF. That thread runs between
    lines; it cannot preempt one match, which holds the GIL."""
    job = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
    try:
        import resource
        seconds = int(job.get("cpu_seconds", WORKER_CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds + 1))
    except (ImportError, ValueError, OSError):
        pass
    finished = threading.Event()
    def orphaned():
        sys.stdin.buffer.read()
        # A parent that has read the done line may close stdin before the
        # main thread reaches its own exit: that is success, not an orphan.
        os._exit(0 if finished.is_set() else 3)
    threading.Thread(target=orphaned, daemon=True).start()
    # Every exit is os._exit: a normal interpreter shutdown aborts while
    # that thread still holds stdin's lock.
    try:
        root = str(source_context.workspace_root(job["root"]))
        _refuse_secret_root(root)
        pattern = re.compile(job["query"], re.IGNORECASE if job["ignore_case"] else 0)
        for record in _scan_files(root, [tuple(item) for item in job["files"]], pattern, job["per_file"],
                                  job["context"]):
            sys.stdout.buffer.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
            sys.stdout.buffer.flush()
        finished.set()
        sys.stdout.buffer.write(b'{"done":true}\n')
        sys.stdout.buffer.flush()
    except Exception as exc:  # reported through the parent's stderr tail
        sys.stderr.write("ork-meter scan worker: %s\n" % exc)
        sys.stderr.flush()
        os._exit(2)
    os._exit(0)


def _worker_records(job, deadline):
    """Run the regex scan in a child process and yield its records until it
    finishes or the deadline passes; then it is killed and reaped, also when
    the caller closes this generator. A regex that backtracks for minutes
    therefore costs at most the deadline, on every platform. Yields
    ``{"stop": reason}`` for anything but completion, with the last 200
    bytes of the child's stderr for ``worker_failed``."""
    global _last_worker
    child = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--scan-worker"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _last_worker = child
    lines, tail = queue.Queue(), bytearray()
    def pump():
        for line in child.stdout:
            lines.put(line)
        lines.put(None)
    def drain():
        for chunk in iter(lambda: child.stderr.read(4096), b""):
            tail.extend(chunk)
            del tail[:-200]
    def send():
        # The job line may block on a child that never reads; the deadline
        # still holds because the child is killed from the reading side.
        try:
            child.stdin.write(json.dumps(job, ensure_ascii=True).encode("ascii") + b"\n")
            child.stdin.flush()
        except OSError:
            pass
    threads = [threading.Thread(target=target, daemon=True) for target in (pump, drain, send)]
    for thread in threads:
        thread.start()
    def failed():
        if child.poll() is None:
            child.kill()
        child.wait()
        threads[1].join(timeout=2)
        return {"stop": "worker_failed", "stderr": tail.decode("utf-8", "replace")}
    try:
        while True:
            remaining = deadline - _clock()
            try:
                line = lines.get(timeout=remaining) if remaining > 0 else lines.get_nowait()
            except queue.Empty:
                yield {"stop": "time_limit"}
                return
            if line is None:
                yield failed()
                return
            try:
                record = json.loads(line)
            except ValueError:  # a line cut short by the child's death
                yield failed()
                return
            if record.get("done"):
                return
            yield record
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        for stream in (child.stdin, child.stdout, child.stderr):
            try:
                stream.close()
            except OSError:
                pass
        for thread in threads:
            thread.join(timeout=2)


def search_workspace(workspace, query, *, regex=False, ignore_case=False, globs=(), context=1, per_file=8,
                     budget=SEARCH_BUDGET, time_limit=SEARCH_SECONDS, text=False):
    """Rank the workspace's files for ``query`` and return exact snippet lines,
    as a JSON-ready dict or, with ``text``, the compact text of
    ``_pack_search_text``; the budget applies to whichever is returned.

    Literal unless ``regex``. Uses project_map's walker: ignored, vendored,
    generated and secret-looking files are never opened, symlinks are never
    followed and every read stays inside the resolved workspace. Files over
    1 MiB, binary and non-UTF-8 files are skipped and counted. Each file's
    snippet comes from one snapshot with its SHA-256. Identical snippet blocks
    in later files are shown once, with the other locations. Whole files that
    do not fit the output budget are withheld, never cut, and every limit
    reached is named in ``incomplete_reasons``.

    ``time_limit`` bounds the whole call. A literal scan checks the clock on
    every line; a regex scan runs in a child process killed at the deadline.
    The file being scanned when time runs out is ``interrupted_file`` and is
    counted as not scanned.
    """
    if type(budget) is not int or not 1024 <= budget <= MAX_SEARCH_BUDGET:
        raise ValueError("search output budget must be between 1024 and %d bytes" % MAX_SEARCH_BUDGET)
    if type(context) is not int or not 0 <= context <= 5:
        raise ValueError("search context must be 0..5 lines")
    if type(per_file) is not int or not 1 <= per_file <= 100:
        raise ValueError("search per-file hits must be 1..100")
    if len(globs) > 16:
        raise ValueError("at most 16 globs")
    started = _clock()
    deadline = started + time_limit
    pattern = _search_pattern(query, regex, ignore_case)
    rules = _glob_rules(globs)
    root = str(source_context.workspace_root(workspace))
    _refuse_secret_root(root)
    project_map = _workspace_rules()
    skipped = {"ignored": 0, "vendored_or_generated": 0, "secret_looking": 0, "symlinks": 0, "glob": 0,
               "oversized": 0, "binary": 0, "undecodable": 0, "unreadable": 0, "not_scanned": 0}
    reasons, matched = [], []
    scanned = input_bytes = processed = 0
    files, walked = project_map.walk(root)
    for key, value in walked.items():
        skipped[key] += value
    if len(files) >= project_map.MAX_FILES:
        reasons.append("walk_file_limit")
    candidates = []
    for rel, size in files:
        name = rel.rsplit("/", 1)[-1]
        if rules and not any(glob.match(rel if anchored else name) for anchored, glob in rules):
            skipped["glob"] += 1
        elif size > MAX_SEARCH_FILE_BYTES:
            skipped["oversized"] += 1
        else:
            candidates.append((rel, size))
    if len(candidates) > MAX_SEARCH_FILES:
        skipped["not_scanned"] += len(candidates) - MAX_SEARCH_FILES
        del candidates[MAX_SEARCH_FILES:]
        reasons.append("file_limit")
    if regex:
        records = _worker_records({"root": root, "query": query, "ignore_case": ignore_case, "per_file": per_file,
                                   "context": context, "files": candidates,
                                   "cpu_seconds": max(WORKER_CPU_SECONDS, math.ceil(time_limit) + 3)}, deadline)
    else:
        records = _scan_files(root, candidates, pattern, per_file, context, deadline)
    stop = opened = worker_stderr = None
    try:
        while candidates:
            try:
                record = next(records)
            except StopIteration:
                break
            except _Deadline:
                stop = "time_limit"
                break
            if "open" in record:
                opened = record["open"]
                continue
            if "stop" in record:
                stop, worker_stderr = record["stop"], record.get("stderr")
                break
            opened = None
            processed += 1
            input_bytes += record["bytes"]
            if record["outcome"] != "scanned":
                skipped[record["outcome"]] += 1
                continue
            scanned += 1
            if record["found"]:
                matched.append(record["found"])
    finally:
        # Ends the regex child now, not whenever the generator is collected.
        records.close()
    if stop is not None:
        reasons.append(stop)
    skipped["not_scanned"] += len(candidates) - processed
    # Only a file that was opened and not finished; one the clock stopped
    # before is simply not scanned.
    interrupted = opened if stop in ("time_limit", "worker_failed") else None
    matched.sort(key=lambda item: (-item["score"], item["path"]))
    output = {"schema_version": 1, "kind": "source_search", "workspace": root, "query": query,
              "mode": "regex" if regex else "literal", "ignore_case": ignore_case, "globs": list(globs),
              "context": context, "per_file": per_file, "budget_bytes": budget,
              "snapshot_scope": "one snapshot per file; pass sha256 as expected_hash before editing",
              "files_scanned": scanned, "files_skipped": skipped, "files_matched": len(matched),
              "interrupted_file": interrupted,
              **({"worker_stderr": worker_stderr} if worker_stderr is not None else {}),
              "input_bytes": input_bytes, "elapsed_seconds": round(_clock() - started, 3)}
    return (_pack_search_text if text else _pack_search)(output, matched, reasons, budget)


# Characters that could end a displayed line (str.splitlines boundaries and
# other controls): a source line holding one is shown as a JSON string.
_UNSAFE_TEXT = re.compile(r"[\x00-\x08\x0a-\x1f\x7f\x85\u2028\u2029]")
_PLAIN_PATH = re.compile(r"[A-Za-z_.@+][\w.@+,/-]*")


def _quoted(value):
    """A JSON string that also escapes what json.dumps leaves raw (DEL, NEL,
    U+2028, U+2029), so it stays on one displayed line."""
    return _UNSAFE_TEXT.sub(lambda match: "\\u%04x" % ord(match.group()), json.dumps(value, ensure_ascii=False))


def _text_path(path):
    """A path as it appears on a header: plain when it can't be mistaken for
    a numbered line, a separator or a footer, else a JSON string."""
    return path if _PLAIN_PATH.fullmatch(path) else _quoted(path)


def _text_line(number, text, hit):
    """``N: text`` for a hit, ``N- text`` for context (as grep prints them),
    or ``N:"..."``/``N-"..."`` with JSON escapes for unsafe text. Every
    source line starts with its number, and nothing else does except
    dedupe notes (``N=``), so source text can never forge a header."""
    body = text[:-2] if text.endswith("\r\n") else text[:-1] if text.endswith(("\n", "\r")) else text
    mark = ":" if hit else "-"
    if _UNSAFE_TEXT.search(body):
        return "%d%s%s" % (number, mark, _quoted(body))
    return "%d%s %s" % (number, mark, body)


def _text_summary(output, counts, reasons):
    """The first line and, when incomplete, the footer."""
    flags = [output["mode"]] + (["ignore-case"] if output["ignore_case"] else [])
    flags += ["glob " + _quoted(glob) for glob in output["globs"]]
    shown = ["%d shown" % counts["shown"]]
    if counts["deduplicated"]:
        shown.append("%d duplicate" % counts["deduplicated"])
    if counts["not_shown"]:
        shown.append("%d not shown" % counts["not_shown"])
    skipped = ", ".join("%s %d" % (key, value) for key, value in output["files_skipped"].items() if value)
    head = "search %s %s: %d files matched of %d scanned, %d matches (%s) %s" % (
        _quoted(output["query"]), " ".join(flags), output["files_matched"],
        output["files_scanned"], output["matches_total"], ", ".join(shown), "incomplete" if reasons else "complete")
    if not reasons:
        return head + ("; skipped " + skipped if skipped else "") + "\n", ""
    stopped = "; stopped in " + _text_path(output["interrupted_file"]) if output.get("interrupted_file") else ""
    if output.get("worker_stderr"):
        stopped += "; worker stderr " + _quoted(output["worker_stderr"])
    footer = "incomplete: %s%s; files withheld %d, omitted %d; matches not shown %d%s\n" % (
        ", ".join(reasons), stopped, counts["withheld"], counts["omitted"], counts["not_shown"],
        "; skipped " + skipped if skipped else "")
    return head + "\n", footer


def _pack_search_text(output, matched, reasons, budget):
    """The compact text form: a summary line, then per file a header
    (path, full sha256 for expected_hash, ``[def]`` when it defines the
    query, hits beyond --per-file) and its numbered lines, ``--`` between
    ranges when there is context (as grep does). A block already shown for a higher-ranked file becomes
    ``N= path:M`` (``N..E=`` for several lines). Files that don't fit the
    budget are withheld as a header-only line, or only counted."""
    total = output["matches_total"] = sum(len(item["hits"]) for item in matched)
    if any(len(item["hits"]) > len(item["chosen"]) for item in matched):
        reasons.append("per_file_cap")
    # Reserve the summary at its largest: every count at its maximum and the
    # budget reason present. The real one is never longer.
    widest = dict.fromkeys(("shown", "deduplicated", "not_shown", "withheld", "omitted"), max(total, len(matched)))
    head, footer = _text_summary(output, widest, reasons + ["output_budget"])
    used = len((head + footer).encode("utf-8"))
    if used > budget:
        raise ValueError("search summary exceeds the output budget; use a larger --budget")
    sections, seen = [], {}
    counts = dict.fromkeys(("shown", "deduplicated", "withheld", "omitted"), 0)
    for item in matched:
        chosen = {index + 1 for index in item["chosen"]}
        header = "%s sha256=%s" % (_text_path(item["path"]), item["sha256"])
        if item["definitions"]:
            header += " [def]"
        if len(item["hits"]) > len(chosen):
            header += " +%d hits not shown" % (len(item["hits"]) - len(chosen))
        rows, fresh, repeated_hits, previous_end = [], [], 0, None
        for block in item["blocks"]:
            first, last = block[0][0], block[-1][0]
            if output["context"] and previous_end is not None and first != previous_end + 1:
                rows.append("--")
            previous_end = last
            key = tuple(text for _, text in block)
            if key in seen:
                span = str(first) if first == last else "%d..%d" % (first, last)
                rows.append("%s= %s:%d" % (span, _text_path(seen[key][0]), seen[key][1]))
                repeated_hits += sum(line in chosen for line, _ in block)
            else:
                rows.extend(_text_line(line, text, line in chosen) for line, text in block)
                fresh.append((key, first))
        section = "\n".join([header] + rows) + "\n"
        size = len(section.encode("utf-8"))
        if used + size <= budget:
            used += size
            sections.append(section)
            for key, line in fresh:
                seen.setdefault(key, (item["path"], line))
            counts["shown"] += len(chosen) - repeated_hits
            counts["deduplicated"] += repeated_hits
            continue
        stub = "%s sha256=%s withheld: %d hits over budget\n" % (_text_path(item["path"]), item["sha256"],
                                                                 len(item["hits"]))
        if used + len(stub.encode("utf-8")) <= budget:
            used += len(stub.encode("utf-8"))
            sections.append(stub)
            counts["withheld"] += 1
        else:
            counts["omitted"] += 1
    counts["not_shown"] = total - counts["shown"] - counts["deduplicated"]
    reasons = reasons + (["output_budget"] if counts["withheld"] or counts["omitted"] else [])
    head, footer = _text_summary(output, counts, reasons)
    return head + "".join(sections) + footer


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _pack_search(output, matched, reasons, budget):
    """Fill ``results`` in rank order. A file that does not fit is withheld as
    a path-and-hash stub, or only counted when not even that fits."""
    total = sum(len(item["hits"]) for item in matched)
    if any(len(item["hits"]) > len(item["chosen"]) for item in matched):
        reasons.append("per_file_cap")
    # Size against the largest values the counts can take, so filling in the
    # real (never longer) ones afterwards keeps the output within budget.
    output.update(dict.fromkeys(("files_returned", "files_withheld", "files_omitted"), len(matched)))
    output.update(matches_total=total, **dict.fromkeys(("matches_returned", "matches_deduplicated", "matches_omitted"), total))
    output.update(complete=False, incomplete_reasons=reasons + ["output_budget"], results=[])
    used = len(_serialized(output).encode("utf-8"))
    if used > budget:
        raise ValueError("search metadata exceeds the output budget; use a larger --budget")
    results, seen = output["results"], {}
    returned = withheld = omitted = shown = deduplicated = 0
    for item in matched:
        chosen = {index + 1 for index in item["chosen"]}
        entry = {"path": item["path"], "sha256": item["sha256"], "status": "ok", "score": item["score"],
                 "definition_hits": item["definitions"], "total_lines": item["total_lines"],
                 "matching_lines": len(item["hits"]), "returned_matching_lines": len(chosen),
                 "omitted_matching_lines": len(item["hits"]) - len(chosen), "hits": sorted(chosen), "lines": []}
        # A block already shown by a higher-ranked file is replaced by this
        # file's location in that block's "also_at" list.
        owners, undo, fresh, same_as, repeated_hits, repeated_lines = {}, [], [], [], 0, 0
        for block in item["blocks"]:
            key = tuple(text for _, text in block)
            if key in seen:
                owner, record = seen[key]
                if id(owner) not in owners:
                    owners[id(owner)] = (owner, _json_size(owner))
                undo.append((owner, record, list(record["also_at"]), record.get("more")))
                if not record["also_at"]:
                    owner.setdefault("repeated", []).append(record)
                if len(record["also_at"]) < MAX_ALSO_AT:
                    record["also_at"].append("%s:%d" % (item["path"], block[0][0]))
                else:
                    record["more"] = record.get("more", 0) + 1
                same_as.append("%s:%d" % (owner["path"], record["line"]))
                repeated_lines += len(block)
                repeated_hits += sum(line in chosen for line, _ in block)
            else:
                entry["lines"].extend({"line": line, "text": text} for line, text in block)
                fresh.append((key, block[0][0]))
        if repeated_lines:
            entry["deduplicated_lines"] = repeated_lines
            # Named here too: "also_at" stops listing after MAX_ALSO_AT.
            entry["same_as"] = same_as
        delta = sum(_json_size(owner) - before for owner, before in owners.values()) + _json_size(entry) + bool(results)
        if used + delta <= budget:
            used += delta
            results.append(entry)
            for key, line in fresh:
                seen.setdefault(key, (entry, {"line": line, "also_at": []}))
            returned += 1
            shown += len(chosen) - repeated_hits
            deduplicated += repeated_hits
            continue
        for owner, record, also_at, more in reversed(undo):
            record["also_at"] = also_at
            if more is None:
                record.pop("more", None)
            else:
                record["more"] = more
            if not also_at:
                owner["repeated"] = [kept for kept in owner["repeated"] if kept is not record]
                if not owner["repeated"]:
                    del owner["repeated"]
        stub = {"path": item["path"], "sha256": item["sha256"], "status": "output_budget_exceeded",
                "matching_lines": len(item["hits"])}
        delta = _json_size(stub) + bool(results)
        if used + delta <= budget:
            used += delta
            results.append(stub)
            withheld += 1
        else:
            omitted += 1
    output.update(files_returned=returned, files_withheld=withheld, files_omitted=omitted, matches_returned=shown,
                  matches_deduplicated=deduplicated, matches_omitted=total - shown - deduplicated,
                  incomplete_reasons=reasons + (["output_budget"] if withheld or omitted else []))
    output["complete"] = not output["incomplete_reasons"]
    return output


def _artifact_roots():
    """Resolved bases under which Ork Meter keeps its own session stores."""
    import session
    bases = session.scratchpad_roots(os.environ) + session.fallback_roots(os.environ)
    return [Path(base).resolve() for base in bases], session.is_recovery_path


def _refuse_secret(path, root, written=None):
    """Refuse a secret-looking file or one in a credential directory, by the
    rules workspace search skips them with (project_map.secret_path), on the
    resolved path and on the path as written. The person's Read tool and its
    deny rules remain the way to read such a file."""
    secret_path = _workspace_rules().secret_path
    # The written spelling catches a symlink named like a secret; an absolute
    # one already resolved inside the root, whose own ancestors don't count.
    written = "" if written is None or Path(written).is_absolute() else str(written).replace("\\", "/")
    relative = Path(path).relative_to(root).as_posix()
    # The absolute paths count by their directories only, so a project
    # merely named like a secret is not refused, but ~/.docker or
    # ~/.config/gh given as the workspace is.
    if (any(rel and secret_path(rel) for rel in (relative, written))
            or _workspace_rules().secret_location(Path(path).as_posix())
            or _workspace_rules().secret_location(Path(root).as_posix())):
        raise ValueError("refusing %s: it looks like a secret or credential file; use Claude's Read tool so "
                         "your permission rules apply" % (written or relative))


class _Once(argparse.Action):
    """An option that may appear once: with --workspace given twice the last
    would win, letting a pre-approved prefix's workspace be replaced."""
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error("%s may be given only once" % self.option_strings[0])
        setattr(namespace, self.dest, values)


def single_path(value):
    """Resolve a single-retrieval path, refusing anything outside the workspace.

    A script's own reads are invisible to Claude Code's Read deny rules, so this
    reads only regular files that resolve (symlinks followed) inside the
    current working directory or inside Ork Meter's own tool-output store.
    """
    path = Path(value).resolve()
    allowed = path.is_relative_to(Path.cwd().resolve())
    if not allowed:
        roots, is_recovery_path = _artifact_roots()
        allowed = any(path.is_relative_to(root) and is_recovery_path(path.relative_to(root).as_posix())
                      for root in roots)
    if allowed:
        if path.is_relative_to(Path.cwd().resolve()):
            _refuse_secret(path, Path.cwd().resolve(), value)
        if not path.is_file():
            raise ValueError("source path must be an existing regular file")
        return path
    raise ValueError("refusing %s: single retrieval reads only regular files inside the current workspace "
                     "or Ork Meter's own tool-output store; use Claude's Read tool for other files so your "
                     "permission rules apply" % value)


def main(argv=None):
    if (sys.argv[1:] if argv is None else argv) == ["--scan-worker"]:
        return _scan_worker()
    # No abbreviations: "--work" must not be another spelling of --workspace.
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("path", nargs="?")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--outline", action="store_true")
    action.add_argument("--symbol")
    action.add_argument("--batch", action=_Once, help="JSON array of independent source requests; requires --workspace")
    action.add_argument("--related", metavar="SYMBOL", help="exact symbol plus bounded static workspace dependencies")
    action.add_argument("--search", action=_Once, metavar="QUERY", help="ranked workspace search with exact snippets; requires --workspace")
    parser.add_argument("--workspace", action=_Once, help="explicit task workspace for batch, related or search retrieval")
    parser.add_argument("--budget", type=int, help="maximum serialized output bytes (batch and related 24 KiB, "
                        "search 12 KiB by default)")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--expected-hash")
    parser.add_argument("--hops", type=int, default=1, help="related dependency hops, 0..2")
    parser.add_argument("--fanout", type=int, default=8, help="maximum related source items, 1..16")
    parser.add_argument("--references", nargs="*", default=[], help="up to eight explicit Python reference/test files")
    parser.add_argument("--regex", action="store_true", help="search: QUERY is a Python regular expression")
    parser.add_argument("--ignore-case", action="store_true", help="search: case-insensitive matching")
    parser.add_argument("--glob", action="append", default=[], help="search: only paths matching this glob (repeatable)")
    parser.add_argument("--context", type=int, help="search: exact lines around each hit, 0..5 (default 1)")
    parser.add_argument("--per-file", type=int, help="search: hits returned per file, 1..100 (default 8)")
    parser.add_argument("--json", action="store_true", help="search: JSON output instead of the compact text")
    args = parser.parse_args(argv)
    if args.search is not None and (args.path is not None or args.workspace is None or args.expected_hash is not None
                                   or args.start != 1 or args.limit != 100):
        parser.error("--search requires --workspace and takes no path, --expected-hash, --start or --limit")
    if args.search is None and (args.regex or args.ignore_case or args.glob or args.context is not None
                                or args.per_file is not None or args.json):
        parser.error("--regex, --ignore-case, --glob, --context, --per-file and --json apply only to --search")
    if args.batch is not None and (args.path is not None or args.workspace is None or args.expected_hash is not None
                                  or args.start != 1 or args.limit != 100):
        parser.error("--batch requires --workspace and places each source selection in its request object")
    if args.related is not None and (args.path is None or args.workspace is None or args.start != 1 or args.limit != 100):
        parser.error("--related requires a path, exact symbol and explicit --workspace")
    if args.related is None and (args.hops != 1 or args.fanout != 8 or args.references):
        parser.error("--hops, --fanout and --references apply only to --related")
    if args.batch is None and args.related is None and args.search is None and (args.path is None or args.workspace is not None):
        parser.error("single retrieval requires a path; --workspace is for batch, related or search retrieval")
    if args.budget is None:
        args.budget = SEARCH_BUDGET if args.search is not None else 24 * 1024
    try:
        if args.search is not None:
            result = search_workspace(args.workspace, args.search, regex=args.regex, ignore_case=args.ignore_case,
                                      globs=args.glob, context=1 if args.context is None else args.context,
                                      per_file=8 if args.per_file is None else args.per_file, budget=args.budget,
                                      text=not args.json)
            sys.stdout.buffer.write((result if isinstance(result, str) else _serialized(result)).encode("utf-8"))
            sys.stdout.buffer.flush()
            return 0
        if args.related is not None:
            result = source_context.related(args.path, args.related, args.workspace, hops=args.hops, fanout=args.fanout,
                                            references=args.references, expected_hash=args.expected_hash, budget=args.budget,
                                            guard=_refuse_secret)
            sys.stdout.buffer.write(_serialized(result).encode("utf-8"))
            sys.stdout.buffer.flush()
            return 0
        elif args.batch is not None:
            if len(args.batch.encode("utf-8")) > 64 * 1024:
                raise ValueError("batch request JSON exceeds 64 KiB")
            result = batch(json.loads(args.batch), args.workspace, args.budget)
            # UTF-8 bytes keep the budget exact on Windows too: no locale
            # encoding failures or LF-to-CRLF expansion at the transport edge.
            encoded = _serialized(result).encode("utf-8")
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()
            return 0
        result = retrieve(single_path(args.path), args.symbol, args.outline, args.start, args.limit, args.expected_hash)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, SyntaxError) as exc:
        parser.exit(1, "ork-meter: %s\n" % exc)


if __name__ == "__main__":
    sys.exit(main())
