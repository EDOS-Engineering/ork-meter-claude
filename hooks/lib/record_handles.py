"""Bounded schema-only descriptions; the exact received JSON remains recoverable."""
import hashlib
import json
import os
import shlex


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Number:
    def __init__(self, token):
        pass


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError('non-JSON constant')


def inspect(text):
    if not isinstance(text, str) or len(text) > 1024 * 1024 or not text.lstrip().startswith('['):
        return None
    try:
        raw = text.encode('utf-8')
        if len(raw) > 1024 * 1024:
            return None
        rows = json.loads(text, object_pairs_hook=unique, parse_int=Number,
                          parse_float=Number, parse_constant=invalid_constant)
        if not isinstance(rows, list) or not 16 <= len(rows) <= 5000:
            return None
        fields = {}
        for row in rows:
            if not isinstance(row, dict) or not row:
                return None
            for key, value in row.items():
                kind = ('number' if isinstance(value, Number) else 'null' if value is None else
                        'boolean' if type(value) is bool else 'string' if isinstance(value, str) else
                        'array' if isinstance(value, list) else 'object')
                field = fields.setdefault(key, {'types': set(), 'present': 0})
                field['types'].add(kind)
                field['present'] += 1
            if len(fields) > 32:
                return None
        schema = {key: {'types': sorted(value['types']), 'present': value['present']}
                  for key, value in sorted(fields.items())}
        if len(json.dumps(schema).encode('utf-8')) > 2048:
            return None
        return {'record_count': len(rows), 'fields': schema,
                'source_sha256': hashlib.sha256(raw).hexdigest()}
    except (ValueError, RecursionError, UnicodeError):
        return None


def recovery_command(artifact_path):
    """The exact scripts/artifact.py call that selects records 0-2 of the artifact.

    The artifact is one line of minified JSON, which Claude Code's Read cuts at
    2000 characters (#146), so the hint names a JSON selection instead. The
    store's scratchpad is the artifact's directory three levels up
    (``<scratchpad>/ork-meter/tool-output/<id>.txt``).
    """
    path = os.path.abspath(str(artifact_path))
    scratchpad = os.path.dirname(os.path.dirname(os.path.dirname(path)))
    identity = os.path.splitext(os.path.basename(path))[0]
    return ' '.join(['bash', shlex.quote(os.path.join(ROOT, 'hooks', 'py.sh').replace('\\', '/')),
                     '--script', 'scripts/artifact.py', '--scratchpad', shlex.quote(scratchpad.replace('\\', '/')),
                     '--id', identity, '--select-json', shlex.quote('{"pointers":["/0","/1","/2"]}')])


def describe(info, artifact_path, source_bytes):
    document = {'schema': 'ork-record-handle-v1', **info, 'values_included': False,
                'scope': 'received tool text only; upstream completeness unknown',
                'artifact_path': str(artifact_path),
                'recovery': ('The artifact is one line of JSON, too long for Read: parse it with local code, '
                             'or run the Select records command. Each pointer /N is record N (up to 16 pointers); '
                             'or pass {"filter":{"pointer":"","where":"/FIELD","equals":VALUE,"start":1,"limit":20}}.')}
    return ('Ork Meter structured records: values withheld; this is not an empty result.\n'
            + json.dumps(document, ensure_ascii=False, separators=(',', ':'))
            + '\nFull output: %s (%d bytes)' % (artifact_path, source_bytes)
            + '\nSelect records: ' + recovery_command(artifact_path))
