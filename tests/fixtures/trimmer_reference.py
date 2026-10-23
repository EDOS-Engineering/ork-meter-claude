"""Tool-output trimming: pure functions over text, no I/O, no environment.

The hook scripts call ``trim_text`` (head/tail elision for Bash, Grep, Glob
and WebFetch text), ``trim_list`` (Glob and Grep file lists), ``trim_head``
(Read: head only, so the line numbers Claude Code prints stay right) and
``filter_test_output`` (test-runner noise). Every function is deterministic
and keeps error-looking lines, so a trimmed result still shows what failed.
Budgets are arguments; hooks/lib/features.py declares them.
"""

import re
import shlex
from evidence_formats import unsupported_line_format as _unsupported_line_format


LINE_CAP = 2000            # chars kept of any single line
REPEAT_MIN = 3             # identical consecutive lines collapsed at this run length
HEAD_SHARE = 0.6           # head gets 60% of the budget, tail 40%
FIRST_LINES = 15           # lines shown for a repeated identical Read

ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"           # CSI sequences (colours, cursor moves)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (titles, hyperlinks)
    r"|\x1b[@-Z\\-_]"                    # two-byte escapes
)
ERROR_RE = re.compile(
    r"error|fail|panic|traceback|exception|fatal|denied|timeout|exit code", re.IGNORECASE
)

# Lines that only say a test passed. Only these are removed by the filter;
# anything the filter can't classify stays, so failures keep their context.
PASS_LINE_RE = re.compile(
    r"^\s*(?:"
    r"=== (?:RUN|PAUSE|CONT)\s+\S+"                      # go test -v
    r"|--- PASS: .*"                                      # go test -v
    r"|ok\s+\S+\s+(?:\(cached\)|[\d.]+s)(?:\s+coverage:.*)?"  # go package summary
    r"|\S+\.py(?:::\S+)?\s+PASSED(?:\s+\[\s*\d+%\])?"    # pytest -v
    r"|\S+\.py\s+[.]+(?:\s+\[\s*\d+%\])?"                 # pytest passing progress only
    r"|PASSED \S+\.py::\S+"                                 # pytest -rA short test summary
    r"|test \S+ \.\.\. ok"                                # cargo passing assertions only
    r"|(?:✓|✔|√)\s.*"                                    # jest, vitest, mocha
    r"|PASS\s+\S+(?:\s+\([\d.]+\s*m?s\))?"               # jest suite line
    r"|\S+(?: > \S+)* PASSED"                            # gradle
    r"|\[INFO\] Tests run: \d+, Failures: 0, Errors: 0, Skipped: 0(?:\D.*)?"  # maven, no skipped coverage
    r")\s*$"
)
PYTEST_PLAIN_PASS_RE = re.compile(
    r"^\s*\S+\.py(?:::[^\s\[\]]+)+\s+PASSED(?:\s+\[\s*\d+%\])?\s*$"
)
PYTEST_NAMED_PASS_RE = re.compile(r"^\s*\S+\.py::\S+\s+PASSED(?:\s+\[\s*\d+%\])?\s*$")
PYTEST_SUMMARY_PASS_RE = re.compile(r"^PASSED \S+\.py::\S+\s*$")
MAX_PLAIN_PYTEST_PASSES = 8
GO_PASS_RE = re.compile(r"^\s*--- PASS:\s+(.+?)\s+\([^)]*\)\s*$")
GO_ACTIVITY_RE = re.compile(r"^\s*=== (?:RUN|PAUSE|CONT)\s+(.+?)\s*$")
MIN_FILTER_LINES = 3       # don't bother noting fewer omitted lines than this
SKIP_LINE_RE = re.compile(r"\b(?:skip(?:ped|s|ping)?|ignored|xfail(?:ed)?|xpass(?:ed)?|pending|todo)\b", re.IGNORECASE)


def nbytes(text):
    return len(text.encode("utf-8", "replace"))


def strip_ansi(text):
    return ANSI_RE.sub("", text)


def mandatory_line(text):
    """Keep failure diagnostics and evidence of skipped/unexecuted coverage."""
    return ERROR_RE.search(text) is not None or SKIP_LINE_RE.search(text) is not None


def unsupported_line_format(text):
    """Conservative guard; generic line elision cannot preserve these formats.

    Inspect syntax without parsing/re-serializing structured values. Broken or
    partial JSON-like payloads also pass through. Bracketed log tags such as
    [build] remain eligible; scalar numbers, JSON/JSONL, markup and binary-like
    received text need their own schema-aware reducer.
    """
    return _unsupported_line_format(text, allow_log_tags=True)


def normalise(text):
    """ANSI stripped, trailing whitespace removed, repeats collapsed,
    long lines capped. Returns the list of lines."""
    lines = strip_ansi(text).split("\n")
    lines = [line.rstrip() for line in lines]
    protected = _diagnostic_indices(lines)
    out = []
    i = 0
    n = len(lines)
    while i < n:
        j = i + 1
        while j < n and lines[j] == lines[i] and i not in protected and j not in protected:
            j += 1
        run = j - i
        out.append(lines[i])
        if run >= REPEAT_MIN and not mandatory_line(lines[i]):
            out.append("… (repeated %d×)" % run)
        else:
            out.extend(lines[i + 1:j])
        i = j
    capped = []
    protected = _diagnostic_indices(out)
    for idx, line in enumerate(out):
        if len(line) > LINE_CAP and idx not in protected:
            capped.append(line[:LINE_CAP] + "… [+%d chars]" % (len(line) - LINE_CAP))
        else:
            capped.append(line)
    return capped


def error_excerpt(line):
    """Complete diagnostic; mandatory evidence is allowed to exceed a budget."""
    return line


def _sizes(lines):
    return [nbytes(line) + 1 for line in lines]


def _split(sizes, budget):
    """Indices (i, j): keep lines[:i] and lines[j:], elide lines[i:j]."""
    n = len(sizes)
    head_budget = int(budget * HEAD_SHARE)
    tail_budget = budget - head_budget
    i = 0
    used = 0
    while i < n and used + sizes[i] <= head_budget:
        used += sizes[i]
        i += 1
    j = n
    used = 0
    while j > i and used + sizes[j - 1] <= tail_budget:
        used += sizes[j - 1]
        j -= 1
    return i, j


def _error_lines(lines, start, end, numbered=False):
    kept = []
    for idx in range(start, end):
        line = lines[idx]
        if mandatory_line(line):
            line = error_excerpt(line)
            kept.append("L%d: %s" % (idx + 1, line) if numbered else line)
    return kept


def _small_pytest_indices(lines):
    named = {i for i, line in enumerate(lines)
             if PYTEST_NAMED_PASS_RE.fullmatch(line) or PYTEST_SUMMARY_PASS_RE.fullmatch(line)}
    if len(named) <= MAX_PLAIN_PYTEST_PASSES:
        return named
    plain = {i for i in named if PYTEST_PLAIN_PASS_RE.fullmatch(lines[i])}
    return plain if len(plain) <= MAX_PLAIN_PYTEST_PASSES else set()


def _diagnostic_indices(lines):
    """Keep bounded neighboring evidence, including locations and expected values."""
    kept = _small_pytest_indices(lines)
    for idx, line in enumerate(lines):
        if mandatory_line(line):
            kept.update(range(max(0, idx - 3), min(len(lines), idx + 9)))
    return kept


def trim_text(text, budget, retain_errors=True, do_normalise=True):
    """Head/tail elision of ``text`` to about ``budget`` bytes.

    Returns ``(new_text, info)``; ``info`` is None when nothing changed.
    The marker between head and tail states what was elided, and
    error-looking lines from the elided range are kept under it.
    """
    if unsupported_line_format(text):
        return text, None
    before = nbytes(text)
    if before <= budget:
        return text, None
    lines = normalise(text) if do_normalise else text.split("\n")
    normalised = "\n".join(lines)
    info = {"bytes_before": before, "lines_elided": 0, "bytes_elided": 0, "error_lines_kept": 0}
    if nbytes(normalised) <= budget:
        info["bytes_after"] = nbytes(normalised)
        return normalised, info
    sizes = _sizes(lines)
    marker_reserve = 320
    i, j = _split(sizes, max(512, budget - marker_reserve))
    protected = _diagnostic_indices(lines) if retain_errors else set()
    errors = [lines[idx] for idx in range(i, j) if idx in protected]
    reserve = marker_reserve + sum(nbytes(e) + 1 for e in errors)
    i, j = _split(sizes, max(budget // 2, budget - reserve))
    errors = [lines[idx] for idx in range(i, j) if idx in protected]
    diagnostic_count = sum(mandatory_line(line) for line in errors)
    omitted = [lines[idx] for idx in range(i, j) if idx not in protected]
    elided_bytes = sum(nbytes(line) + 1 for line in omitted)
    marker = ["… [Ork Meter trimmed %d normalized lines (%d bytes) here]" % (len(omitted), elided_bytes)]
    if errors:
        marker[0] = "… [Ork Meter trimmed %d normalized lines (%d bytes) here; %d diagnostic/context lines from that range follow]" % (
            len(omitted), elided_bytes, len(errors))
        marker.extend(errors)
        marker.append("… [end of trimmed range]")
    out = lines[:i] + marker + lines[j:]
    new_text = "\n".join(out)
    if nbytes(new_text) >= before:
        return text, None
    info.update({"lines_elided": len(omitted), "bytes_elided": elided_bytes,
                 "error_lines_kept": diagnostic_count,
                 "context_lines_kept": len(errors) - diagnostic_count, "bytes_after": nbytes(new_text)})
    return new_text, info


def trim_list(items, budget):
    """Head/tail elision of a list of strings (file paths)."""
    if not all(isinstance(item, str) for item in items):
        return items, None
    before = sum(nbytes(item) + 1 for item in items)
    if before <= budget:
        return items, None
    sizes = _sizes(items)
    i, j = _split(sizes, max(512, budget - 160))
    elided = items[i:j]
    marker = "… [Ork Meter trimmed %d entries (%d bytes) here]" % (len(elided), sum(sizes[i:j]))
    out = items[:i] + [marker] + items[j:]
    info = {"bytes_before": before, "lines_elided": len(elided), "bytes_elided": sum(sizes[i:j]),
            "error_lines_kept": 0, "bytes_after": sum(nbytes(item) + 1 for item in out)}
    return out, info


def trim_head(text, budget, start_line=1):
    """Keep whole Read lines within ``budget``, or a prefix of an oversized first line.

    Returns ``(head_text, info)``. ``info`` is None when nothing changed;
    otherwise it names the first and last elided line (numbered as the
    file is numbered, from ``start_line``) and the error-looking lines in
    the elided range with their real line numbers.
    """
    before = nbytes(text)
    if before <= budget:
        return text, None
    lines = text.split("\n")
    sizes = _sizes(lines)
    i = 0
    used = 0
    while i < len(lines) and used + sizes[i] <= budget:
        used += sizes[i]
        i += 1
    if i >= len(lines):
        return text, None
    kept = lines[:i]
    partial_line = i == 0
    if partial_line:
        # A prefix of one oversized line is preferable to an unbounded result.
        kept = [lines[0].encode("utf-8", "replace")[:budget].decode("utf-8", "ignore")]
    errors = []
    for idx in range(i, len(lines)):
        line = lines[idx]
        if mandatory_line(line):
            line = error_excerpt(line)
            errors.append("line %d: %s" % (start_line + idx, line))
    head = "\n".join(kept)
    info = {
        "bytes_before": before,
        "bytes_after": nbytes(head),
        "lines_kept": len(kept),
        "partial_line": partial_line,
        "lines_elided": len(lines) - i,
        "bytes_elided": before - nbytes(head),
        "first_elided_line": start_line + i,
        "last_elided_line": start_line + len(lines) - 1,
        "error_lines": errors,
        "error_lines_kept": len(errors),
    }
    return head, info


def is_test_command(command):
    if not isinstance(command, str):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()`\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return False
    if not words:
        return False
    executable = words[0].rsplit("/", 1)[-1].lower()
    if executable in {"bash", "sh", "zsh"} and len(words) == 3 and words[1] in {"-c", "-lc", "-ec"}:
        return is_test_command(words[2])
    if words[0] == "cd" and len(words) >= 4 and words[2] == "&&":
        words = words[3:]
        executable = words[0].rsplit("/", 1)[-1].lower()
    if any(re.fullmatch(r"[;&|<>()`\n]+", word) or "$(" in word or "`" in word for word in words):
        return False
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", executable):
        position = 1
        while position < len(words) and words[position] in {"-B", "-I", "-S", "-E", "-s", "-u", "-q"}:
            position += 1
        if words[position:position + 1] != ["-m"]:
            return False
        words = words[position + 1:]
        executable = words[0].lower() if words else ""
    if executable == "npx" and len(words) > 1:
        words = words[1:]
        executable = words[0].lower()
    tail = words[1:]
    if executable in {"pytest", "pytest.exe", "py.test", "jest", "vitest", "mocha", "unittest"}:
        return True
    if executable == "go":
        return tail[:1] == ["test"]
    if executable == "cargo":
        return tail[:1] == ["test"] or tail[:2] == ["nextest", "run"]
    if executable in {"npm", "yarn", "pnpm", "bun"}:
        return tail[:1] in (["test"], ["t"]) or tail[:2] in (["run", "test"], ["run", "t"])
    if executable in {"mvn", "mvnw", "gradle", "gradlew"}:
        return "test" in tail
    return False


def filter_test_output(text):
    """Drop lines that only report a passing test. Returns ``(text, omitted)``.

    Nothing else is removed: failure blocks, errors, summaries and anything
    unrecognised stay. JSON output (``go test -json``) is left alone.
    """
    if unsupported_line_format(text):
        return text, 0
    lines = strip_ansi(text).split("\n")
    first = next((line for line in lines if line.strip()), "")
    if first.lstrip().startswith("{"):
        return text, 0
    # A small set can confirm a fix, including parameterized cases. Preserve
    # those names through both filtering and subsequent budget trimming.
    kept_pytest = _small_pytest_indices(lines)
    # An active test is diagnostic evidence if the process fails or crashes
    # before reporting its outcome. Only passed tests have routine activity.
    pending_go_activity = {}
    passed_go_activity = set()
    for i, line in enumerate(lines):
        activity = GO_ACTIVITY_RE.fullmatch(line)
        passed = GO_PASS_RE.fullmatch(line)
        if activity:
            pending_go_activity.setdefault(activity.group(1), []).append(i)
        elif passed:
            passed_go_activity.update(pending_go_activity.pop(passed.group(1), ()))
    out = []
    omitted = 0
    collapsed = False
    for i, line in enumerate(lines):
        skip_evidence = SKIP_LINE_RE.search(re.sub(r"\bSkipped:\s*0\b", "", line))
        activity = GO_ACTIVITY_RE.fullmatch(line)
        if (PASS_LINE_RE.match(line) and not skip_evidence
                and not (activity and i not in passed_go_activity)
                and i not in kept_pytest):
            omitted += 1
            continue
        if not line.strip() and out and not out[-1].strip():
            collapsed = True
            continue
        out.append(line)
    if omitted < MIN_FILTER_LINES:
        return text, 0
    out.append(filter_note(omitted, strip_ansi(text) != text, collapsed,
                           any(SUMMARY_LINE_RE.search(line) for line in out)))
    return "\n".join(out), omitted


# The filter's note is complete in itself: it names what went and does not
# send Claude to the saved original (B1, #212: the old "recover the saved
# original" wording cost a confirming read of output already shown). It claims
# only what holds: every other line shown, with ANSI colour codes removed or
# repeated blank lines collapsed when that happened, "including the summary"
# only when a summary line is present. The full output stays saved and named
# by the caller's "Full output" line.
FILTER_NOTE_RE = re.compile(r"… \[Ork Meter test filter: (\d+) passing-test lines omitted; [^\]\n]*\]")
SUMMARY_LINE_RE = re.compile(
    r"\b\d+ (?:passed|failed|errors?|skipped)\b"                   # pytest, mocha
    r"|^(?:Tests|Test Suites):\s"                                 # jest, vitest
    r"|^test result: "                                            # cargo
    r"|^\[INFO\] Tests run: \d+"                                  # maven
    r"|^(?:ok|FAIL|PASS)(?:\s|$)"                                  # go
)


def filter_note(omitted, ansi=False, collapsed=False, summary=False):
    changes = [change for change, done in (("colour codes removed", ansi), ("repeated blank lines collapsed", collapsed))
               if done]
    return ("… [Ork Meter test filter: %d passing-test lines omitted; every other line is shown%s%s]"
            % (omitted, " (%s)" % ", ".join(changes) if changes else "", ", including the summary" if summary else ""))


def after_trim(text):
    """``text`` (filtered, then trimmed further, stdout or stderr) with the
    filter's note no longer claiming that every other line is shown."""
    return FILTER_NOTE_RE.sub(
        lambda match: "… [Ork Meter test filter: %s passing-test lines omitted; other lines trimmed where marked]"
        % match.group(1), text)
