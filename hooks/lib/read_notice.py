"""The fixed text Ork Meter may add to a Read result, and nothing else.

The emission boundary (hooks/lib/native_text.py) strips every line of a
replaced tool result that the tool did not return, and every note beside it.
A Read that leaves lines out must still say so, or Claude works from a file
that seems to end early (F016, 0.5.8: the Read budget kept lines 1-706 of
1,402 and Claude never saw the rest). So exactly two kinds of plugin text
pass the boundary for a Read, both built here and checked here:

* a marker line inside the content, in place of the first line it stands
  for: ``… lines A–B not shown; Read with offset=A``. Lines A+1..B follow it
  as empty slots (none when nothing is shown after it), so every shown line
  keeps its real number in Claude Code's rendering;
* one notice beside the result (``additionalContext``), from the templates
  below, naming the file by the path the tool returned.

Anything else, or a template filled with other text, is still stripped.
"""
import re

MARKER = "… lines %d–%d not shown; Read with offset=%d"
MARKER_RE = re.compile(r"^… lines (\d+)–(\d+) not shown; Read with offset=(\d+)$")

PAGE = ("Ork Meter shortened this Read of {path}: it shows lines {first}–{last} of {total}. "
        "Lines {next}–{total} are not shown (the last line of the result marks them); "
        "Read with offset={next} to see them.")
COMPACT = ("Ork Meter compacted this Read of {path}: {elided} of its {total} lines are not shown, in "
           "{ranges} ranges. Each range is one line '… lines A–B not shown; Read with offset=A' followed "
           "by empty numbered lines that are NOT empty in the file; every other line is exact at its own number. "
           "Read a range with offset and limit before editing it; a Write rebuilt from this view is refused.")
UNCHANGED = ("Ork Meter: {path} is unchanged since your earlier Read of it for this prompt, so this result shows "
             "only lines 1–{last} of {total}; that earlier result holds the rest. If it is no longer in your "
             "context, Read with offset={next}.")
REST = ("Ork Meter: your earlier Read of {path} showed only part of it, so this Read was sent with "
        "offset={first} and shows the lines you had not seen, {first}–{last} of {total}.")
REST_MORE = REST + " Lines {next}–{total} are still not shown; Read with offset={next} to see them."
DELTA = ("Ork Meter delta: {path} changed since its earlier full Read in this conversation. This result is "
         "not the file: it is a unified diff against that earlier result ({hunks} hunks, +{added}/-{removed} "
         "lines). Its @@ headers give real line numbers; its left-margin numbers are not file line numbers. "
         "Read with offset and limit for exact current lines.")
TEMPLATES = (PAGE, COMPACT, UNCHANGED, REST_MORE, DELTA)


def marker(first, last):
    return MARKER % (first, last, first)


def notice(template, path, **numbers):
    return template.format(path=path, **numbers)


def _pattern(template, path):
    parts = re.split(r"(\{[a-z]+\})", template)
    out = []
    for part in parts:
        if part == "{path}":
            out.append(re.escape(path))
        elif re.fullmatch(r"\{[a-z]+\}", part):
            out.append(r"\d+")
        else:
            out.append(re.escape(part))
    return "".join(out)


def allowed(text, path):
    """``text`` is exactly one of the notices, filled for ``path``."""
    if not isinstance(text, str) or not isinstance(path, str) or not path:
        return False
    return any(re.fullmatch(_pattern(template, path), text) for template in TEMPLATES)
