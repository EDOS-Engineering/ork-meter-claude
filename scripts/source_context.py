"""Bounded static source context. Never import or execute workspace code.

Dependencies are lexical candidates within an explicit workspace, not a proof
of Python runtime binding or semantic closure. Each file has one byte snapshot.
"""
import ast
import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import tokenize

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_FILES = 16


# serialized, physical_lines, workspace_root and fill_budget are shared with
# scripts/source.py, so each byte-exact rule has one definition.
def serialized(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def physical_lines(raw, python=True):
    """``raw``'s lines as Python's parser counts them (LF, CRLF and CR
    physical lines), decoded by its coding cookie or BOM when ``python``,
    else as UTF-8. str.splitlines would also split form feeds and Unicode
    separators inside comments and strings, so otherwise correct AST
    coordinates would select the wrong source."""
    encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0] if python else "utf-8"
    return io.StringIO(raw.decode("utf-8" if encoding == "utf-8-sig" else encoding), newline="").readlines()


def workspace_root(workspace):
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    return root


def fill_budget(output, entries, budget):
    """Put each ``(index, entry)`` into ``output["results"]`` in turn,
    keeping it only while the serialized output fits ``budget`` bytes; a
    result that doesn't fit stays withheld, never cut."""
    for index, entry in entries:
        withheld = output["results"][index]
        output["results"][index] = entry
        if len(serialized(output).encode("utf-8")) > budget:
            output["results"][index] = withheld


class Snapshot:
    def __init__(self, path, raw):
        self.path, self.raw = path, raw
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.lines = physical_lines(raw)
        self.tree = ast.parse(raw)
        self.definitions = {}
        def walk(node, prefix=""):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = prefix + child.name
                    self.definitions.setdefault(name, []).append(child)
                    walk(child, name + ".")
                else:
                    walk(child, prefix)
        walk(self.tree)
        self.imports, self.assignments, self.ambiguous_imports = {}, {}, set()
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    binding = alias.asname or alias.name
                    if binding in self.imports:
                        self.ambiguous_imports.add(binding)
                    self.imports[binding] = (node, node.module or "", alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    binding = alias.asname or alias.name.split(".")[0]
                    if binding in self.imports:
                        self.ambiguous_imports.add(binding)
                    self.imports[binding] = (node, alias.name, None)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name):
                            self.assignments.setdefault(name.id, []).append(node)

    def select(self, node, reason, symbol=None):
        decorators = getattr(node, "decorator_list", [])
        start = min([node.lineno] + [item.lineno for item in decorators])
        end = node.end_lineno
        return {"path": str(self.path), "sha256": self.sha256, "start": start, "end": end,
                "symbol": symbol, "reason": reason, "method": "Python AST exact source",
                "source": "".join(self.lines[start - 1:end])}


def _attributes(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.insert(0, node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id] + parts
    return None


def _locals(node):
    """Bindings in this scope; nested scopes cannot shadow its global reads."""
    names, globals_ = set(), set()
    arguments = getattr(node, "args", None)
    if arguments is not None:
        names.update(arg.arg for arg in list(arguments.posonlyargs) + list(arguments.args) + list(arguments.kwonlyargs)
                     + [arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None])
    pending = list(getattr(node, "body", [])) if isinstance(getattr(node, "body", None), list) else [node.body]
    while pending:
        child = pending.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
            continue
        if isinstance(child, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or (alias.name.split(".")[0] if isinstance(child, ast.Import) else alias.name)
                         for alias in child.names)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
            names.add(child.name)
        elif isinstance(child, ast.MatchMapping) and child.rest:
            names.add(child.rest)
        elif isinstance(child, ast.Global):
            globals_.update(child.names)
        pending.extend(ast.iter_child_nodes(child))
    return names - globals_, globals_


def _global_loads(node):
    found = set()
    def visit(child, blocked=frozenset(), method_scope=None):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id not in blocked:
            found.add(child.id)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            outer = method_scope if method_scope is not None and not isinstance(child, ast.ClassDef) else blocked
            # Defaults, decorators, bases and annotations execute outside the body scope.
            for field, value in ast.iter_fields(child):
                if field != "body":
                    for expression in value if isinstance(value, list) else [value]:
                        if isinstance(expression, ast.AST):
                            visit(expression, blocked)
            local, globals_ = _locals(child)
            body_scope = (outer | local) - globals_
            body = child.body if isinstance(child.body, list) else [child.body]
            for expression in body:
                visit(expression, body_scope, outer if isinstance(child, ast.ClassDef) else None)
            return
        if isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            inner = set(blocked)
            for generator in child.generators:
                visit(generator.iter, inner)
                inner.update(target.id for target in ast.walk(generator.target) if isinstance(target, ast.Name))
                for condition in generator.ifs:
                    visit(condition, inner)
            for field in ("elt", "key", "value"):
                if hasattr(child, field):
                    visit(getattr(child, field), inner)
            return
        for expression in ast.iter_child_nodes(child):
            visit(expression, blocked, method_scope)
    visit(node)
    return sorted(found)


def related(path, symbol, workspace, *, hops=1, fanout=8, references=(), expected_hash=None, budget=24 * 1024,
            guard=None):
    """``guard(resolved, root, written)`` may raise ValueError to refuse a
    file before it is read (scripts/source.py refuses secret-looking ones)."""
    if type(hops) is not int or not 0 <= hops <= 2 or type(fanout) is not int or not 1 <= fanout <= 16:
        raise ValueError("related context requires hops 0..2 and fanout 1..16")
    if type(budget) is not int or not 1024 <= budget <= 1024 * 1024 or len(references) > 8:
        raise ValueError("related context requires a 1 KiB..1 MiB budget and at most eight reference files")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("provide an exact symbol name")
    root = workspace_root(workspace)
    snapshots, failed_snapshots, planned, keys, issues, discovered = {}, {}, [], set(), [], set()
    input_bytes, omitted_candidates = 0, 0
    def load(value):
        nonlocal input_bytes
        resolved = (root / value).resolve(strict=True)
        resolved.relative_to(root)
        if guard is not None:
            guard(resolved, root, str(value))
        if not resolved.is_file() or resolved.suffix != ".py":
            raise ValueError("automatic source context requires a Python file inside the workspace")
        if resolved in failed_snapshots:
            raise ValueError(failed_snapshots[resolved])
        if resolved not in snapshots:
            if len(snapshots) + len(failed_snapshots) >= MAX_FILES:
                raise ValueError("related source file limit reached")
            try:
                remaining = MAX_INPUT_BYTES - input_bytes
                if remaining <= 0:
                    raise ValueError("related source input allowance exceeded")
                with resolved.open("rb") as stream:
                    if os.fstat(stream.fileno()).st_size > min(MAX_FILE_BYTES, remaining):
                        raise ValueError("related source input allowance exceeded")
                    raw = stream.read(min(MAX_FILE_BYTES + 1, remaining))
                    input_bytes += len(raw)
                    if len(raw) > MAX_FILE_BYTES or os.fstat(stream.fileno()).st_size > len(raw):
                        raise ValueError("related source changed beyond the bounded input allowance")
                snapshots[resolved] = Snapshot(resolved, raw)
            except (OSError, ValueError, SyntaxError, UnicodeError, RecursionError) as exc:
                failed_snapshots[resolved] = str(exc)
                raise
        return snapshots[resolved]
    def add(snapshot, node, reason, name=None):
        nonlocal omitted_candidates
        item = snapshot.select(node, reason, name)
        key = (item["path"], item["start"], item["end"])
        if key in keys:
            return True
        if len(planned) >= fanout:
            omitted_candidates += 1
            return False
        keys.add(key)
        planned.append(item)
        return True
    primary = load(path)
    if expected_hash and expected_hash != primary.sha256:
        raise ValueError("source changed; retrieve fresh source context")
    matches = primary.definitions.get(symbol, [])
    if len(matches) != 1:
        raise ValueError("exact symbol not found or ambiguous; use exact ranges or literal search")
    add(primary, matches[0], "requested exact symbol", symbol)
    queue = [(primary, symbol, matches[0], 0)]
    if "." in symbol:
        parent = symbol.rsplit(".", 1)[0]
        parents = primary.definitions.get(parent, [])
        if len(parents) == 1:
            add(primary, parents[0], "enclosing definition context", parent)
    def module_file(snapshot, module, level):
        base = snapshot.path.parent if level else root
        for _ in range(max(0, level - 1)):
            base = base.parent
        base.relative_to(root)
        stem = base.joinpath(*module.split(".")) if module else base
        candidates = [stem.with_suffix(".py"), stem / "__init__.py"] if module else [stem / "__init__.py"]
        for candidate in candidates:
            if candidate.is_file():
                candidate.resolve(strict=True).relative_to(root)
                return candidate
        raise ValueError("imported definition is not available within the declared workspace")
    def expand(snapshot, name, node, depth):
        if depth >= hops:
            return
        loads = _global_loads(node)
        attributes = {_attributes(child) and tuple(_attributes(child)) for child in ast.walk(node) if isinstance(child, ast.Attribute)} - {None}
        for identifier in loads:
            binding_kinds = sum(identifier in bindings for bindings in (snapshot.imports, snapshot.assignments, snapshot.definitions))
            if identifier in snapshot.ambiguous_imports or binding_kinds > 1:
                issues.append({"path": str(snapshot.path), "name": identifier,
                               "reason": "ambiguous module binding; use exact source/search"})
                continue
            if identifier in snapshot.assignments:
                for declaration in snapshot.assignments[identifier]:
                    if add(snapshot, declaration, "referenced module assignment", identifier):
                        queue.append((snapshot, "<assignment:" + identifier + ">", declaration, depth + 1))
            elif identifier in snapshot.imports:
                declaration, module, imported = snapshot.imports[identifier]
                add(snapshot, declaration, "referenced import")
                names = [imported] if imported not in (None, "*") else []
                if imported is None:
                    for chain in sorted(attributes):
                        if chain[0] == identifier:
                            suffix = list(chain[1:])
                            # import pkg.mod binds pkg; import ... as alias binds alias.
                            alias = next(alias for alias in declaration.names if (alias.asname or alias.name.split(".")[0]) == identifier)
                            if not alias.asname:
                                prefix = module.split(".")[1:]
                                if suffix[:len(prefix)] != prefix:
                                    continue
                                suffix = suffix[len(prefix):]
                            if suffix:
                                names.append(".".join(suffix))
                try:
                    dependency = load(module_file(snapshot, module, getattr(declaration, "level", 0)))
                    for dependency_name in sorted(set(names)):
                        targets = dependency.definitions.get(dependency_name, [])
                        assignments = dependency.assignments.get(dependency_name, [])
                        if not targets and assignments:
                            for target in assignments:
                                if add(dependency, target, "imported assignment candidate", dependency_name):
                                    queue.append((dependency, "<assignment:" + dependency_name + ">", target, depth + 1))
                            continue
                        if len(targets) != 1:
                            raise ValueError("imported symbol is missing or ambiguous; use lexical search")
                        if add(dependency, targets[0], "imported definition candidate", dependency_name):
                            queue.append((dependency, dependency_name, targets[0], depth + 1))
                except (OSError, ValueError, SyntaxError, UnicodeError, RecursionError) as exc:
                    issues.append({"path": str(snapshot.path), "name": identifier, "reason": str(exc)})
            elif identifier in snapshot.definitions:
                targets = snapshot.definitions[identifier]
                if len(targets) == 1 and add(snapshot, targets[0], "same-file definition candidate", identifier):
                    queue.append((snapshot, identifier, targets[0], depth + 1))
                elif len(targets) != 1:
                    issues.append({"path": str(snapshot.path), "name": identifier, "reason": "ambiguous definition"})
            elif identifier not in vars(builtins) and identifier != name:
                issues.append({"path": str(snapshot.path), "name": identifier, "reason": "unresolved lexical name; use exact source/search"})
    while queue:
        snapshot, name, node, depth = queue.pop(0)
        key = (snapshot.path, name)
        if key in discovered:
            continue
        discovered.add(key)
        expand(snapshot, name, node, depth)
    for reference in references:
        try:
            candidate = load(reference)
            leaf = symbol.rsplit(".", 1)[-1]
            hits = [index + 1 for index, line in enumerate(candidate.lines) if leaf in line]
            for _, nodes in sorted(candidate.definitions.items()):
                for node in nodes:
                    if any(node.lineno <= line <= node.end_lineno for line in hits):
                        add(candidate, node, "literal reference/test candidate")
            for line in hits:
                if not any(node.lineno <= line <= node.end_lineno for nodes in candidate.definitions.values() for node in nodes):
                    node = next((node for node in candidate.tree.body if node.lineno <= line <= node.end_lineno), None)
                    if node is not None:
                        add(candidate, node, "literal reference outside a definition")
        except (OSError, ValueError, SyntaxError, UnicodeError, RecursionError) as exc:
            issues.append({"path": str(reference), "reason": str(exc)})
    output = {"schema_version": 1, "kind": "source_context", "workspace": str(root),
              "budget_bytes": budget, "reference_scope": "only explicitly supplied reference files",
              "selection": "bounded static candidates; not semantic closure", "semantic_closure": False,
              "snapshot_scope": "one byte snapshot per resolved file", "hops": hops, "fanout": fanout,
              "input_bytes": input_bytes, "omitted_candidates": omitted_candidates,
              "issues": issues[:32], "omitted_issues": max(0, len(issues) - 32), "complete": False,
              "results": [{key: value for key, value in item.items() if key != "source"}
                          | {"status": "output_budget_exceeded", "exact_evidence_included": False} for item in planned]}
    if len(serialized(output).encode("utf-8")) > budget:
        raise ValueError("related context metadata exceeds budget; increase budget or reduce fanout")
    fill_budget(output, [(index, dict(item, status="ok", exact_evidence_included=True))
                         for index, item in enumerate(planned)], budget)
    output["complete"] = not issues and omitted_candidates == 0 and all(item["status"] == "ok" for item in output["results"])
    return output
