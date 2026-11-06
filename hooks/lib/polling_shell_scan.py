"""Bounded Bash admission, validated by Bash's non-executing syntax check."""
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

EXECUTABLES = {'python', 'python3', 'node', 'ffmpeg', 'ffprobe', 'cat', 'mkdir',
               'sed', 'head', 'tail', 'ls', 'cp', 'mv', 'curl', 'timeout', 'echo',
               'true', 'false', 'cd', 'tee'}
LAUNCHERS = {'python', 'python3', 'node', 'ffmpeg', 'timeout'}
RESERVED = {'PATH', 'IFS', 'ENV', 'BASH_ENV', 'SHELLOPTS', 'BASHOPTS', 'CDPATH',
            'GLOBIGNORE', 'PROMPT_COMMAND', 'PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH'}


class Unsupported(ValueError):
    pass


def scan(text, depth=0):
    if depth > 4:
        raise Unsupported('nested substitutions')
    tokens, substitutions, pending = [], [], []
    index, size = 0, len(text)
    while index < size:
        char = text[index]
        if char in ' \t\r':
            index += 1
            continue
        if char == '#':
            end = text.find('\n', index)
            index = size if end < 0 else end
            continue
        if char == '\n':
            tokens.append(('separator', '\n', True))
            index += 1
            for delimiter in pending:
                while True:
                    end = text.find('\n', index)
                    if end < 0:
                        if text[index:] == delimiter:
                            index = size
                            break
                        raise Unsupported('unterminated heredoc')
                    line = text[index:end]
                    index = end+1
                    if line == delimiter:
                        break
            pending = []
            continue
        if char in ';|&':
            operator = text[index:index+2]
            if operator in ('&&', '||'):
                index += 2
            elif char == '|' and operator != '|&':
                operator = '|'
                index += 1
            elif char == ';' and operator != ';;':
                operator = ';'
                index += 1
            else:
                raise Unsupported('pipeline or asynchronous shell list')
            tokens.append(('separator', operator, True))
            continue
        if char in '<>':
            operator = char
            index += 1
            if index < size and text[index] in (char, '&'):
                operator += text[index]
                index += 1
            if operator not in ('<', '>', '>>', '<<', '>&', '<&'):
                raise Unsupported('redirect')
            if index < size and text[index] in '<>()-':
                raise Unsupported('redirect extension')
            tokens.append(('redirect', operator, True))
            continue
        start = index
        literal, quote, spans = True, None, []
        while index < size:
            char = text[index]
            if quote == "'":
                index += 1
                if char == "'":
                    quote = None
                continue
            if char == '\\':
                if index+1 >= size:
                    raise Unsupported('trailing escape')
                index += 2
                continue
            if char in ("'", '"'):
                if quote is None:
                    quote = char
                elif quote == char:
                    quote = None
                index += 1
                continue
            if char == '`':
                raise Unsupported('backtick substitution')
            if char == '$':
                literal = False
                if text.startswith('$(', index):
                    begin = index+2
                    cursor, nesting, inner_quote, escaped = begin, 1, None, False
                    while cursor < size and nesting:
                        current = text[cursor]
                        if escaped:
                            escaped = False
                        elif current == '\\' and inner_quote != "'":
                            escaped = True
                        elif inner_quote:
                            if current == inner_quote:
                                inner_quote = None
                        elif current in ("'", '"'):
                            inner_quote = current
                        elif current == '(':
                            nesting += 1
                        elif current == ')':
                            nesting -= 1
                        cursor += 1
                    if nesting or text.startswith('$((', index):
                        raise Unsupported('substitution')
                    substitutions.append(scan(text[begin:cursor-1], depth+1))
                    spans.append((index-start, cursor-start))
                    index = cursor
                    continue
                match = re.match(r'\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*', text[index:])
                if match is None:
                    raise Unsupported('parameter expansion')
                index += len(match[0])
                continue
            if quote is None and char in ' \t\r\n;|&<>':
                break
            if quote is None and char in '(){}':
                raise Unsupported('compound shell structure')
            index += 1
        if quote is not None or index == start:
            raise Unsupported('word')
        raw = text[start:index]
        cooked = raw
        for begin, end in reversed(spans):
            cooked = cooked[:begin]+'SUBSTITUTION'+cooked[end:]
        try:
            values = shlex.split(cooked)
        except ValueError as error:
            raise Unsupported('word') from error
        if len(values) != 1:
            raise Unsupported('word')
        value = values[0]
        tokens.append(('word', value, literal))
        if len(tokens) >= 2 and tokens[-2][:2] == ('redirect', '<<'):
            if not literal or not re.fullmatch(r"'[^']+'|\"[^\"$`\\]+\"", raw):
                raise Unsupported('expanding heredoc')
            pending.append(value)
    if pending:
        raise Unsupported('heredoc body missing')
    return tokens, substitutions


def classify(scanned, cwd=None, polling_loop=False):
    tokens, substitutions = scanned
    outputs = None
    if os.environ.get('ORK_EXPERIMENT_POLLING_CWD_OUTPUT_V1') == '1':
        from polling_cwd_outputs import Outputs
        outputs = Outputs(cwd, tokens)
    workspace = None
    current = scope_root = None
    prepared = set()
    if os.environ.get('ORK_EXPERIMENT_POLLING_WORKSPACE_V1') == '1':
        import polling_workspace as workspace
        current = workspace.resolve(str(cwd), None) if cwd is not None else None
        scope_root = current
    paths, changed_cwd, has_launcher = [], False, False
    for inner in substitutions:
        _, _, child_launcher = classify(inner, cwd=cwd, polling_loop=polling_loop)
        has_launcher |= child_launcher
    command = []
    require_tee = False
    previous_separator = None

    def output_path(name):
        target = outputs.path(name) if outputs is not None else name
        if target is not None:
            paths.append(target)
    index = 0
    while index <= len(tokens):
        kind, value, literal = tokens[index] if index < len(tokens) else ('separator', '\n', True)
        if kind == 'redirect':
            if index+1 >= len(tokens) or tokens[index+1][0] != 'word':
                raise Unsupported('redirect operand')
            target = tokens[index+1]
            if value in ('>', '>>') and target[2] and not any(c in target[1] for c in '*?['):
                output_path(target[1])
                if workspace is not None:
                    workspace.remember(target[1], current, scope_root, prepared)
            index += 2
            continue
        if kind == 'separator':
            words = command
            if polling_loop:
                # Bash -n below validates nesting. These control words do not
                # execute an unvalidated command or bypass the executable list.
                while words and words[0][1] and words[0][0] in ('while', 'do', 'if', 'then', 'else'):
                    words = words[1:]
            while words and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0][0]):
                name = words.pop(0)[0].split('=', 1)[0]
                if name in RESERVED or name.startswith('BASH'):
                    raise Unsupported('shell state assignment')
            if words:
                name, static = words[0]
                name = name.replace('\\', '/')
                if not static or not re.fullmatch(r'(?:[A-Za-z]:)?[A-Za-z0-9_./ -]+', name):
                    raise Unsupported('dynamic command')
                raw_name = name
                name = name.rsplit('/', 1)[-1]
                if os.name == 'nt':
                    name = name.lower()
                if name.endswith('.exe'):
                    name = name[:-4]
                if require_tee and name != 'tee' and not (polling_loop and name == 'grep'):
                    if name != 'tail' or os.environ.get('ORK_EXPERIMENT_POLLING_TAIL_V1') != '1':
                        raise Unsupported('unsupported pipeline consumer')
                    from polling_tail_consumer import bounded_tail
                    if not bounded_tail(words):
                        raise Unsupported('unbounded or non-stdin tail consumer')
                workspace_command = (workspace is not None and
                                     workspace.local_command(raw_name, current, scope_root, prepared))
                loop_command = polling_loop and name in ('sleep', 'grep', 'break', 'fi', 'done')
                if (name not in EXECUTABLES and not loop_command and not workspace_command and
                        not (workspace is not None and name in workspace.PREPARATION)):
                    raise Unsupported('shell command')
                if name == 'tee':
                    for word, static in words[1:]:
                        if static and not word.startswith('-') and not any(c in word for c in '*?['):
                            output_path(word)
                has_launcher |= (name in LAUNCHERS or workspace_command or
                                 (polling_loop and name == 'curl') or
                                 (workspace is not None and name in workspace.INTERPRETERS))
                changed_cwd |= name == 'cd'
                if outputs is not None and name == 'cd':
                    outputs.advance(words, value, previous_separator)
                if workspace is not None and name == 'cd':
                    current = workspace.advance(words, current)
            if require_tee and not words:
                raise Unsupported('empty pipeline consumer')
            require_tee = value == '|'
            if outputs is not None:
                outputs.boundary(value)
            previous_separator = value
            command = []
        else:
            if value.isdigit() and index+1 < len(tokens) and tokens[index+1][0] == 'redirect':
                index += 1
                continue
            command.append((value, literal))
        index += 1
    return paths, changed_cwd, has_launcher


def bash_binary():
    if os.name == 'nt':
        candidates = [os.environ.get('CLAUDE_CODE_GIT_BASH_PATH'),
                      str(Path(os.environ.get('ProgramFiles', 'C:/Program Files'))/'Git/bin/bash.exe')]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
        return None  # System32/bash.exe is a WSL launcher, not native Git Bash.
    return shutil.which('bash')


def admission(command, cwd):
    if not isinstance(command, str) or not 0 < len(command.encode()) <= 32768 or '\x00' in command:
        return None
    try:
        from polling_http import loop_origin
        paths, changed_cwd, has_launcher = classify(scan(command), cwd=cwd,
                                                   polling_loop=loop_origin(command) is not None)
        if not has_launcher:
            return None
        binary = bash_binary()
        if binary is None:
            return None
        env = {k: v for k, v in os.environ.items() if k.upper() in
               ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP')}
        result = subprocess.run([binary, '--noprofile', '--norc', '-n'], input=command,
                                text=True, capture_output=True, env=env, timeout=1)
        if result.returncode:
            return None
    except (Unsupported, OSError, subprocess.SubprocessError):
        return None
    return sorted({str(Path(p) if Path(p).is_absolute() else cwd/p) for p in paths
                   if Path(p).is_absolute() or not changed_cwd})
