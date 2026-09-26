"""Component 2: combine importers + hop-1/hop-2 callers into a ranked BlastRadius.

The search runs against the head revision, so for removed or re-signed symbols
it finds the call sites that still exist after the PR — the ones that can break.
Added symbols are skipped: nothing outside the PR can depend on them yet.
"""
from __future__ import annotations

from pathlib import Path

from .callers import Target, find_callers
from .diff_parser import classify
from .import_graph import ImportGraph, build_import_graph
from .models import BlastRadius, Caller, ChangeType, DiffResult, FileKind, Importer

MAX_FANOUT = 50  # max hop-1 callers expanded into hop-2 searches


def _targets(diff: DiffResult) -> list[Target]:
    kinds = {f.path: f.kind for f in diff.files}
    seen: dict[str, Target] = {}
    for s in diff.symbols:
        if s.change == ChangeType.ADDED or kinds.get(s.file) != FileKind.PYTHON:
            continue
        seen.setdefault(s.fq_name, Target(s.module, s.qualname))
    return list(seen.values())


def _is_test_file(path: str) -> bool:
    return classify(path) == FileKind.TEST


def _importers(graph: ImportGraph, modules: set[str]) -> list[Importer]:
    found: dict[tuple[str, int, str], Importer] = {}
    for module in sorted(modules):
        for e in graph.importers_of(module):
            if e.importer_module != module:
                found.setdefault((e.importer_file, e.line, module), Importer(e.importer_file, e.line, module))
    return sorted(found.values(), key=lambda i: (_is_test_file(i.file), i.file, i.line))


def compute_blast_radius(repo: Path, diff: DiffResult, hops: int = 2, graph: ImportGraph | None = None) -> BlastRadius:
    graph = graph or build_import_graph(repo, diff.head_sha)
    targets = _targets(diff)

    hop1: list[Caller] = []
    for t in targets:
        hop1.extend(find_callers(graph, t, hop=1))

    hop2: list[Caller] = []
    truncated = False
    if hops >= 2:
        expand: dict[str, tuple[Target, str]] = {}
        for c in hop1:
            if c.enclosing_symbol is None or _is_test_file(c.file):
                continue
            t = Target(graph.module_of(c.file), c.enclosing_symbol)
            expand.setdefault(t.fq, (t, c.root_symbol))
        if len(expand) > MAX_FANOUT:
            truncated = True
        for t, root in list(expand.values())[:MAX_FANOUT]:
            hop2.extend(find_callers(graph, t, hop=2, root=root))

    unique: dict[tuple[str, int, str], Caller] = {}
    for c in hop1 + hop2:
        unique.setdefault((c.file, c.line, c.via_symbol), c)
    callers = sorted(
        unique.values(),
        key=lambda c: (c.hop, c.confidence != "high", _is_test_file(c.file), c.file, c.line),
    )

    return BlastRadius(
        importers=_importers(graph, {t.module for t in targets}),
        callers=callers,
        files_scanned=len(graph.sources),
        truncated=truncated,
    )
