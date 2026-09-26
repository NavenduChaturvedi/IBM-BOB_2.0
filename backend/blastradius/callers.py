"""Component 2b: find where a symbol is used, resolving each file's import aliases.

For a target `pkg.mod.f`, each candidate file gets a set of dotted patterns that
refer to it in that file's namespace, e.g. `f`, `g` (from pkg.mod import f as g),
`mod.f` (from pkg import mod), `pkg.mod.f` (import pkg.mod). Name/Attribute nodes
whose dotted form matches a pattern are hits; hits in call position are calls,
the rest are references (callbacks, attribute reads).

Heuristic by design: misses getattr, dynamic dispatch, and dependency injection.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath

from .import_graph import ImportGraph
from .models import CallShape, Caller


@dataclass(frozen=True)
class Target:
    module: str
    qualname: str  # f | C | C.m | VAR

    @property
    def fq(self) -> str:
        return f"{self.module}.{self.qualname}"

    @property
    def is_method(self) -> bool:
        return "." in self.qualname


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _locations(graph: ImportGraph, target: Target) -> list[tuple[str, str]]:
    """(module, qualname) pairs the target is reachable as, including package __init__ re-exports."""
    head, _, rest = target.qualname.partition(".")
    suffix = f".{rest}" if rest else ""
    locs = [(target.module, target.qualname)]
    for e in graph.importers_of(target.module):
        if PurePosixPath(e.importer_file).name == "__init__.py":
            locs += [(e.importer_module, local + suffix) for local, name in e.names.items() if name == head]
    return locs


def _patterns(graph: ImportGraph, file: str, locations: list[tuple[str, str]]) -> set[str]:
    pats: set[str] = set()
    file_module = graph.module_of(file)
    for module, qualname in locations:
        head, _, rest = qualname.partition(".")
        suffix = f".{rest}" if rest else ""
        if file_module == module:
            pats.add(qualname)
        for e in graph.edges_from(file):
            if e.alias and (module == e.imported_module or module.startswith(e.imported_module + ".")):
                pats.add(f"{e.alias}{module[len(e.imported_module):]}.{qualname}")
            if e.imported_module == module:
                for local, name in e.names.items():
                    if name == head:
                        pats.add(local + suffix)
                    elif name == "*":
                        pats.add(qualname)
    return pats


def _call_shape(call: ast.Call) -> CallShape:
    text = ast.unparse(call)
    return CallShape(
        n_positional=sum(1 for a in call.args if not isinstance(a, ast.Starred)),
        keywords=[k.arg for k in call.keywords if k.arg],
        has_star_args=any(isinstance(a, ast.Starred) for a in call.args),
        has_star_kwargs=any(k.arg is None for k in call.keywords),
        text=text if len(text) <= 120 else text[:117] + "...",
    )


class _Finder(ast.NodeVisitor):
    def __init__(self, patterns: set[str], target: Target):
        self.patterns = patterns
        self.owner_class, _, self.method = target.qualname.rpartition(".") if target.is_method else ("", "", "")
        self.stack: list[tuple[str, str]] = []  # (class|def, name)
        self.hits: list[tuple[int, str | None, str, str, CallShape | None]] = []

    # scope tracking
    def _scoped(self, kind: str, node: ast.AST) -> None:
        self.stack.append((kind, node.name))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self._scoped("def", node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._scoped("class", node)

    def _enclosing(self) -> str | None:
        """Qualname of the nearest callable: classes up to and including the first def."""
        names = []
        for kind, name in self.stack:
            names.append(name)
            if kind == "def":
                return ".".join(names)
        return ".".join(names) if names else None

    def _in_owner_class(self) -> bool:
        return any(kind == "class" and name == self.owner_class for kind, name in self.stack)

    # matching
    def _match(self, node: ast.AST) -> str | None:
        """Return confidence if node refers to the target, else None."""
        dotted = dotted_name(node)
        if dotted is None:
            return None
        if dotted in self.patterns:
            return "high"
        if self.method and isinstance(node, ast.Attribute) and node.attr == self.method:
            if dotted in (f"self.{self.method}", f"cls.{self.method}") and self._in_owner_class():
                return "high"
            if self.patterns:  # file can see the class; instance.method() is plausible
                return "low"
        return None

    def _record(self, node: ast.AST, confidence: str, call: ast.Call | None) -> None:
        self.hits.append((
            node.lineno,
            self._enclosing(),
            "call" if call else "reference",
            confidence,
            _call_shape(call) if call else None,
        ))

    def visit_Call(self, node: ast.Call):
        confidence = self._match(node.func)
        if confidence:
            self._record(node.func, confidence, node)
            if isinstance(node.func, ast.Attribute):
                self.visit(node.func.value)
        else:
            self.visit(node.func)
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            self.visit(kw.value)

    def visit_Attribute(self, node: ast.Attribute):
        confidence = self._match(node)
        if confidence == "high":
            self._record(node, confidence, None)
        else:
            self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load) and node.id in self.patterns:
            self._record(node, "high", None)


def candidate_files(graph: ImportGraph, locations: list[tuple[str, str]], name: str) -> list[str]:
    files: set[str] = set()
    for module, _ in locations:
        if module in graph.modules:
            files.add(graph.modules[module])
        for e in graph.edges:
            if module == e.imported_module or module.startswith(e.imported_module + "."):
                files.add(e.importer_file)
    # cheap text pre-filter (what ripgrep would do, without a subprocess)
    return sorted(f for f in files if name in graph.sources.get(f, ""))


def find_callers(graph: ImportGraph, target: Target, hop: int = 1, root: str | None = None) -> list[Caller]:
    locations = _locations(graph, target)
    last_name = target.qualname.rsplit(".", 1)[-1]
    callers: list[Caller] = []
    for file in candidate_files(graph, locations, last_name):
        tree = graph.tree(file)
        if tree is None:
            continue
        finder = _Finder(_patterns(graph, file, locations), target)
        finder.visit(tree)
        for line, enclosing, usage, confidence, shape in finder.hits:
            # the target's own body referring to itself isn't blast radius
            if file == graph.modules.get(target.module) and enclosing == target.qualname:
                continue
            callers.append(Caller(
                file=file,
                line=line,
                enclosing_symbol=enclosing,
                hop=hop,
                via_symbol=target.fq,
                root_symbol=root or target.fq,
                usage=usage,
                confidence=confidence,
                call=shape,
            ))
    return callers
