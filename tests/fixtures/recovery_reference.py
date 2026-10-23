"""hooks/lib/recovery.py as it was before #190 item 4, verbatim below this note.

Each of its exemption checks parsed the command again. tests/recovery_regressions.py
checks that ``recovery.exempt_script`` decides exactly as these did, on the
recovery corpora and every hint in skills/ and docs/.

Recognize the plugin's standalone recovery and exact source invocations,
and plain reads (``cat``, ``head``, ``tail``, ``sed -n``) of saved tool output.

No shell evaluation or command rewriting. Quoted query text is data; compound
shell commands, unquoted glob or brace characters, expansions other than the
plugin's own variables (in or out of double quotes), interpreters given by
path, wrapper or script paths that only resolve to the plugin through a
symlink, other scripts and lookalike wrappers keep normal reduction.

The interpreter must be a bare ``bash``, ``python3`` or ``python``, which the
shell resolves through the user's PATH. That is trusted as it is for every
other command Claude runs: a PATH that puts some other ``bash`` first runs it,
and its output is exempt like the real one's.
"""
import os
import re


# A recovery command longer than this is not exempt; nothing the plugin emits
# comes close, and scanning stays cheap.
MAX_COMMAND_CHARS = 8192
# The only expansions a recovery command may contain: the variables the
# plugin's own hints and skills spell out (hooks/lib/record_handles.py,
# skills/recover, skills/source, skills/capture). A plain reference expands to
# a value and runs nothing; any other ``$`` form (``$(...)``, ``$((...))``,
# ``${x:-...}``, ``$x``) may run a command or reshape the arguments.
# Only CLAUDE_PLUGIN_ROOT may appear in the wrapper or script path itself.
# CLAUDE_PROJECT_DIR was added in step with hooks/lib/recovery.py (#107: the
# explore skill's --workspace argument), so the differential keeps comparing
# the exemption logic itself rather than this one deliberate vocabulary change.
_HINT_VARIABLES = frozenset(("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR"))
_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
# Unquoted, these make bash glob or brace-expand a word.
_PATTERN_CHARS = frozenset("*?[]{}")
_INTERPRETERS = ("bash", "python3", "python")


def _variable(command, index, variables):
    """The allowed plain variable reference at ``index``, or None."""
    match = _VARIABLE.match(command, index)
    if not match or (match.group(1) or match.group(2)) not in variables:
        return None
    return match


def _words(command, variables=()):
    """One simple command as words, or None for anything else.

    A word is a list of literal strings and ``(name, quoted)`` variable
    references. Pipes, lists, redirections, subshells, comments, tilde
    expansion, unquoted glob or brace characters (anywhere: ``cat */x`` or
    ``--id *`` would expand to other words) and every ``$`` form other than a
    plain reference to one of ``variables`` are refused. Quoting follows bash:
    single quotes are literal; in double quotes a backslash escapes only
    ``$``, backtick, ``"`` and backslash."""
    if not isinstance(command, str) or not command or len(command) > MAX_COMMAND_CHARS:
        return None
    words = []
    parts = None
    index, length = 0, len(command)
    while index < length:
        char = command[index]
        if char in " \t":
            if parts is not None:
                words.append(parts)
                parts = None
            index += 1
            continue
        if parts is None:
            if char in "#~":
                return None
            parts = []
        if char == "\\":
            if index + 1 >= length or command[index + 1] in "\r\n":
                return None
            parts.append(command[index + 1])
            index += 2
        elif char == "'":
            end = command.find("'", index + 1)
            if end < 0:
                return None
            parts.append(command[index + 1:end])
            index = end + 1
        elif char == '"':
            index += 1
            while True:
                if index >= length:
                    return None
                char = command[index]
                if char == '"':
                    index += 1
                    break
                if char == "\\":
                    if index + 1 >= length or command[index + 1] in "\r\n":
                        return None
                    following = command[index + 1]
                    parts.append(following if following in '$`"\\' else "\\" + following)
                    index += 2
                elif char == "$":
                    match = _variable(command, index, variables)
                    if not match:
                        return None
                    parts.append((match.group(1) or match.group(2), True))
                    index = match.end()
                elif char == "`":
                    return None
                else:
                    parts.append(char)
                    index += 1
        elif char == "$":
            match = _variable(command, index, variables)
            if not match:
                return None
            parts.append((match.group(1) or match.group(2), False))
            index = match.end()
        elif char in ";&|<>()`\r\n":
            return None
        else:
            if char in _PATTERN_CHARS:
                return None
            parts.append(char)
            index += 1
    if parts is not None:
        words.append(parts)
    return words or None


def _text(word):
    """A word's literal text; a variable reference becomes a NUL-marked name
    that matches no literal."""
    return "".join(part if isinstance(part, str) else "\0" + part[0] for part in word)


def _simple_args(command):
    """The words of one simple command with no expansions, or None."""
    words = _words(command)
    return [_text(word) for word in words] if words else None


def _is_plugin_file(word, root, cwd, relative, absolute=False):
    """Whether ``word`` names ``root/<relative>`` as written; with
    ``absolute``, only as an absolute path.

    The written path is not resolved: py.sh and the scripts find the plugin
    from the path they were started by, so a symlink to them elsewhere would
    run other code. Only the trusted side is resolved, so the path may spell
    the root as given or as its realpath. Any error means no."""
    value = []
    for part in word:
        if isinstance(part, str):
            value.append(part)
        elif part[0] != "CLAUDE_PLUGIN_ROOT" or (not part[1] and re.search(r"[\s*?\[\]]", root)):
            # An unquoted root with spaces or glob characters is split or globbed.
            return False
        else:
            value.append(root)
    value = "".join(value)
    if os.name == "nt":
        value = value.replace("\\", "/")
        # Git Bash /c/foo spelling and Windows C:/foo refer to the same root.
        if re.match(r"^/[a-zA-Z]/", value):
            value = value[1] + ":" + value[2:]
    if ".." in value.replace("\\", "/").split("/") or (absolute and not os.path.isabs(value)):
        return False
    try:
        # The working directory is resolved: a relative path is looked up
        # from the real directory, whatever spelling the payload gives.
        written = os.path.normcase(os.path.abspath(os.path.join(os.path.realpath(cwd), value)))
        roots = {os.path.abspath(root), os.path.realpath(root)}
        return any(written == os.path.normcase(os.path.join(trusted, *relative)) for trusted in roots)
    except (OSError, ValueError):
        return False


def is_recovery_command(command, env, cwd=None, *, script="artifact.py"):
    if script not in ("artifact.py", "source.py", "command.py"):
        return False
    words = _words(command, _HINT_VARIABLES)
    if not words:
        return False
    root = env.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    root = os.path.abspath(root)
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    # The interpreter as a bare name resolved through the user's PATH, never
    # a path to some binary (see the module docstring).
    interpreter = _text(words.pop(0))
    if interpreter not in _INTERPRETERS:
        return False
    if interpreter == "bash":
        if len(words) < 3 or [_text(word) for word in words[1:3]] != ["--script", "scripts/" + script]:
            return False
        # py.sh finds the plugin from $0, so a relative one depends on how the
        # shell spells the working directory, not on the payload's cwd: every
        # hint names it absolutely or through $CLAUDE_PLUGIN_ROOT.
        return _is_plugin_file(words[0], root, cwd, ("hooks", "py.sh"), absolute=True)
    while words and _text(words[0]) in ("-I", "-B"):
        words.pop(0)
    if not words:
        return False
    return _is_plugin_file(words[0], root, cwd, ("scripts", script))


def is_source_command(command, env, cwd=None):
    """Source retrieval must expose the requested exact source before editing."""
    return is_recovery_command(command, env, cwd, script="source.py")


def is_capture_command(command, env, cwd=None):
    """Captured command results have already been transformed once."""
    return is_recovery_command(command, env, cwd, script="command.py")


ARTIFACT_NAME = re.compile(r"^[a-f0-9]{64}\.(txt|json)$")
_COUNT = r"[+-]?\d+[kKmMbB]?"
_HEAD_TAIL_OPTION = re.compile(r"^(-\d+|-[nc]%s|--(lines|bytes)=%s|-q|-v|--)$" % (_COUNT, _COUNT))
_SED_ADDRESS = r"(\d+|\$)"
_SED_SCRIPT = re.compile(r"^%s(,(%s|\+\d+))?p(;%s(,(%s|\+\d+))?p)*;?$"
                         % (_SED_ADDRESS, _SED_ADDRESS, _SED_ADDRESS, _SED_ADDRESS))


def artifact_read_paths(command, cwd=None):
    """The files a plain ``cat``, ``head``, ``tail`` or ``sed -n 'A,Bp'`` of
    saved tool output reads, as absolute paths, or None.

    Conservative: one simple command, no expansions, only known options, and
    every operand named like an artifact (``<sha256>.txt`` or ``.json``).
    Anything else is None and the caller keeps its normal behaviour."""
    args = _simple_args(command)
    if not args:
        return None
    executable = args.pop(0).replace("\\", "/").rsplit("/", 1)[-1]
    operands = []
    if executable == "cat":
        for arg in args:
            if arg in ("-n", "-b", "--"):
                continue
            operands.append(arg)
    elif executable in ("head", "tail"):
        pending = False
        for arg in args:
            if pending:
                if not re.fullmatch(_COUNT, arg):
                    return None
                pending = False
            elif arg in ("-n", "-c"):
                pending = True
            elif arg.startswith("-"):
                if not _HEAD_TAIL_OPTION.match(arg):
                    return None
            else:
                operands.append(arg)
        if pending:
            return None
    elif executable == "sed":
        quiet = script = False
        for arg in args:
            if arg == "-n":
                quiet = True
            elif arg.startswith("-"):
                return None
            elif not script:
                if not _SED_SCRIPT.match(arg):
                    return None
                script = True
            else:
                operands.append(arg)
        if not quiet or not script:
            return None
    else:
        return None
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    paths = []
    for operand in operands:
        if operand.startswith("-") or not ARTIFACT_NAME.match(operand.replace("\\", "/").rsplit("/", 1)[-1]):
            return None
        paths.append(os.path.join(cwd, operand))
    return paths or None


def reads_only_artifacts(command, directory, cwd=None):
    """True when ``command`` only prints files that sit directly in
    ``directory`` (one session's tool-output store)."""
    paths = artifact_read_paths(command, cwd)
    if not paths:
        return False
    home = os.path.normcase(os.path.realpath(directory))
    return all(os.path.normcase(os.path.dirname(os.path.realpath(path))) == home for path in paths)
