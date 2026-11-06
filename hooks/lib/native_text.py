"""Remove plugin-authored prose at the native hook emission boundary.

Lossy reductions remain lossy. Only received source lines (or a verified JSON
whitespace-only minification) survive a replacement; no explanations are added.
"""
from bisect import bisect_left
import re

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
CAPPED = re.compile(r'(?:…|â€¦) \[\+\d+ chars\]$')


def source_text(original, candidate):
    if candidate == original:
        return candidate
    if not isinstance(original, str) or not isinstance(candidate, str):
        return original
    # Preserve complete, native-shaped JSON minification without line matching.
    try:
        from json_prefix_factor import parse_prefix, compact
        parsed = parse_prefix(original)
        if parsed is not None and compact(parsed) == candidate:
            return candidate
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        pass
    lines = [ANSI.sub('', line).rstrip() for line in original.split('\n')]
    positions = {}
    for index, line in enumerate(lines):
        positions.setdefault(line, []).append(index)
    result = []
    cursor = 0
    for line in candidate.split('\n'):
        variants = [line.rstrip()]
        capped = CAPPED.sub('', variants[0])
        if capped != variants[0]:
            for index in range(cursor, len(lines)):
                if lines[index].startswith(capped):
                    result.append(capped)
                    cursor = index + 1
                    break
            continue
        # A unified diff may contain source lines with a leading diff marker.
        if line.startswith(('+', ' ')) and not line.startswith('+++'):
            variants.append(line[1:].rstrip())
        for variant in variants:
            indices = positions.get(variant, ())
            location = bisect_left(indices, cursor)
            if location < len(indices):
                result.append(variant)
                cursor = indices[location] + 1
                break
    return '\n'.join(result)


def source_output(original, candidate):
    if isinstance(original, str) and isinstance(candidate, str):
        return source_text(original, candidate)
    if isinstance(original, dict) and isinstance(candidate, dict):
        updated = dict(candidate)
        for key, value in candidate.items():
            if key not in original:
                updated.pop(key, None)
                continue
            if isinstance(value, str):
                updated[key] = source_text(original.get(key, ''), value)
            elif isinstance(value, dict) and isinstance(original.get(key), dict):
                updated[key] = source_output(original[key], value)
            elif key == 'filenames' and isinstance(value, list):
                received = original.get(key, [])
                updated[key] = [v for v in value if v in received]
            elif isinstance(value, list) and isinstance(original.get(key), list):
                received = original[key]
                updated[key] = [source_output(received[i], v) for i, v in enumerate(value)
                                if i < len(received)]
        if 'content' in updated and 'numLines' in updated and isinstance(updated['content'], str):
            updated['numLines'] = len(updated['content'].splitlines())
        return updated
    return candidate if type(original) is type(candidate) else original


def clean_reply(payload, reply):
    if not isinstance(reply, dict):
        return None
    clean = {key: value for key, value in reply.items()
             if key in {'hookSpecificOutput', 'continue', 'suppressOutput', 'decision'}}
    specific = clean.get('hookSpecificOutput')
    if isinstance(specific, dict):
        specific = {key: value for key, value in specific.items()
                    if key in {'hookEventName', 'updatedToolOutput', 'updatedInput', 'permissionDecision'}}
        if 'updatedToolOutput' in specific:
            specific['updatedToolOutput'] = source_output(payload.get('tool_response'), specific['updatedToolOutput'])
        if set(specific) <= {'hookEventName'}:
            clean.pop('hookSpecificOutput', None)
        else:
            clean['hookSpecificOutput'] = specific
    return clean or None


def reconcile_records(payload, reply, rows):
    """Charge the final reduction stage for the text actually emitted.

    Earlier stages stay intact, including their activation counts. Model usage
    remains independently measured; these counters are local byte estimates.
    """
    if not isinstance(reply, dict):
        return
    output = reply.get('hookSpecificOutput', {}).get('updatedToolOutput')
    if not isinstance(output, dict):
        return
    tool = payload.get('tool_name')
    eligible = [row for row in rows if row.get('tool') == tool and
                row.get('kind') in {'trim', 'test_filter', 'dedupe', 'delta', 'read_compact', 'read_guard'}]
    if not eligible:
        return
    row = eligible[-1]
    size = None
    if tool == 'Bash' and isinstance(output.get('stdout'), str) and isinstance(output.get('stderr'), str):
        size = len(output['stdout'].encode('utf-8'))
        if row['kind'] != 'test_filter':
            size += len(output['stderr'].encode('utf-8'))
    elif tool == 'Read' and isinstance(output.get('file'), dict):
        content = output['file'].get('content')
        if isinstance(content, str):
            size = len(content.encode('utf-8'))
    else:
        for name in ('content', 'result', 'filenames'):
            content = output.get(name)
            if isinstance(content, list) and all(isinstance(v, str) for v in content):
                content = '\n'.join(content)
            if isinstance(content, str):
                size = len(content.encode('utf-8'))
                break
    if size is None:
        return
    import ledger
    row['bytes_after'] = size
    row['est_tokens_after'] = ledger.est_tokens(size)
    row['est_tokens_avoided'] = max(0, row['est_tokens_before'] - row['est_tokens_after'])
    row['native_text_boundary_reconciled'] = True
