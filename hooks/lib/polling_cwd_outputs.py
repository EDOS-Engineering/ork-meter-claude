"""Conservative literal-cwd ownership for existing receipt-backed output files."""
import os
from pathlib import Path
import re
import polling_workspace as workspace


class Outputs:
    def __init__(self, cwd, tokens):
        self.root = workspace.resolve(str(cwd), None) if cwd is not None else None
        self.cwd = self.root
        self.conditional = False
        self.ambiguous_branch = any(kind == 'separator' and value == '||'
                                    for kind, value, _ in tokens)

    def path(self, name):
        # Relative paths require one proven directory; absolute paths do not.
        target = workspace.resolve(name, self.cwd)
        return str(target) if workspace.within(target, self.root) else None

    def advance(self, words, separator, previous_separator):
        args = words[1:]
        if args and args[0] == ('--', True):
            args = args[1:]
        target = None
        if (len(args) == 1 and args[0][1] and not args[0][0].startswith('-')
                and not self.ambiguous_branch and previous_separator != '|'
                and separator != '|'):
            target = workspace.resolve(args[0][0], self.cwd)
            try:
                # Bash's logical symlink cwd can disagree with Path.resolve().
                name = args[0][0].replace('\\', '/')
                if os.name == 'nt':
                    drive = re.match(r'^/(?:mnt/)?([A-Za-z])/(.*)$', name)
                    if drive:
                        name = drive[1].upper()+':/'+drive[2]
                literal = Path(name)
                if not literal.is_absolute():
                    literal = self.cwd / literal if self.cwd is not None else None
                lexical = os.path.normcase(os.path.normpath(str(literal))) if literal is not None else None
                canonical = os.path.normcase(str(target)) if target is not None else None
                if (not workspace.within(target, self.root) or not target.is_dir()
                        or lexical != canonical):
                    target = None
            except (OSError, ValueError, TypeError):
                target = None
        if target is None or (target != self.cwd and separator != '&&'):
            self.cwd = None
        elif target != self.cwd:
            self.cwd = target
            self.conditional = True

    def boundary(self, separator):
        # A failed cd skips its && chain, then execution resumes in the old cwd.
        if self.conditional and separator in (';', '\n', '||'):
            self.cwd = None
            self.conditional = False
