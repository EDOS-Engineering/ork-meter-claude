"""Helpers the Read hooks (read-guard.py, dedupe-reads.py) and scripts/source.py share.

Pure: no I/O and no environment (the outline reads only the clock, for its deadline). Kept apart from the scripts so a hook
imports neither argparse nor tokenize for them (#190).
"""
import ast
import gc
import time

OUTLINE_LIMIT = 48
OUTLINE_MIN_FILE = 24 * 1024
OUTLINE_MAX_FILE = 2 * 1024 * 1024
OUTLINE_MAX_BYTES = 4096
# Parsing costs about 0.5 ms per KiB of code on a laptop and several times
# that on a slow Windows runner, while comment and blank lines cost almost
# nothing. A module with at most this much code is outlined whatever is left
# of the budget: skipping it to save a millisecond made the outline depend on
# runner speed, not on the file (#140). Larger modules still need time left.
CHEAP_CODE_BYTES = 64 * 1024
CHEAP_PARSE_SECONDS = 0.25


def explicit_range(tool_input):
    """The Read asks for an exact range or a large file on purpose (``offset``,
    ``limit``, ``pages`` or ``allow_large``): the Read hooks leave it alone."""
    return (any(tool_input.get(field) is not None for field in ("offset", "limit", "pages"))
            or bool(tool_input.get("allow_large")))


def symbols(tree, prefix=""):
    """Classes and functions in a Python AST, nested names dotted, with the
    first line (decorators included) and the last line of each."""
    found = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + node.name
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            found.append({"name": name, "kind": type(node).__name__, "start": start, "end": node.end_lineno})
            found.extend(symbols(node, name + "."))
        else:
            found.extend(symbols(node, prefix))
    return found


def code_bytes(raw):
    """Bytes on lines that are neither blank nor comment-only: what ast.parse
    actually works through."""
    return sum(len(line) for line in raw.splitlines()
               if line.strip() and not line.lstrip().startswith(b"#"))


def python_outline(raw, first_page_bytes, deadline):
    """``(page_bytes, outline)``: a shorter first page (``OUTLINE_LIMIT``
    lines, ``page_bytes`` long) plus a bounded AST map of ``raw`` (the
    file's bytes), or None to keep the normal page of ``first_page_bytes``."""
    if time.monotonic() >= deadline or len(raw) > OUTLINE_MAX_FILE:
        return None
    # A large module's AST is hundreds of thousands of acyclic nodes: the
    # cyclic collector only rescans them, which tripled the walk (#190).
    collecting = gc.isenabled()
    gc.disable()
    try:
        entries = symbols(ast.parse(raw))
    except (SyntaxError, UnicodeError, ValueError, RecursionError, MemoryError):
        return None   # unparseable here, e.g. too deeply nested (#145): normal paging
    finally:
        if collecting:
            gc.enable()
    if not entries or time.monotonic() >= deadline:
        return None
    prefix = raw[:first_page_bytes]
    pos = 0
    for _ in range(OUTLINE_LIMIT):
        pos = prefix.find(b"\n", pos) + 1
        if pos == 0:
            return None
    lines = ["Python AST outline (source line numbers; use Read offset/limit for exact bodies):"]
    for entry in entries:
        lines.append("%d-%d %s %s" % (entry["start"], entry["end"], entry["kind"], entry["name"]))
    outline = "\n".join(lines)
    # Count the full context, including its framing, before choosing a smaller
    # page. An outline that does not save bytes uses the normal page.
    if (len(outline.encode("utf-8")) > OUTLINE_MAX_BYTES
            or first_page_bytes - pos <= len(outline.encode("utf-8")) + 160):
        return None
    return pos, outline


def outline_page(raw, first_page_bytes, remaining):
    """``python_outline`` with ``remaining`` seconds of a hook's budget: a
    module with little code is outlined however little is left (#140); a
    code-heavy one needs more than 0.2 s."""
    cheap = code_bytes(raw) <= CHEAP_CODE_BYTES
    if not cheap and remaining <= 0.2:
        return None
    budget = max(remaining, CHEAP_PARSE_SECONDS) if cheap else remaining
    return python_outline(raw, first_page_bytes, time.monotonic() + budget)
