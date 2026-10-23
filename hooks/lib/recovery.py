"""Recognize the plugin's standalone recovery and exact source invocations,
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
import collections
import os
import re


# A recovery command longer than this is not exempt; nothing the plugin emits
# comes close, and scanning stays cheap.
MAX_COMMAND_CHARS = 8192
# The only expansions a recovery command may contain: the variables the
# plugin's own hints and skills spell out (hooks/lib/record_handles.py,
# skills/recover, skills/source, skills/capture, and CLAUDE_PROJECT_DIR as
# skills/explore's search workspace). A plain reference expands to
# a value and runs nothing; any other ``$`` form (``$(...)``, ``$((...))``,
# ``${x:-...}``, ``$x``) may run a command or reshape the arguments.
# Only CLAUDE_PLUGIN_ROOT may appear in the wrapper or script path itself.
_HINT_VARIABLES = frozenset(("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR"))
_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
# Unquoted, these make bash glob or brace-expand a word.
_PATTERN_CHARS = frozenset("*?[]{}")
_INTERPRETERS = ("bash", "python3", "python")
# The plugin scripts whose standalone invocation is exempt from Bash reduction.
_SCRIPTS = ("artifact.py", "source.py", "command.py")


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


def _plugin_file(word, root, cwd, candidates, absolute=False):
    """The one of ``candidates`` (paths inside the plugin, as tuples of
    parts) that ``word`` names as ``root/<candidate>`` as written, or None;
    with ``absolute``, only as an absolute path.

    The written path is not resolved: py.sh and the scripts find the plugin
    from the path they were started by, so a symlink to them elsewhere would
    run other code. Only the trusted side is resolved, so the path may spell
    the root as given or as its realpath. Any error means None."""
    value = []
    for part in word:
        if isinstance(part, str):
            value.append(part)
        elif part[0] != "CLAUDE_PLUGIN_ROOT" or (not part[1] and re.search(r"[\s*?\[\]]", root)):
            # An unquoted root with spaces or glob characters is split or globbed.
            return None
        else:
            value.append(root)
    value = "".join(value)
    if os.name == "nt":
        value = value.replace("\\", "/")
        # Git Bash /c/foo spelling and Windows C:/foo refer to the same root.
        if re.match(r"^/[a-zA-Z]/", value):
            value = value[1] + ":" + value[2:]
    if ".." in value.replace("\\", "/").split("/") or (absolute and not os.path.isabs(value)):
        return None
    try:
        # The working directory is resolved: a relative path is looked up
        # from the real directory, whatever spelling the payload gives.
        written = os.path.normcase(os.path.abspath(os.path.join(os.path.realpath(cwd), value)))
        roots = {os.path.abspath(root), os.path.realpath(root)}
        for relative in candidates:
            if any(written == os.path.normcase(os.path.join(trusted, *relative)) for trusted in roots):
                return relative
    except (OSError, ValueError):
        pass
    return None


def _script(words, env, cwd):
    """The plugin script (one of ``_SCRIPTS``) that ``words``, one simple
    command parsed with the hint variables, runs standalone, or None."""
    root = env.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    root = os.path.abspath(root)
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    # The interpreter as a bare name resolved through the user's PATH, never
    # a path to some binary (see the module docstring).
    interpreter = _text(words[0])
    if interpreter not in _INTERPRETERS:
        return None
    words = words[1:]
    if interpreter == "bash":
        if len(words) < 3 or _text(words[1]) != "--script":
            return None
        named = _text(words[2])
        script = named[len("scripts/"):] if named.startswith("scripts/") else None
        if script not in _SCRIPTS:
            return None
        # py.sh finds the plugin from $0, so a relative one depends on how the
        # shell spells the working directory, not on the payload's cwd: every
        # hint names it absolutely or through $CLAUDE_PLUGIN_ROOT.
        return script if _plugin_file(words[0], root, cwd, (("hooks", "py.sh"),), absolute=True) else None
    while words and _text(words[0]) in ("-I", "-B"):
        words = words[1:]
    if not words:
        return None
    found = _plugin_file(words[0], root, cwd, tuple(("scripts", script) for script in _SCRIPTS))
    return found[1] if found else None


# What ``exempt_script`` found in one command: ``script``, the plugin script
# it runs standalone (``artifact.py``, ``source.py`` or ``command.py``), or
# ``reads``, the files a plain read of artifact-named files prints
# (``artifact_read_paths``); at most one of them is set.
Exemption = collections.namedtuple("Exemption", "script reads")
NOT_EXEMPT = Exemption(None, None)


def exempt_script(command, env, cwd=None):
    """Parse ``command`` once and say why its output may skip Bash reduction.

    Decides exactly as ``is_recovery_command``, ``is_source_command``,
    ``is_capture_command`` and ``artifact_read_paths`` do one by one: the
    command is parsed with the hint variables allowed, and a parse that holds
    no variable reference is the same as the plain parse a read needs (that
    parse refuses every ``$`` and differs in nothing else). A ``bash`` or
    ``python`` command is never a ``cat``, ``head``, ``tail`` or ``sed`` read.
    Whether the reads are this session's artifacts is ``reads_only_in``."""
    words = _words(command, _HINT_VARIABLES)
    if not words:
        return NOT_EXEMPT
    script = _script(words, env, cwd)
    if script is not None:
        return Exemption(script, None)
    if any(not isinstance(part, str) for word in words for part in word):
        return NOT_EXEMPT
    return Exemption(None, _artifact_reads([_text(word) for word in words], cwd))


def outside(path, cwd):
    """Whether ``path`` lies outside the working directory ``cwd``, where file
    tools confined to the workspace (``--restricted``, sandboxes, permission
    rules) refuse it. An unknown or unusable cwd counts as outside."""
    if not isinstance(cwd, str) or not cwd:
        return True
    try:
        base = os.path.normcase(os.path.realpath(cwd))
        target = os.path.normcase(os.path.realpath(str(path)))
        return os.path.commonpath([base, target]) != base
    except (OSError, ValueError):
        return True


def is_recovery_command(command, env, cwd=None, *, script="artifact.py"):
    if script not in _SCRIPTS:
        return False
    words = _words(command, _HINT_VARIABLES)
    return bool(words) and _script(words, env, cwd) == script


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
    return _artifact_reads(args, cwd) if args else None


def _artifact_reads(args, cwd):
    """``artifact_read_paths`` for the literal words ``args`` of one command."""
    args = list(args)
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
    return reads_only_in(artifact_read_paths(command, cwd), directory)


def reads_only_in(paths, directory):
    """True when every one of ``paths`` (from ``artifact_read_paths`` or
    ``exempt_script``) sits directly in ``directory``, through symlinks.
    A path that can't be resolved (a NUL in it, a symlink loop) is not."""
    if not paths:
        return False
    try:
        home = os.path.normcase(os.path.realpath(directory))
        return all(os.path.normcase(os.path.dirname(os.path.realpath(path))) == home for path in paths)
    except (OSError, ValueError):
        return False
