"""JSON-ready view of a Report for the web dashboard (live server and static HTML export).

Everything the UI shows is derived here, in Python, from the same data as the
markdown and terminal views. The browser only lays it out.
"""
from __future__ import annotations

import re

from markdown_it import MarkdownIt

from .checklist import call_verdicts
from .diff_parser import classify, path_to_module
from .models import Caller, ChangeType, FileKind, Report, Severity
from .report import LIMITATIONS

# Bob's output is untrusted: raw HTML in it is escaped, never rendered.
_MD = MarkdownIt("commonmark", {"html": False}).enable("table")

RISK_WEIGHTS = {"high": 25, "med": 8, "low": 2, "broken_call": 5}


def _is_test(path: str) -> bool:
    return classify(path) == FileKind.TEST


def risk(report: Report, broken: int) -> dict:
    counts = {s.value: sum(1 for i in report.checklist if i.severity == s) for s in Severity}
    raw = sum(RISK_WEIGHTS[k] * n for k, n in counts.items()) + RISK_WEIGHTS["broken_call"] * broken
    score = min(100, raw)
    level = "High" if score >= 50 else "Elevated" if score >= 20 else "Low"
    return {"score": score, "level": level, "counts": counts, "broken_calls": broken, "weights": RISK_WEIGHTS}


def _runbook_status(source: str) -> tuple[str, str]:
    return {
        "bob (validated)": ("validated", "Bob · validated against repo"),
        "template (bob rejected)": ("rejected", "Bob draft rejected → deterministic template"),
        "template (bob unavailable)": ("unavailable", "Bob unavailable → deterministic template"),
    }.get(source, ("template", "Deterministic template"))


def _caller(c: Caller, verdicts: dict) -> dict:
    key = (c.file, c.line)
    verdict = None
    if key in verdicts:
        verdict = "breaks" if verdicts[key] else "fits"
    return {
        "file": c.file, "line": c.line, "in": c.enclosing_symbol, "hop": c.hop,
        "via": c.via_symbol, "usage": c.usage, "confidence": c.confidence,
        "call": c.call.text if c.call else None, "is_test": _is_test(c.file),
        "verdict": verdict, "reason": verdicts.get(key), "children": [],
    }


def build_view(report: Report, timings: dict[str, float] | None = None) -> dict:
    d, b = report.diff, report.blast
    verdicts = call_verdicts(d, b)
    n_broken = sum(1 for r in verdicts.values() if r)

    # blast radius tree: root symbol -> hop-1 callers -> hop-2 callers
    roots: dict[str, dict] = {}
    hop1_nodes: dict[str, dict] = {}
    changes = {s.fq_name: s for s in d.symbols}
    for c in b.callers:
        root = roots.setdefault(c.root_symbol, {
            "symbol": c.root_symbol,
            "change": changes[c.root_symbol].change.value if c.root_symbol in changes else None,
            "callers": [],
        })
        if c.hop == 1:
            node = _caller(c, verdicts)
            root["callers"].append(node)
            if c.enclosing_symbol:
                hop1_nodes[f"{path_to_module(c.file)}.{c.enclosing_symbol}"] = node
    for c in b.callers:
        if c.hop == 2:
            parent = hop1_nodes.get(c.via_symbol)
            (parent["children"] if parent else roots[c.root_symbol]["callers"]).append(_caller(c, verdicts))
    for r in roots.values():
        r["breaks"] = sum(1 for n in r["callers"] if n["verdict"] == "breaks")
    tree = sorted(roots.values(), key=lambda r: -len(r["callers"]))

    # call sites per file (bar chart)
    per_file: dict[str, dict] = {}
    for c in b.callers:
        f = per_file.setdefault(c.file, {"file": c.file, "calls": 0, "breaks": 0, "is_test": _is_test(c.file)})
        f["calls"] += 1
        f["breaks"] += 1 if verdicts.get((c.file, c.line)) else 0
    by_file = sorted(per_file.values(), key=lambda f: (-f["calls"], f["file"]))[:8]

    # changed symbols with their reach
    order = {ChangeType.REMOVED: 0, ChangeType.SIGNATURE_CHANGED: 1, ChangeType.BODY_CHANGED: 2, ChangeType.ADDED: 3}
    symbols = []
    for s in sorted(d.symbols, key=lambda s: (order[s.change], s.file, s.lineno)):
        callers = [c for c in b.callers if c.root_symbol == s.fq_name]
        direct = [c for c in callers if c.via_symbol == s.fq_name]
        symbols.append({
            "name": s.qualname, "fq": s.fq_name, "kind": s.kind, "change": s.change.value,
            "file": s.file, "line": s.lineno, "old_sig": s.old_sig, "new_sig": s.new_sig,
            "callers": len(callers),
            "breaks": sum(1 for c in direct if verdicts.get((c.file, c.line))),
        })
    symbols.sort(key=lambda s: (order[ChangeType(s["change"])], -s["callers"]))

    status, label = _runbook_status(report.runbook.source)
    runbook_md = report.runbook.markdown
    f = report.facts
    app_calls = sum(1 for c in b.callers if not _is_test(c.file))

    return {
        "range": f"{d.base}...{d.head}",
        "base": d.base, "head": d.head, "base_sha": d.base_sha, "head_sha": d.head_sha,
        "commits": d.commits,
        "files": [{"path": x.path, "kind": x.kind.value, "status": x.status} for x in d.files],
        "stats": {
            "call_sites": len(b.callers), "app_call_sites": app_calls, "call_files": len(per_file),
            "broken_calls": n_broken, "files_scanned": b.files_scanned, "files_changed": len(d.files),
            "high": sum(1 for i in report.checklist if i.severity == Severity.HIGH),
            "checklist": len(report.checklist),
            "runbook_steps": len(re.findall(r"^\d+\.\s", runbook_md, flags=re.MULTILINE)),
        },
        "timings": {k: round(v, 2) for k, v in (timings or {}).items()},
        "risk": risk(report, n_broken),
        "by_file": by_file,
        "tree": tree,
        "importers": [{"file": i.file, "line": i.line, "module": i.module} for i in b.importers],
        "truncated": b.truncated,
        "symbols": symbols,
        "checklist": [{"severity": i.severity.value, "rule": i.rule_id, "text": i.text,
                       "evidence": [e for e in i.evidence if e not in i.text]} for i in report.checklist],
        "runbook": {
            "status": status, "label": label, "source": report.runbook.source,
            "html": _MD.render(runbook_md), "markdown": runbook_md,
            "rejections": report.runbook.rejections,
        },
        "facts": {
            "workloads": [{"kind": w.kind, "name": w.name, "namespace": w.namespace} for w in f.k8s_deployments],
            "migrations": [{"revision": m.revision, "down": m.down_revision, "ops": [str(o) for o in m.upgrade_ops]}
                           for m in f.migrations if m.in_diff],
            "flags": [{"name": x.name, "file": x.file, "line": x.line} for x in f.feature_flags],
            "env_vars": [{"name": e.name, "declared": bool(f.declared_env.get(e.name)), "at_import": e.at_import}
                         for e in f.new_env_vars],
        },
        "limitations": LIMITATIONS,
    }
