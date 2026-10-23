"""Read compaction: elide whole lines of a whole-file Read result, never rewrite one.

Pure: no I/O and no environment. ``compact(text, path, budget, counter)``
works on the ``content`` of a Read result, which is the file's raw text;
Claude Code adds the line numbers when it renders the result, numbering
every line of ``content`` from ``startLine`` (``N<TAB>line``, observed with
2.1.302). A replacement can't carry its own numbers, so an elided range
keeps its line slots:

* its first slot holds one marker, ``… lines A–B not shown (<what>; Read offset=A
  limit=N before editing)``;
* its other slots are empty, so every later shown line keeps its number;
* a range that runs to the end of the file is its marker alone.

Every other line is the original line, byte for byte, at its original
number. Nothing is ever cut inside a line. An empty slot still costs its
number (about two tokens), so a range is elided only where that saves
tokens, and every count here is of the text as Claude Code renders it.

Candidates, in priority order; each stage is applied range by range,
deterministically, until the rendered result fits ``budget`` tokens:

1. a licence or copyright header block (its first line kept);
2. long comment blocks and Python docstrings (their first line kept);
3. long data literals and runs of data lines (their first line kept), and
   single lines over ``LONG_LINE`` characters (minified or generated text);
4. bodies of large functions and classes, farthest from the top first,
   keeping the signature: from the AST for Python; for brace languages a
   conservative brace and indentation check that skips any block it can't
   delimit with certainty (strings or comments that could hide a brace, a
   closing brace at another indentation); no other language.

Runs of blank lines between two elided ranges join them. JSON is never
compacted (``compactable``). The Write guard (hooks/write-guard.py) uses
``unread`` to refuse a Write that would delete elided lines unread. The first
``HEAD_KEEP`` lines (except a licence block) and the lines around a likely
point of interest (``TODO``, ``FIXME``, ``XXX``, ``HACK``, ``BUG`` and merge
conflict markers, ``INTEREST_CONTEXT`` lines either side) are never elided.
"""

import ast
import gc
import re

# The largest Read (rendered tokens, o200k_base) the read guard lets through
# whole for compaction: Claude Code pages or refuses a whole-file Read over
# its own cap (25,000 tokens by its count in 2.1.302, which is not this
# tokenizer's), so this keeps a margin under it.
DEFER_MAX_TOKENS = 20000
HEAD_KEEP = 40
INTEREST_CONTEXT = 2
MIN_COMMENT_BLOCK = 6     # lines; a shorter comment block stays whole
MIN_DATA_RUN = 12         # lines of a data literal or a run of data lines
MIN_BODY = 8              # lines of a function or class body
LONG_LINE = 1000          # characters: a minified or generated line
LICENCE_SCAN = 80         # a licence header starts within these lines
MARKER = "… lines %d–%d not shown (%s; Read offset=%d limit=%d before editing)"
INTEREST_RE = re.compile(r"\b(?:TODO|FIXME|XXX|HACK|BUG)\b|^(?:<{7}|={7}|>{7})(?:\s|$)")
LICENCE_RE = re.compile(r"copyright|licen[cs]e|spdx-license-identifier|all rights reserved|permission is hereby granted",
                        re.IGNORECASE)

HASH = ("#",)
SLASH = ("//",)
COMMENT_PREFIXES = {
    **{ext: HASH for ext in (".py", ".pyi", ".sh", ".bash", ".zsh", ".rb", ".pl", ".pm", ".yaml", ".yml",
                             ".toml", ".r", ".cfg", ".conf", ".ps1", ".cmake", ".tf", ".nix", ".ex", ".exs")},
    **{ext: SLASH for ext in (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".m", ".mm", ".cs", ".java",
                              ".kt", ".kts", ".scala", ".go", ".rs", ".swift", ".js", ".jsx", ".mjs", ".cjs",
                              ".ts", ".tsx", ".mts", ".cts", ".dart", ".php", ".groovy", ".proto", ".zig")},
    **{ext: ("--",) for ext in (".sql", ".lua", ".hs")},
    **{ext: (";",) for ext in (".lisp", ".clj", ".el", ".scm", ".asm", ".ini")},
    **{ext: ("%",) for ext in (".tex", ".erl")},
}
# JSON is all structure: eliding runs of its lines keeps a run's first line
# and drops the keys and nesting that make the rest usable, so a JSON Read is
# never compacted (the read guard and the Read budget handle it as before).
# Notebooks are not text Reads at all.
SKIPPED_EXTS = frozenset((".json", ".jsonl", ".ndjson", ".geojson", ".ipynb"))
# Languages whose backtick strings (template literals, Go raw strings) span lines.
BACKTICK_STRINGS = frozenset((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".go"))
BLOCK_COMMENTS = frozenset(ext for ext, prefixes in COMMENT_PREFIXES.items() if prefixes == SLASH) | {".css", ".scss", ".less"}
BRACE_LANGUAGES = BLOCK_COMMENTS - {".css", ".scss", ".less", ".proto"}
DEFINITION_RE = re.compile(
    r"^(?:(?:export|public|private|protected|internal|static|async|override|final|abstract|sealed|open|"
    r"pub(?:\([\w:]+\))?|unsafe|inline|virtual|default|extern|const|suspend|data|partial)\s+)*"
    r"(?:(?P<keyword>function\*?|func|fn|class|struct|impl|interface|enum|trait|namespace|object|module|union)\b"
    r"|(?P<call>.*\)\s*[^;=]*$)|(?P<arrow>.*=>\s*\{$))")
CONTROL_RE = re.compile(r"^(?:\}\s*)?(?:if|else|for|foreach|while|do|switch|try|catch|finally|return|case|match|"
                        r"select|loop|unless|until|with|defer|go|synchronized|using|lock)\b")
NAME_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*(?:<[^()]*>)?\s*\(")
KEYWORD_NAME_RE = re.compile(r"\b(?:function\*?|func|fn|class|struct|impl|interface|enum|trait|namespace|object|"
                             r"module|union)\s+(?:<[^>]*>\s*)?([A-Za-z_$][\w$:.<>]*)")
STRING_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\\n]|\\.)*`')
NUMBER_RE = re.compile(r"[-+]?(?:0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*(?:\.\d*)?(?:[eE][-+]?\d+)?)[lLuUfFdDjJn]*\b")
DATA_WORDS_RE = re.compile(r"\b(?:true|false|null|nil|None|True|False|undefined|NaN)\b")
DATA_REST = frozenset(" \t,:;[]{}()=>")


class Range:
    """Lines ``start``..``end`` (0-based, inclusive) elided: a ``kind``
    (licence, comment, data, line or body), described by ``what``."""
    __slots__ = ("start", "end", "kind", "what")

    def __init__(self, start, end, kind, what):
        self.start, self.end, self.kind, self.what = start, end, kind, what

    def __repr__(self):
        return "Range(%d, %d, %r, %r)" % (self.start, self.end, self.kind, self.what)


def rendered_line(number, line):
    """One line as Claude Code renders a Read result: ``N<TAB>line``, a
    trailing carriage return dropped."""
    return "%d\t%s" % (number, line[:-1] if line.endswith("\r") else line)


def render(lines, start_line=1):
    """``lines`` (the ``content`` split on newlines) as the model sees them."""
    return "\n".join(rendered_line(number, line) for number, line in enumerate(lines, start_line))


class Costs:
    """Tokens of each rendered line (its newline included, except the last's),
    of each line's empty slot and of a marker, with prefix sums. Every
    rendered line starts with its number, so no pre-tokenization piece
    crosses a line break and the lines' counts add up to the count of the
    whole rendering."""

    def __init__(self, lines, counter):
        self.count = counter
        last = len(lines) - 1
        full = [counter(rendered_line(i + 1, line) + ("\n" if i < last else "")) for i, line in enumerate(lines)]
        widths = {}
        blank = []
        for i in range(len(lines)):
            width = len(str(i + 1))
            if width not in widths:
                widths[width] = counter("%d\t\n" % (i + 1))
            blank.append(widths[width])
        self.full_sum = [0]
        self.blank_sum = [0]
        for a, b in zip(full, blank):
            self.full_sum.append(self.full_sum[-1] + a)
            self.blank_sum.append(self.blank_sum[-1] + b)
        self.total = self.full_sum[-1]

    def full(self, first, last):
        return self.full_sum[last + 1] - self.full_sum[first]

    def blanks(self, first, last):
        return self.blank_sum[last + 1] - self.blank_sum[first] if last >= first else 0

    def marker(self, rng):
        return self.count(rendered_line(rng.start + 1, marker_text(rng)) + "\n")


def marker_text(rng):
    first, last = rng.start + 1, rng.end + 1
    return MARKER % (first, last, rng.what, first, last - first + 1)


# --- protection ------------------------------------------------------------

class Protected:
    """Lines never elided: the head and the lines around a likely point of
    interest. ``any(first, last)`` asks about a range in constant time."""

    def __init__(self, lines):
        interest = [False] * len(lines)
        for index, line in enumerate(lines):
            if INTEREST_RE.search(line.strip()):
                for near in range(max(0, index - INTEREST_CONTEXT), min(len(lines), index + INTEREST_CONTEXT + 1)):
                    interest[near] = True
        self.prefix, self.interest = [0], [0]
        for index, flag in enumerate(interest):
            self.prefix.append(self.prefix[-1] + (flag or index < HEAD_KEEP))
            self.interest.append(self.interest[-1] + flag)

    def any(self, first, last):
        return self.prefix[last + 1] - self.prefix[first] > 0

    def interesting(self, first, last):
        """Whether ``first``..``last`` holds a line around a point of interest."""
        return self.interest[last + 1] - self.interest[first] > 0


# --- candidates --------------------------------------------------------------

def backtick_strings(lines, ext):
    """Indices of lines inside a multi-line backtick string (a template
    literal, a Go raw string): every line after the one that opens it, to
    the one that closes it. Empty for other languages."""
    inside = set()
    if ext not in BACKTICK_STRINGS:
        return inside
    open_ = False
    for index, line in enumerate(lines):
        if open_:
            inside.add(index)
        ticks = line.replace("\\`", "").count("`")
        if ticks % 2:
            open_ = not open_
    return inside


def comment_lines(lines, ext, strings=frozenset()):
    """Indices of whole-line comments for ``ext``'s language (empty when
    unknown), leaving out lines inside multi-line ``strings``."""
    prefixes = COMMENT_PREFIXES.get(ext, ())
    found = set()
    for index, line in enumerate(lines):
        if index in strings:
            continue
        stripped = line.strip()
        if stripped and any(stripped.startswith(prefix) for prefix in prefixes) and not stripped.startswith("#!"):
            found.add(index)
    if ext in BLOCK_COMMENTS:
        index = 0
        while index < len(lines):
            stripped = lines[index].strip()
            if index in strings:
                index += 1
                continue
            if stripped.startswith("/*") and "*/" not in stripped[2:]:
                end = index + 1
                while end < len(lines) and "*/" not in lines[end]:
                    end += 1
                if end < len(lines) and lines[end].rstrip().endswith("*/") and lines[end].count("*/") == 1:
                    found.update(range(index, end + 1))
                index = end + 1
                continue
            if stripped.startswith("/*") and stripped.endswith("*/"):
                found.add(index)
            index += 1
    return found


def runs(indices):
    """Maximal runs of consecutive indices, as (first, last) in order."""
    out = []
    for index in sorted(indices):
        if out and out[-1][1] == index - 1:
            out[-1][1] = index
        else:
            out.append([index, index])
    return [tuple(run) for run in out]


def is_data_line(line):
    """Only literals, separators and brackets: an element of a data literal."""
    stripped = line.strip()
    if not stripped:
        return False
    rest = DATA_WORDS_RE.sub(" ", NUMBER_RE.sub(" ", STRING_RE.sub(" ", stripped)))
    return set(rest) <= DATA_REST and (rest.strip(" \t") != "" or stripped[0] in "\"'`-+0123456789")


class PythonFacts:
    """What the AST says: definitions, docstrings, data literals and the
    lines inside multi-line strings. ``None`` attributes when the text
    doesn't parse, or its line breaks aren't the ones ``split("\\n")`` sees."""

    def __init__(self, text):
        self.definitions, self.docstrings, self.literals, self.in_strings = [], [], [], set()
        self.strings = []   # multi-line string nodes that are neither docstrings nor assigned literals
        self.ok = False
        if "\r" in text.replace("\r\n", "") or "\x0c" in text or "\x0b" in text:
            return
        collecting = gc.isenabled()
        gc.disable()   # an AST is acyclic; the collector only rescans it (#190)
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return
        finally:
            if collecting:
                gc.enable()
        self.ok = True
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.definitions.append(node)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = node.body
                if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    self.docstrings.append(body[0])
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and isinstance(
                    node.value, (ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Constant, ast.JoinedStr)):
                self.literals.append(node)
            if isinstance(node, (ast.Constant, ast.JoinedStr)) and node.end_lineno > node.lineno and (
                    not isinstance(node, ast.Constant) or isinstance(node.value, (str, bytes))):
                self.in_strings.update(range(node.lineno - 1, node.end_lineno))
                self.strings.append(node)
        claimed = {id(doc.value) for doc in self.docstrings} | {id(node.value) for node in self.literals}
        self.strings = [node for node in self.strings if id(node) not in claimed]


def licence_range(lines, comments, facts):
    """The first licence or copyright block within ``LICENCE_SCAN`` lines, its first line kept."""
    blocks = [run for run in runs(comments) if run[0] < LICENCE_SCAN]
    if facts is not None and facts.ok:
        blocks += [(node.lineno - 1, node.end_lineno - 1) for node in facts.docstrings
                   if node.lineno - 1 < LICENCE_SCAN]
    for first, last in sorted(blocks):
        if last - first >= 3 and LICENCE_RE.search("\n".join(lines[first:last + 1])):
            return Range(first + 1, last, "licence", "licence header")
    return None


def comment_ranges(lines, comments, facts, backticks=frozenset()):
    """Comment blocks, docstrings and multi-line strings (labelled
    ``string``, never ``comment``), each with its first line kept."""
    found = []
    strings = facts.in_strings if facts is not None and facts.ok else set()
    if facts is not None and facts.ok:
        for node in facts.strings:
            if node.end_lineno - node.lineno + 1 >= MIN_COMMENT_BLOCK:
                found.append(Range(node.lineno, node.end_lineno - 1, "comment", "string"))
    for first, last in runs(backticks):
        # ``first`` is the line after the opening one, which stays shown.
        if last - first + 2 >= MIN_COMMENT_BLOCK:
            found.append(Range(first, last, "comment", "string"))
    for first, last in runs(index for index in comments if index not in strings):
        if last - first + 1 >= MIN_COMMENT_BLOCK:
            found.append(Range(first + 1, last, "comment", "comment"))
    if facts is not None and facts.ok:
        for node in facts.docstrings:
            if node.end_lineno - node.lineno + 1 >= MIN_COMMENT_BLOCK:
                found.append(Range(node.lineno, node.end_lineno - 1, "comment",
                                   "docstring"))
    return found


def data_ranges(lines, facts):
    found = []
    if facts is not None and facts.ok:
        for node in facts.literals:
            if node.end_lineno - node.lineno + 1 >= MIN_DATA_RUN:
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                name = target.id if isinstance(target, ast.Name) else "data"
                found.append(Range(node.lineno, node.end_lineno - 1, "data",
                                   "%s literal" % name))
        strings = facts.in_strings
    else:
        strings = set()
    for first, last in runs(i for i, line in enumerate(lines) if i not in strings and is_data_line(line)):
        if last - first + 1 >= MIN_DATA_RUN:
            found.append(Range(first + 1, last, "data", "data"))
    for index, line in enumerate(lines):
        if len(line) > LONG_LINE:
            found.append(Range(index, index, "line", "one line of %d characters" % len(line)))
    return found


def python_bodies(facts):
    found = []
    for node in facts.definitions:
        body_start = node.body[0].lineno   # 1-based: the first body line
        header_end = body_start - 1
        if header_end < node.lineno:
            continue   # a one-line definition: no separate body
        first = body_start - 1          # 0-based
        last = node.end_lineno - 1
        if last - first + 1 < MIN_BODY:
            continue
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        found.append(Range(first, last, "body", "%s %s body" % (kind, node.name)))
    return found


def brace_bodies(lines):
    """Bodies of definitions in a brace language whose extent is certain."""
    found = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.endswith("{") or CONTROL_RE.match(stripped) or not DEFINITION_RE.match(stripped):
            continue
        header = _code(stripped)
        if header is None or header.count("{") - header.count("}") != 1:
            continue
        indent = line[:len(line) - len(line.lstrip())]
        depth = 1
        end = None
        for later in range(index + 1, len(lines)):
            text = lines[later]
            code = _code(text.strip())
            if code is None:
                break
            if text.strip() and not text.startswith(indent):
                break
            depth += code.count("{") - code.count("}")
            if depth < 0:
                break
            if depth == 0:
                if text.strip().startswith("}") and text[:len(text) - len(text.lstrip())] == indent:
                    end = later
                break
            if text.strip() and len(text) - len(text.lstrip()) <= len(indent) and not text.lstrip().startswith("#"):
                break   # a body line no deeper than its header: not a block we can delimit
        if end is None or end - index - 1 < MIN_BODY:
            continue
        found.append(Range(index + 1, end - 1, "body", "%s body" % _name(stripped)))
    return found


def _code(text):
    """``text`` without its string literals and its line comment, or None
    when a quote, backtick or block comment leaves the line's extent of
    code uncertain."""
    code = STRING_RE.sub('""', text)
    if "//" in code:
        code = code[:code.index("//")]
    if any(mark in code.replace('""', "") for mark in ('"', "'", "`", "/*", "*/")):
        return None
    return code


def _name(header):
    match = KEYWORD_NAME_RE.search(header) or NAME_RE.search(header)
    return ("definition " + match.group(1)) if match else "block"


# --- selection ---------------------------------------------------------------

def merge(lines, ranges):
    """Sorted, disjoint ranges joined across runs of blank lines, the last
    one extended over trailing blank lines to the end of the file."""
    merged = []
    for rng in sorted(ranges, key=lambda r: (r.start, -r.end)):
        if merged and (rng.start <= merged[-1].end + 1 or all(
                not lines[i].strip() for i in range(merged[-1].end + 1, rng.start))):
            previous = merged[-1]
            if rng.end > previous.end:
                what = _joined(previous.what, rng.what)
                merged[-1] = Range(previous.start, rng.end, previous.kind, what)
            continue
        merged.append(Range(rng.start, rng.end, rng.kind, rng.what))
    if merged and all(not line.strip() for line in lines[merged[-1].end + 1:]):
        last = merged[-1]
        merged[-1] = Range(last.start, len(lines) - 1, last.kind, last.what)
    return merged


def _joined(first, second):
    """Two ranges' labels joined, at most two kinds named: a merged marker
    stays one short line however many ranges it joins."""
    parts = first.split("; ")
    more = parts[-1] == "more"
    parts = [part for part in parts if part != "more"]
    if second not in parts:
        parts.append(second)
    if len(parts) > 2:
        parts, more = parts[:2], True
    return "; ".join(parts + (["more"] if more else []))


def compactable(path):
    """Whether a Read of ``path`` may be compacted at all (not JSON or a notebook)."""
    return _extension(path) not in SKIPPED_EXTS


def gain(costs, rng, size):
    """Tokens saved by eliding ``rng`` alone: its lines, less its marker and
    empty slots (none after a marker that ends the file)."""
    slots = 0 if rng.end == size - 1 else costs.blanks(rng.start + 1, rng.end)
    return costs.full(rng.start, rng.end) - costs.marker(rng) - slots


def compact(text, path, budget, counter, deadline=None, now=None, margin=0):
    """``(lines, info)``: the content lines of a compaction of ``text`` whose
    rendering fits ``budget`` tokens and saves at least ``margin`` tokens,
    or None when ``text`` already fits ``budget`` or no compaction does.

    ``counter(text)`` counts tokens; past ``deadline`` (on the clock
    ``now``) the search stops with None. ``info``: ``tokens_before``, the
    rendered count of ``text``; ``tokens_after``, the count of the result,
    or the smallest reached when none fits; ``lines_elided``; ``ranges``;
    ``late`` when the deadline stopped it."""
    lines = text.split("\n")
    size = len(lines)
    costs = Costs(lines, counter)
    info = {"tokens_before": costs.total, "tokens_after": costs.total, "lines_elided": 0, "ranges": 0,
            "late": False}
    if costs.total <= budget:
        return None, info
    budget = min(budget, costs.total - margin)   # a saving the replacement's admission can accept

    def late():
        if deadline is not None and now is not None and now() >= deadline:
            info["late"] = True
            return True
        return False

    ext = _extension(path)
    if ext in SKIPPED_EXTS:
        return None, info
    facts = PythonFacts(text) if ext in (".py", ".pyi") else None
    if late():
        return None, info
    keep = Protected(lines)
    backticks = backtick_strings(lines, ext)
    comments = comment_lines(lines, ext, backticks)
    licence = licence_range(lines, comments, facts)
    if facts is not None and facts.ok:
        bodies = python_bodies(facts)
    elif ext in BRACE_LANGUAGES:
        bodies = brace_bodies(lines)
    else:
        bodies = []
    stages = [[licence] if licence else [], comment_ranges(lines, comments, facts, backticks), data_ranges(lines, facts),
              sorted(bodies, key=lambda r: (-r.start, r.end))]   # farthest from the top first, inner first

    chosen = []          # disjoint ranges, each with its own gain
    estimate = costs.total
    for number, stage in enumerate(stages):
        ordered = stage if number == 3 else sorted(stage, key=lambda r: (r.start, r.end))
        for rng in ordered:
            if late():
                info["tokens_after"] = estimate
                return None, info
            rng = _unprotected(rng, keep)
            if rng is None:
                continue
            inside = [item for item in chosen if rng.start <= item[0].start and item[0].end <= rng.end]
            if any(item[0].start <= rng.start and rng.end <= item[0].end for item in chosen):
                continue   # already elided
            if any(rng.start <= item[0].end and item[0].start <= rng.end for item in chosen if item not in inside):
                continue   # a partial overlap: never split a range
            saved = gain(costs, rng, size)
            replaced = sum(item[1] for item in inside)
            if saved <= replaced:
                continue   # its marker and empty slots cost at least what they hide
            chosen = [item for item in chosen if item not in inside] + [(rng, saved)]
            estimate -= saved - replaced
            if estimate <= budget:
                merged = merge(lines, [item[0] for item in chosen])
                out = shown_lines(lines, merged)
                exact = counter(render(out))
                info.update(tokens_after=exact, lines_elided=sum(r.end - r.start + 1 for r in merged),
                            ranges=len(merged), elided=[(r.start + 1, r.end + 1, r.what) for r in merged])
                if exact <= budget:
                    return out, info
                estimate = exact
    if chosen:
        merged = merge(lines, [item[0] for item in chosen])
        info.update(tokens_after=counter(render(shown_lines(lines, merged))),
                    lines_elided=sum(r.end - r.start + 1 for r in merged), ranges=len(merged))
    return None, info


# --- the Write guard (hooks/write-guard.py) ---------------------------------

def covered(intervals, first, last):
    """Whether the 1-based line ranges ``intervals`` together cover ``first``..``last``."""
    reach = first - 1
    for start, end in sorted(intervals):
        if start > reach + 1:
            break
        reach = max(reach, end)
        if reach >= last:
            return True
    return reach >= last


def unread(ranges, seen, original, disk, new):
    """The elided ``ranges`` ((first, last, what), 1-based) a Write of
    ``new`` would delete without the model having seen them: not covered by
    the ranged Reads ``seen`` since the compaction, their text (from
    ``original``, the compacted Read's content) still in the file on disk
    (``disk``) and not in ``new``. Line endings are compared as LF, so a
    range's text is found wherever an Edit has moved it."""
    def lf(text):
        return text.replace("\r\n", "\n")
    lines = lf(original).split("\n")
    disk, new = lf(disk), lf(new)
    found = []
    for first, last, what in ranges:
        if covered(seen, first, last):
            continue
        block = "\n".join(lines[first - 1:last])
        if block.strip() and block in disk and block not in new:
            found.append((first, last, what))
    return found


def describe(ranges, limit=10):
    """``A–B what`` for up to ``limit`` ranges, then how many more."""
    shown = ["%d\u2013%d %s" % (first, last, what) for first, last, what in ranges[:limit]]
    if len(ranges) > limit:
        shown.append("and %d more" % (len(ranges) - limit))
    return ", ".join(shown)


def shown_lines(lines, merged):
    """The content lines that replace ``lines`` with ``merged`` elided."""
    out = []
    position = 0
    for rng in merged:
        out.extend(lines[position:rng.start])
        out.append(marker_text(rng))
        if rng.end == len(lines) - 1:
            return out
        out.extend([""] * (rng.end - rng.start))
        position = rng.end + 1
    out.extend(lines[position:])
    return out


def _unprotected(rng, keep):
    """``rng``, or None when it would elide a protected line. A licence
    block may lie in the head, though not around a point of interest; a
    comment, docstring or data run that starts in the head loses its head
    part, so its first lines stay shown."""
    if rng.kind == "licence":
        if keep.interesting(rng.start, rng.end):
            return None
        return None if rng.end >= HEAD_KEEP and keep.any(max(rng.start, HEAD_KEEP), rng.end) else rng
    if rng.kind in ("comment", "data") and rng.start < HEAD_KEEP <= rng.end:
        rng = Range(HEAD_KEEP, rng.end, rng.kind, rng.what)
    if rng.end < rng.start or keep.any(rng.start, rng.end):
        return None
    return rng


def _extension(path):
    name = str(path or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    return name[name.rindex("."):] if "." in name[1:] else ""
