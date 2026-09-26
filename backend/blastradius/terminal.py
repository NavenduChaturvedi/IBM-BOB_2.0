"""Rich terminal view of a Report: for people at a terminal (and the demo stage).

Markdown (report.py) stays the format for files and PR descriptions; this view
uses color and structure that don't survive a paste:
  header panel with severity counts -> blast radius as a caller tree, with calls
  that break the new signature marked in red -> checklist with severity badges ->
  runbook in a panel colored by where it came from (Bob validated / template /
  Bob rejected, with the rejection reasons shown, not hidden).
"""
from __future__ import annotations

import re

from rich import box
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from .checklist import call_verdicts
from .diff_parser import classify, path_to_module
from .models import Caller, ChangedSymbol, ChangeType, FileKind, Report, Severity
from .report import LIMITATIONS

SEV_BADGE = {Severity.HIGH: "bold white on red", Severity.MED: "bold black on yellow",
             Severity.LOW: "bold white on blue"}
SEV_TEXT = {Severity.HIGH: "bold red", Severity.MED: "bold yellow", Severity.LOW: "bold blue"}
CHANGE_TEXT = {ChangeType.ADDED: ("added", "green"), ChangeType.REMOVED: ("removed", "bold red"),
               ChangeType.BODY_CHANGED: ("body changed", "yellow"),
               ChangeType.SIGNATURE_CHANGED: ("signature changed", "bold magenta")}


def _inline(text: str, base: str = "") -> Text:
    """Render `code` spans in cyan, the rest in `base` style."""
    out = Text(style=base)
    for i, part in enumerate(re.split(r"`([^`]*)`", text)):
        out.append(part, style="cyan" if i % 2 else None)
    return out


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _is_test(path: str) -> bool:
    return classify(path) == FileKind.TEST


def _runbook_status(source: str) -> tuple[str, str]:
    if source == "bob (validated)":
        return "Bob ✓ validated against repo", "green"
    if source == "template (bob rejected)":
        return "Bob draft rejected → deterministic template", "yellow"
    if source == "template (bob unavailable)":
        return "Bob unavailable → deterministic template", "yellow"
    return "deterministic template", "cyan"


# --- sections -----------------------------------------------------------------------

def _header(report: Report, timings: dict[str, float]) -> Panel:
    d, b = report.diff, report.blast
    counts = {s: sum(1 for i in report.checklist if i.severity == s) for s in Severity}

    title = Text.assemble(("blastradius", "bold magenta"), ("  ·  ", "dim"), (f"{d.base}...{d.head}", "bold"))
    meta = Text(
        f"{d.base_sha[:8]} → {d.head_sha[:8]}  ·  {_plural(len(d.commits), 'commit')}  ·  "
        f"{_plural(len(d.files), 'file')} changed  ·  {_plural(b.files_scanned, 'Python file')} scanned",
        style="dim",
    )
    summary = Text()
    for sev in Severity:
        summary.append(f" {counts[sev]} {sev.value.upper()} ", style=SEV_BADGE[sev] if counts[sev] else "dim")
        summary.append("  ")
    app_sites = sum(1 for c in b.callers if not _is_test(c.file))
    reach = (f"{_plural(len(b.callers), 'call site')} ({app_sites} in app code)"
             if b.callers else "no existing callers affected")
    summary.append(f"  {reach}")
    label, color = _runbook_status(report.runbook.source)
    runbook = Text.assemble(("Runbook: ", "dim"), (label, f"bold {color}"))
    if timings:
        runbook.append("   " + "  ·  ".join(f"{k} {v:.1f}s" for k, v in timings.items()), style="dim")
    return Panel(Group(title, meta, Text(), summary, runbook), box=box.ROUNDED, border_style="magenta",
                 padding=(1, 2))


def _changed(report: Report) -> RenderableType:
    order = {ChangeType.REMOVED: 0, ChangeType.SIGNATURE_CHANGED: 1, ChangeType.BODY_CHANGED: 2, ChangeType.ADDED: 3}
    reach: dict[str, int] = {}
    for c in report.blast.callers:
        reach[c.root_symbol] = reach.get(c.root_symbol, 0) + 1
    symbols = sorted(report.diff.symbols, key=lambda s: (order[s.change], -reach.get(s.fq_name, 0), s.file, s.lineno))

    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="bold dim")
    table.add_column("Symbol", style="bold")
    table.add_column("Change")
    table.add_column("Where", style="dim", no_wrap=True)
    for s in symbols:
        label, style = CHANGE_TEXT[s.change]
        change = Text(label, style=style)
        if s.change == ChangeType.SIGNATURE_CHANGED:
            change = Text.assemble((label, style), "\n", (s.old_sig or "", "dim strike"), "\n", (s.new_sig or "", "cyan"))
        table.add_row(s.qualname, change, f"{s.file}:{s.lineno}")
    for f in report.diff.files:
        if f.kind not in (FileKind.PYTHON, FileKind.TEST):
            table.add_row(Text(f.path, style="bold"), Text(f.kind.value, style="yellow"), "")
    if not table.rows:
        return Text("No code changes detected.", style="dim")
    return table


def _caller_label(c: Caller, broken: dict[tuple[str, int], str], compatible: set[tuple[str, int]]) -> Text:
    test = _is_test(c.file)
    key = (c.file, c.line)
    label = Text()
    if key in broken:
        label.append("✗ ", style="bold red")
    elif key in compatible:
        label.append("✓ ", style="green")
    else:
        label.append("• ", style="dim")
    label.append(f"{c.file}:{c.line}", style="dim" if test and key not in broken else "bold")
    label.append(f"  in {c.enclosing_symbol or 'module level'}", style="dim")
    if c.confidence == "low":
        label.append("  (matched by method name)", style="dim italic")
    if c.usage == "call" and c.call:
        label.append("\n  ")
        label.append(c.call.text, style="red" if key in broken else "cyan dim")
    elif c.usage == "reference":
        label.append("\n  passed as a value", style="dim italic")
    if key in broken:
        label.append("\n  ")
        label.append_text(_inline(broken[key], "bold red"))
    return label


def _blast(report: Report) -> RenderableType:
    b = report.blast
    if not b.callers:
        importers = ", ".join(f"{i.file}:{i.line}" for i in b.importers)
        return Text("No existing callers affected." + (f"  Imported by {importers}." if importers else ""), style="dim")

    symbols: dict[str, ChangedSymbol] = {s.fq_name: s for s in report.diff.symbols}
    verdicts = call_verdicts(report.diff, b)
    broken = {key: reason for key, reason in verdicts.items() if reason}
    compatible = {key for key, reason in verdicts.items() if reason is None}

    by_root: dict[str, list[Caller]] = {}
    for c in b.callers:
        by_root.setdefault(c.root_symbol, []).append(c)

    trees = []
    for root, callers in sorted(by_root.items(), key=lambda kv: -len(kv[1])):
        sym = symbols.get(root)
        head = Text(root, style="bold")
        if sym:
            label, style = CHANGE_TEXT[sym.change]
            head.append(f"  {label}", style=style)
        n_broken = sum(1 for c in callers if (c.file, c.line) in broken)
        if n_broken:
            head.append(f"  {n_broken} call{'s' if n_broken != 1 else ''} will fail", style="bold white on red")
        tree = Tree(head, guide_style="dim")
        nodes: dict[str, Tree] = {}
        for c in callers:
            if c.hop == 1:
                node = tree.add(_caller_label(c, broken, compatible))
                if c.enclosing_symbol:
                    nodes[f"{path_to_module(c.file)}.{c.enclosing_symbol}"] = node
        for c in callers:
            if c.hop == 2:
                nodes.get(c.via_symbol, tree).add(_caller_label(c, broken, compatible))
        trees.append(tree)

    if b.importers:
        trees.append(Text(f"{_plural(len(b.importers), 'import')} of changed modules: "
                          + ", ".join(f"{i.file}:{i.line}" for i in b.importers[:6])
                          + (" …" if len(b.importers) > 6 else ""), style="dim"))
    if b.truncated:
        trees.append(Text("Second-hop search was capped; some indirect callers are not shown.", style="yellow"))
    return Group(*trees)


def _checklist(report: Report) -> RenderableType:
    if not report.checklist:
        return Text("Nothing specific to check for this change.", style="dim")
    table = Table.grid(padding=(0, 1))
    table.add_column(no_wrap=True)
    table.add_column()
    for item in report.checklist:
        body = _inline(item.text)
        extra = [e for e in item.evidence if e not in item.text]
        for e in extra:
            body.append("\n  ")
            body.append_text(_inline(e, "dim"))
        table.add_row(Text(f" {item.severity.value.upper():<4} ", style=SEV_BADGE[item.severity]), body)
        table.add_row("", "")
    return table


def _runbook(report: Report) -> RenderableType:
    rb = report.runbook
    label, color = _runbook_status(rb.source)
    parts: list[RenderableType] = []
    if rb.rejections:
        lines = Text()
        for i, r in enumerate(rb.rejections):
            if i:
                lines.append("\n")
            lines.append("✗ ", style="bold red")
            lines.append_text(_inline(r))
        parts.append(Panel(lines, title="[bold yellow]Bob's draft was rejected[/]", title_align="left",
                           border_style="yellow", box=box.ROUNDED, padding=(0, 1)))
    parts.append(Panel(Markdown(rb.markdown, code_theme="ansi_dark"), title=f"[bold {color}]{label}[/]",
                       title_align="left", border_style=color, box=box.ROUNDED, padding=(1, 2)))
    return Group(*parts)


def _section(title: str, subtitle: str = "") -> Text:
    t = Text.assemble(("\n" + title, "bold underline"))
    if subtitle:
        t.append(f"  {subtitle}", style="dim italic")
    return t


def render(report: Report, console: Console, timings: dict[str, float] | None = None) -> None:
    console.print(_header(report, timings or {}))
    console.print(_section("Changed code"))
    console.print(_changed(report))
    console.print(_section("Blast radius", "import graph + AST call search, 2 hops"))
    console.print(_blast(report))
    console.print(_section("Pre-flight checklist"))
    console.print(_checklist(report))
    console.print(_section("Rollback runbook"))
    console.print(_runbook(report))
    console.print()
    console.print(Text(LIMITATIONS, style="dim italic"))
