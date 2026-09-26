"""Render a Report as paste-ready markdown (or JSON).

Sections: header + summary -> changed symbols -> blast radius -> pre-flight
checklist -> rollback runbook -> limitations. Output is deterministic (no
timestamps or absolute paths) so it can be golden-tested and pasted into a PR.
"""
from __future__ import annotations

import dataclasses
import json
from collections import defaultdict

from .diff_parser import classify
from .models import Caller, ChangedSymbol, ChangeType, FileKind, Report, Severity

LIMITATIONS = (
    "Blast radius is heuristic (import graph + AST call search): it misses dynamic "
    "dispatch, getattr, and dependency injection. The runbook is grounded in repo "
    "artifacts but must be reviewed by a human before running in production."
)

_CHANGE_LABEL = {
    ChangeType.ADDED: "added",
    ChangeType.REMOVED: "**removed**",
    ChangeType.BODY_CHANGED: "body changed",
    ChangeType.SIGNATURE_CHANGED: "**signature changed**",
}


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _symbol_change(s: ChangedSymbol) -> str:
    if s.change == ChangeType.SIGNATURE_CHANGED:
        return f"{_CHANGE_LABEL[s.change]}: `{s.old_sig}` → `{s.new_sig}`"
    return _CHANGE_LABEL[s.change]


def _header(report: Report) -> list[str]:
    d, b = report.diff, report.blast
    counts = {sev: sum(1 for i in report.checklist if i.severity == sev) for sev in Severity}
    app_sites = [c for c in b.callers if classify(c.file) != FileKind.TEST]
    call_files = {c.file for c in b.callers}
    reach = (f"{_plural(len(b.callers), 'call site')} in {_plural(len(call_files), 'file')} "
             f"({len(app_sites)} in app code)" if b.callers else "no existing callers affected")
    return [
        f"## Blast radius report: `{d.base}...{d.head}`",
        "",
        f"`{d.base_sha[:8]}` → `{d.head_sha[:8]}` · {_plural(len(d.commits), 'commit')} · "
        f"{_plural(len(d.files), 'file')} changed · {_plural(b.files_scanned, 'Python file')} scanned",
        "",
        f"**Summary:** {counts[Severity.HIGH]} high · {counts[Severity.MED]} med · {counts[Severity.LOW]} low "
        f"checklist items · {reach} · rollback via {report.runbook.source}",
    ]


def _changed_symbols(report: Report) -> list[str]:
    order = {ChangeType.REMOVED: 0, ChangeType.SIGNATURE_CHANGED: 1, ChangeType.BODY_CHANGED: 2, ChangeType.ADDED: 3}
    reach: dict[str, int] = defaultdict(int)
    for c in report.blast.callers:
        reach[c.root_symbol] += 1
    symbols = sorted(report.diff.symbols, key=lambda s: (order[s.change], -reach[s.fq_name], s.file, s.lineno))
    lines = ["### Changed code", ""]
    if symbols:
        lines += ["| Symbol | Change | Where |", "| --- | --- | --- |"]
        for s in symbols:
            lines.append(f"| `{s.qualname}` | {_cell(_symbol_change(s))} | {s.file}:{s.lineno} |")
        lines.append("")
    other = [f for f in report.diff.files if f.kind not in (FileKind.PYTHON, FileKind.TEST)]
    if other:
        lines.append("Other files: " + ", ".join(f"`{f.path}` ({f.kind.value})" for f in other))
        lines.append("")
    if not symbols and not other:
        lines += ["No code changes detected.", ""]
    return lines


def _caller_row(c: Caller) -> str:
    where = f"`{c.enclosing_symbol}`" if c.enclosing_symbol else "module level"
    if c.usage == "call" and c.call:
        what = f"`{_cell(c.call.text)}`"
    else:
        what = "_referenced as a value_"
    if c.confidence == "low":
        what += " _(matched by method name only)_"
    via = "" if c.hop == 1 else f" via `{c.via_symbol.rsplit('.', 1)[-1]}`"
    return f"| {c.hop}{via} | {c.file}:{c.line} | {where} | {what} |"


def _blast(report: Report) -> list[str]:
    b = report.blast
    lines = ["### Blast radius", ""]
    if not b.callers and not b.importers:
        return lines + ["No callers or importers found for the changed code.", ""]

    by_root: dict[str, list[Caller]] = defaultdict(list)
    for c in b.callers:
        by_root[c.root_symbol].append(c)
    for root, callers in sorted(by_root.items(), key=lambda kv: -len(kv[1])):
        hop1 = sum(1 for c in callers if c.hop == 1)
        hop2 = len(callers) - hop1
        detail = _plural(hop1, "direct call site") + (f", {_plural(hop2, 'second-hop caller')}" if hop2 else "")
        lines += [
            f"**`{root}`**: {detail}",
            "",
            "| Hop | Location | In | Usage |",
            "| --- | --- | --- | --- |",
            *[_caller_row(c) for c in callers],
            "",
        ]
    if b.truncated:
        lines += ["_Second-hop search was capped; some indirect callers are not shown._", ""]

    if b.importers:
        by_module: dict[str, list[str]] = defaultdict(list)
        for i in b.importers:
            by_module[i.module].append(f"{i.file}:{i.line}")
        lines += ["<details><summary>" + _plural(len(b.importers), "import") + " of changed modules</summary>", ""]
        lines += [f"- `{m}`: " + ", ".join(locs) for m, locs in by_module.items()]
        lines += ["", "</details>", ""]
    return lines


def _checklist(report: Report) -> list[str]:
    lines = ["### Pre-flight checklist", ""]
    if not report.checklist:
        return lines + ["Nothing specific to check for this change.", ""]
    for item in report.checklist:
        lines.append(f"- [ ] **{item.severity.value.upper()}**: {item.text}")
        for e in item.evidence:
            if e not in item.text:
                lines.append(f"  - {e}")
    return lines + [""]


def _runbook(report: Report) -> list[str]:
    rb = report.runbook
    lines = ["### Rollback runbook", "", f"_Generated by: {rb.source}_", ""]
    if rb.rejections:
        lines += ["<details><summary>Why the Bob draft was not used</summary>", ""]
        lines += [f"- {r}" for r in rb.rejections]
        lines += ["", "</details>", ""]
    return lines + [rb.markdown.rstrip(), ""]


def render_markdown(report: Report) -> str:
    lines = [
        *_header(report), "",
        *_changed_symbols(report),
        *_blast(report),
        *_checklist(report),
        *_runbook(report),
        "---",
        f"<sub>{LIMITATIONS}</sub>",
        "",
    ]
    return "\n".join(lines)


def render_json(report: Report) -> str:
    data = dataclasses.asdict(report)
    for f in data["diff"]["files"]:  # large and redundant with the diff itself
        f.pop("added_lines", None)
        f.pop("removed_lines", None)
    return json.dumps(data, indent=2, default=str)
