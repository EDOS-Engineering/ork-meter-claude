"""hooks/lib/json_selection.py as it was before #190, verbatim below this note.

Its ``select`` re-serialized the whole output once per selected value, which
is quadratic. tests/json_selection_regressions.py checks that the current
module gives identical results on a varied corpus.

Bounded JSON Pointer selection from immutable artifact text; no evaluation.

Numbers retain their original JSON lexemes. Duplicate keys and unsupported
structures are refused rather than guessed. This module loads on manual use.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_NODES = 200000
MAX_DEPTH = 64
INDEX = re.compile(r"(?:0|[1-9][0-9]*)\Z")
BAD_ESCAPE = re.compile(r"~(?:[^01]|$)")


class ExactNumber:
    __slots__ = ("text",)

    def __init__(self, text):
        if len(text) > 4096:
            raise ValueError("JSON numeric literal exceeds selection allowance")
        self.text = text


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON keys make exact selection ambiguous")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite constants are not supported JSON")


def encode(value):
    """Serialize selected values without rounding or quoting numeric literals."""
    if isinstance(value, ExactNumber):
        return value.text
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite comparison value")
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + encode(item)
                              for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(encode(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def serialized(value):
    return (encode(value) + "\n").encode("utf-8")


def _bounded_tree(value):
    pending, count = [(value, 0)], 0
    while pending:
        current, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("JSON structure exceeds selection allowance")
        if isinstance(current, dict):
            for key, item in current.items():
                key.encode("utf-8")
                pending.append((item, depth + 1))
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            current.encode("utf-8")


def _pointer(value):
    if not isinstance(value, str) or len(value) > 1024 or value and not value.startswith("/"):
        raise ValueError("pointer must be empty or start with /, at most 1024 characters")
    value.encode("utf-8")
    if BAD_ESCAPE.search(value):
        raise ValueError("pointer contains an invalid ~ escape")
    tokens = [] if value == "" else [part.replace("~1", "/").replace("~0", "~") for part in value[1:].split("/")]
    if len(tokens) > MAX_DEPTH:
        raise ValueError("pointer depth exceeds selection allowance")
    return tokens


def _resolve(value, tokens):
    for token in tokens:
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and INDEX.fullmatch(token) and len(token) <= 9 and int(token) < len(value):
            value = value[int(token)]
        else:
            return False, None
    return True, value


def _equal(actual, expected):
    if isinstance(actual, ExactNumber):
        if type(expected) is not int and not isinstance(expected, Decimal):
            return False  # JSON booleans are not the numbers zero and one.
        try:
            return Decimal(actual.text) == Decimal(expected)
        except InvalidOperation as exc:
            raise ValueError("numeric comparison exceeds supported decimal range") from exc
    return type(actual) is type(expected) and actual == expected


def select(document, request, budget=24 * 1024):
    if type(budget) is not int or not 1024 <= budget <= 1024 * 1024:
        raise ValueError("JSON selection budget must be 1 KiB..1 MiB")
    if not isinstance(request, str) or len(request.encode("utf-8")) > 64 * 1024:
        raise ValueError("JSON selection request must be a string of at most 64 KiB")
    try:
        spec = json.loads(request, object_pairs_hook=_pairs, parse_float=Decimal, parse_constant=_constant)
    except (RecursionError, InvalidOperation) as exc:
        raise ValueError("JSON request exceeds selection allowance") from exc
    if not isinstance(spec, dict) or set(spec) - {"pointers", "filter"}:
        raise ValueError("JSON selection requires pointers and/or filter")
    pointers = spec.get("pointers", [])
    if not isinstance(pointers, list) or len(pointers) > 16 or not pointers and "filter" not in spec:
        raise ValueError("select at most sixteen pointers or one array filter")
    tokens = [_pointer(pointer) for pointer in pointers]
    if len(set(pointers)) != len(pointers):
        raise ValueError("duplicate pointers are not needed")
    filter_spec = spec.get("filter")
    if "filter" in spec:
        if (not isinstance(filter_spec, dict) or set(filter_spec) - {"pointer", "where", "equals", "start", "limit"}
                or not {"pointer", "where", "equals"} <= set(filter_spec)):
            raise ValueError("filter requires pointer, where and scalar equals")
        array_tokens, where_tokens = _pointer(filter_spec["pointer"]), _pointer(filter_spec["where"])
        expected = filter_spec["equals"]
        if expected is not None and type(expected) not in (str, bool, int, Decimal):
            raise ValueError("filter equals must be a JSON scalar")
        if isinstance(expected, str):
            expected.encode("utf-8")
        start, limit = filter_spec.get("start", 1), filter_spec.get("limit", 100)
        if type(start) is not int or not 1 <= start <= 100000 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("filter start must be 1..100000 and limit 1..1000")
    text = document.get("text")
    if not isinstance(text, str):
        raise ValueError("artifact contains no JSON text")
    raw = text.encode("utf-8")
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("JSON artifact exceeds 8 MiB selection allowance; use exact text ranges")
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_int=ExactNumber,
                           parse_float=ExactNumber, parse_constant=_constant)
        _bounded_tree(value)
    except RecursionError as exc:
        raise ValueError("JSON structure exceeds selection allowance") from exc
    output = {"schema_version": 1, "kind": "json_selection", "tool": document.get("tool"),
              "source_sha256": hashlib.sha256(raw).hexdigest(), "source_bytes": len(raw),
              "completeness": document.get("completeness", {"received_response": "unknown", "before_hook": "unknown"}),
              "scope": "entire saved JSON text", "full_document": False,
              "complete": False, "projections": [], "filter": None}
    pending = []
    for pointer, path_tokens in zip(pointers, tokens):
        found, selected = _resolve(value, path_tokens)
        item = {"pointer": pointer, "status": "output_budget_exceeded" if found else "missing"}
        output["projections"].append(item)
        if found:
            pending.append((item, selected))
    if filter_spec is not None:
        found, array = _resolve(value, array_tokens)
        if not found or not isinstance(array, list):
            raise ValueError("filter pointer must resolve to a JSON array")
        matches, missing = [], 0
        for index, item in enumerate(array):
            exists, tested = _resolve(item, where_tokens)
            if not exists:
                missing += 1
            elif _equal(tested, expected):
                matches.append(index)
        page = matches[start - 1:start - 1 + limit]
        output["filter"] = {"pointer": filter_spec["pointer"], "where": filter_spec["where"], "equals": expected,
                            "start": start, "limit": limit, "scanned_items": len(array), "missing_where": missing,
                            "total_matches": len(matches), "returned_matches": len(page), "omitted_matches": len(matches),
                            "page_complete": False, "items": []}
        for index in page:
            item = {"index": index, "pointer": filter_spec["pointer"] + "/" + str(index),
                    "status": "output_budget_exceeded"}
            output["filter"]["items"].append(item)
            pending.append((item, array[index]))
    if len(serialized(output)) > budget:
        raise ValueError("selection metadata exceeds budget; reduce pointers/page size or increase budget")
    for item, selected in pending:
        item.update(status="ok", value=selected)
        if len(serialized(output)) > budget:
            item.pop("value")
            item["status"] = "output_budget_exceeded"
    filter_result = output["filter"]
    if filter_result is not None:
        returned = sum(item["status"] == "ok" for item in filter_result["items"])
        filter_result.update(returned_matches=returned, omitted_matches=filter_result["total_matches"] - returned,
                             page_complete=returned == len(filter_result["items"]))
    output["complete"] = (all(item["status"] == "ok" for item in output["projections"])
                          and (filter_result is None or filter_result["page_complete"]))
    output["full_document"] = output["complete"] and pointers == [""] and filter_result is None
    if len(serialized(output)) > budget:
        raise ValueError("final selection metadata exceeds budget; increase budget")
    return output
