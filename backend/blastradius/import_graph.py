"""Component 2a: repo-wide import graph built with ast at a given revision (no grimp).

Every .py file at the revision is read from git (not the working tree), parsed
once, and cached so the caller search can reuse the trees.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import gitio
from .diff_parser import path_to_module

EXCLUDED_DIRS = {".venv", "venv", "env", "node_modules", "site-packages", "__pycache__",
                 "build", "dist", ".tox", ".git", ".mypy_cache", ".pytest_cache"}


@dataclass
class ImportEdge:
    importer_file: str
    importer_module: str
    imported_module: str
    line: int
    # local name bound to the module itself: `import a.b as m` -> "m", bare `import a.b` -> "a.b",
    # `from a import b` where b is a submodule -> "b"
    alias: str | None = None
    names: dict[str, str] = field(default_factory=dict)  # `from m import x as y` -> {"y": "x"}


@dataclass
class ImportGraph:
    rev: str
    modules: dict[str, str]  # module -> file
    sources: dict[str, str]  # file -> source
    edges: list[ImportEdge]
    _trees: dict[str, ast.Module | None] = field(default_factory=dict, repr=False)
    _by_file: dict[str, list[ImportEdge]] = field(default_factory=dict, repr=False)
    _by_target: dict[str, list[ImportEdge]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for e in self.edges:
            self._by_file.setdefault(e.importer_file, []).append(e)
            self._by_target.setdefault(e.imported_module, []).append(e)

    def tree(self, file: str) -> ast.Module | None:
        if file not in self._trees:
            try:
                self._trees[file] = ast.parse(self.sources[file])
            except (SyntaxError, KeyError):
                self._trees[file] = None
        return self._trees[file]

    def module_of(self, file: str) -> str:
        return path_to_module(file)

    def edges_from(self, file: str) -> list[ImportEdge]:
        return self._by_file.get(file, [])

    def importers_of(self, module: str) -> list[ImportEdge]:
        return self._by_target.get(module, [])


def _is_python_source(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix == ".py" and not any(part in EXCLUDED_DIRS for part in p.parts[:-1])


def resolve_relative(importer_module: str, is_package: bool, level: int, module: str | None) -> str:
    parts = importer_module.split(".") if importer_module else []
    if not is_package:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: max(len(parts) - (level - 1), 0)]
    return ".".join(p for p in (".".join(parts), module) if p)


def _edges_for_file(file: str, tree: ast.Module, known_modules: set[str]) -> list[ImportEdge]:
    importer = path_to_module(file)
    is_package = PurePosixPath(file).name == "__init__.py"
    edges: list[ImportEdge] = []

    for node in ast.walk(tree):  # includes function-local imports
        if isinstance(node, ast.Import):
            for a in node.names:
                edges.append(ImportEdge(file, importer, a.name, node.lineno, alias=a.asname or a.name))
        elif isinstance(node, ast.ImportFrom):
            mod = (resolve_relative(importer, is_package, node.level, node.module)
                   if node.level else (node.module or ""))
            names: dict[str, str] = {}
            for a in node.names:
                local = a.asname or a.name
                submodule = f"{mod}.{a.name}"
                if submodule in known_modules:
                    edges.append(ImportEdge(file, importer, submodule, node.lineno, alias=local))
                else:
                    names[local] = a.name
            if names:
                edges.append(ImportEdge(file, importer, mod, node.lineno, names=names))
    return edges


def build_import_graph(repo: Path, rev: str) -> ImportGraph:
    files = [f for f in gitio.list_files(repo, rev) if _is_python_source(f)]
    sources = gitio.read_files(repo, rev, files)
    modules = {path_to_module(f): f for f in sources}

    trees: dict[str, ast.Module | None] = {}
    for f, source in sources.items():
        try:
            trees[f] = ast.parse(source)
        except SyntaxError:
            trees[f] = None

    known = set(modules)
    edges = [e for f, t in trees.items() if t is not None for e in _edges_for_file(f, t, known)]
    return ImportGraph(rev=rev, modules=modules, sources=sources, edges=edges, _trees=trees)
