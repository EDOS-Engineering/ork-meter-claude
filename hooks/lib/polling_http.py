"""Recognize local HTTP polling without treating queue emptiness as completion."""
import re


ORIGIN = r'http://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}'
HISTORY = re.compile(r'(' + ORIGIN + r')/history/(?:[A-Za-z0-9_-]+|\$[A-Za-z_][A-Za-z0-9_]*)')


def loop_origin(command):
    """Only opt shell loops into admission when they poll a local history URL."""
    if not isinstance(command, str) or not re.search(r'\bwhile\s+true\s*;\s*do\b', command):
        return None
    matches = {m[1] for m in HISTORY.finditer(command)}
    return next(iter(matches)) if len(matches) == 1 else None


def delayed_queue(payload):
    value = payload.get('tool_input', {})
    if payload.get('tool_name') != 'Bash' or value.get('run_in_background'):
        return None
    command = value.get('command')
    if not isinstance(command, str):
        return None
    # A narrow read-only native queue check. Preserve curl and its output filter
    # verbatim; do not run HTTP requests in a hook or infer task completion from it.
    match = re.fullmatch(
        r'\s*sleep\s+([0-9]+(?:\.[0-9]+)?)([smhd]?)\s*(?:;|&&)\s*'
        r'(curl\s+(?:-s\s+|-sS\s+|--silent\s+)?(' + ORIGIN + r')/queue'
        r'(?:\s*\|\s*head\s+-c\s+[1-9][0-9]*)?)\s*', command)
    if not match:
        return None
    seconds = float(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
    return match[4], match[3], seconds
