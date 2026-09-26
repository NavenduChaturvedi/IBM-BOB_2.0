"""Component 3: rule-based pre-flight checklist.

Each rule takes the pipeline outputs and returns evidence-backed items. Every
item names a specific file, symbol, revision, or env var — no generic advice.
Rules stay quiet when there's nothing specific to say, so small PRs get short lists.
"""
from __future__ import annotations

import ast
from typing import Callable

from .diff_parser import classify
from .models import (BlastRadius, CallShape, Caller, ChangedSymbol, ChangeType, ChecklistItem, DiffResult,
                     FileKind, RollbackFacts, Severity)

Rule = Callable[[DiffResult, BlastRadius, RollbackFacts], list[ChecklistItem]]

RULES: list[Rule] = []

MAX_LISTED = 5  # evidence lines spelled out in item text before "and N more"


def rule(fn: Rule) -> Rule:
    RULES.append(fn)
    return fn


def _loc(c: Caller) -> str:
    return f"{c.file}:{c.line}"


def _listing(items: list[str]) -> str:
    shown = ", ".join(items[:MAX_LISTED])
    return shown + (f", and {len(items) - MAX_LISTED} more" if len(items) > MAX_LISTED else "")


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _short(sig: str | None) -> str:
    return f"`{sig}`" if sig else "`?`"


def _is_test(path: str) -> bool:
    return classify(path) == FileKind.TEST


# --- signature compatibility -----------------------------------------------------

def parse_signature(sig: str) -> ast.arguments | None:
    """'f(a, b=1) -> int' -> ast.arguments. None if it can't be parsed."""
    try:
        fn = ast.parse(f"def {sig.removeprefix('async ')}: pass").body[0]
    except SyntaxError:
        return None
    return fn.args


def call_breaks(args: ast.arguments, call: CallShape, drop_self: bool = False) -> str | None:
    """Why this call site no longer fits the signature, or None if it fits (or can't be judged)."""
    if call.has_star_args or call.has_star_kwargs:
        return None
    positional = [a.arg for a in args.posonlyargs + args.args]
    posonly = {a.arg for a in args.posonlyargs}
    n_defaults = len(args.defaults)
    required = positional[: len(positional) - n_defaults] if n_defaults else positional
    if drop_self and positional:
        positional, required = positional[1:], [p for p in required if p != positional[0]]

    if call.n_positional > len(positional) and args.vararg is None:
        return f"passes {call.n_positional} positional args, signature takes {len(positional)}"
    filled = set(positional[: call.n_positional])
    kwonly = [a.arg for a in args.kwonlyargs]
    for k in call.keywords:
        if k in filled:
            return f"`{k}` given twice"
        if (k not in positional or k in posonly) and k not in kwonly and args.kwarg is None:
            return f"unknown keyword `{k}`"
    filled |= set(call.keywords)
    missing = [p for p in required if p not in filled]
    missing += [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is None and a.arg not in filled]
    if missing:
        return "missing required " + ", ".join(f"`{m}`" for m in missing)
    return None


def broken_calls(sym: ChangedSymbol, callers: list[Caller]) -> list[tuple[Caller, str]]:
    args = parse_signature(sym.new_sig or "")
    if args is None:
        return []
    drop_self = sym.kind == "method"
    out = []
    for c in callers:
        if c.usage == "call" and c.call and (reason := call_breaks(args, c.call, drop_self)):
            out.append((c, reason))
    return out


def call_verdicts(diff: DiffResult, blast: BlastRadius) -> dict[tuple[str, int], str | None]:
    """(file, line) -> None if the call fits its function's new signature, else why it breaks.
    Only covers direct calls to re-signed functions; shared by the prompt, terminal, and web views."""
    verdicts: dict[tuple[str, int], str | None] = {}
    for sym in diff.symbols:
        if sym.change != ChangeType.SIGNATURE_CHANGED:
            continue
        calls = [c for c in blast.callers_of(sym.fq_name) if c.usage == "call"]
        broken = {(c.file, c.line): reason for c, reason in broken_calls(sym, calls)}
        for c in calls:
            verdicts[(c.file, c.line)] = broken.get((c.file, c.line))
    return verdicts


# --- rules -----------------------------------------------------------------------

@rule
def signature_changed(diff, blast, facts):
    items = []
    for sym in diff.symbols:
        if sym.change != ChangeType.SIGNATURE_CHANGED or sym.kind == "class":
            continue
        direct = blast.callers_of(sym.fq_name, hop=1)
        calls = [c for c in direct if c.usage == "call"]
        broken = broken_calls(sym, calls)
        head = f"{_short(sym.old_sig)} → {_short(sym.new_sig)}"

        if broken:
            evidence = [f"{_loc(c)}: `{c.call.text}` — {reason}" for c, reason in broken]
            stale_tests = sum(1 for c, _ in broken if _is_test(c.file))
            note = f" ({stale_tests} in tests)" if stale_tests else ""
            items.append(ChecklistItem(
                "SIGNATURE_CHANGED", Severity.HIGH,
                f"{head}: {len(broken)} of {_n(len(calls), 'known call site')} no longer match{note} — "
                f"update {_listing([_loc(c) for c, _ in broken])} before merging.",
                evidence,
            ))
        elif calls:
            items.append(ChecklistItem(
                "SIGNATURE_CHANGED", Severity.LOW,
                f"{head} is backward compatible with "
                f"{'its only known call site' if len(calls) == 1 else f'all {len(calls)} known call sites'} "
                f"({_listing([_loc(c) for c in calls])}).",
                [_loc(c) for c in calls],
            ))

        refs = [c for c in direct if c.usage == "reference"]
        if refs:
            items.append(ChecklistItem(
                "SIGNATURE_CHANGED_REFERENCE", Severity.MED,
                f"`{sym.qualname}` is passed around as a value at {_listing([_loc(c) for c in refs])} — "
                f"whoever eventually calls it must use the new signature {_short(sym.new_sig)}.",
                [_loc(c) for c in refs],
            ))

        maybe = [c for c in calls if c.confidence == "low"]
        if maybe:
            items.append(ChecklistItem(
                "SIGNATURE_CHANGED_UNVERIFIED", Severity.MED,
                f"{_n(len(maybe), 'possible call site')} of method `{sym.qualname}` matched by name only "
                f"({_listing([_loc(c) for c in maybe])}) — confirm the receiver's type.",
                [_loc(c) for c in maybe],
            ))
    return items


@rule
def symbol_removed(diff, blast, facts):
    items = []
    for sym in diff.symbols:
        if sym.change != ChangeType.REMOVED:
            continue
        users = blast.callers_of(sym.fq_name, hop=1)
        if users:
            items.append(ChecklistItem(
                "SYMBOL_REMOVED", Severity.HIGH,
                f"`{sym.fq_name}` is removed but still used at {_listing([_loc(c) for c in users])} — "
                f"these will raise ImportError/AttributeError.",
                [_loc(c) for c in users],
            ))
    return items


@rule
def behavior_changed(diff, blast, facts):
    items = []
    for sym in diff.symbols:
        if sym.change != ChangeType.BODY_CHANGED or sym.kind not in ("function", "method"):
            continue
        app_callers = [c for c in blast.callers_of(sym.fq_name, hop=1) if not _is_test(c.file)]
        if app_callers:
            where = _listing([f"{_loc(c)} ({c.enclosing_symbol or 'module level'})" for c in app_callers])
            items.append(ChecklistItem(
                "BEHAVIOR_CHANGED", Severity.LOW,
                f"`{sym.qualname}` behaves differently now; {_n(len(app_callers), 'caller')} in app code {'relies' if len(app_callers) == 1 else 'rely'} on it: "
                f"{where}. Confirm they expect the new behavior.",
                [_loc(c) for c in app_callers],
            ))
    return items


@rule
def migrations(diff, blast, facts):
    items = []
    for m in facts.migrations:
        if not m.in_diff:
            continue
        label = f"Migration `{m.revision}`" + (f" ({m.app})" if m.app else "")
        ops = ", ".join(str(o) for o in m.upgrade_ops) or "no recognized operations"
        risky = [o for o in m.upgrade_ops if o.risky]
        if risky:
            items.append(ChecklistItem(
                "MIGRATION", Severity.HIGH,
                f"{label} runs {', '.join(str(o) for o in risky)} — the app version still serving traffic "
                f"during rollout will break against the new schema. Split into expand/contract or deploy "
                f"the code change first.",
                [m.file],
            ))
        else:
            items.append(ChecklistItem(
                "MIGRATION", Severity.MED,
                f"{label} ({ops}) — additive, so the currently running version should tolerate it; "
                f"run it before the new code rolls out.",
                [m.file],
            ))

        if not m.has_downgrade:
            items.append(ChecklistItem(
                "MIGRATION_NO_DOWNGRADE", Severity.HIGH,
                f"{label} has no working downgrade — it can't be rolled back with the migration tool. "
                f"Implement `downgrade()` / a reverse operation, or plan a restore from backup.",
                [m.file],
            ))
        lossy = [o for o in m.downgrade_ops if o.op in ("drop_column", "drop_table", "RemoveField", "DeleteModel")]
        if lossy:
            items.append(ChecklistItem(
                "MIGRATION_ROLLBACK_DATA_LOSS", Severity.MED,
                f"Rolling back {label[0].lower() + label[1:]} runs {', '.join(str(o) for o in lossy)} — anything written there "
                f"after deploy is lost. Take a backup or export before migrating.",
                [m.file],
            ))
    return items


@rule
def new_env_vars(diff, blast, facts):
    items = []
    for env in facts.new_env_vars:
        where = f"{env.file}:{env.line}"
        declared = facts.declared_env.get(env.name)
        if declared:
            items.append(ChecklistItem(
                "NEW_ENV_VAR", Severity.LOW,
                f"New env var `{env.name}` ({where}) is declared in {', '.join(declared)} — "
                f"confirm the value is right in every environment.",
                [where, *declared],
            ))
            continue
        if env.required and env.at_import:
            severity = Severity.HIGH
            impact = "read with `os.environ[...]` at import time — the service will crash on startup if it's unset"
        elif env.required:
            severity = Severity.HIGH
            impact = "read with `os.environ[...]` — raises KeyError at runtime if unset"
        else:
            severity = Severity.MED
            impact = "optional read — silently falls back to its default if unset"
        targets = [f"{w.kind.lower()}/{w.name}" for w in facts.k8s_deployments]
        set_it = f" Add it to {', '.join(targets)} before deploying." if targets else " Set it in every environment before deploying."
        items.append(ChecklistItem(
            "NEW_ENV_VAR", severity,
            f"New env var `{env.name}` ({where}) is not declared in any manifest, compose file, or .env example; "
            f"{impact}.{set_it}",
            [where],
        ))
    return items


@rule
def feature_flags(diff, blast, facts):
    items = []
    for flag in facts.feature_flags:
        env_guess = [n for n in facts.declared_env if flag.name.upper().replace("-", "_") in n]
        where = f"{flag.file}:{flag.line}"
        config = f" (configured via `{env_guess[0]}` in {', '.join(facts.declared_env[env_guess[0]])})" if env_guess else ""
        items.append(ChecklistItem(
            "FEATURE_FLAG", Severity.LOW,
            f"New code is gated by flag `{flag.name}` ({where}){config} — confirm its value per environment; "
            f"turning it off is the fastest rollback.",
            [where],
        ))
    return items


@rule
def no_tests(diff, blast, facts):
    logic = sorted({s.file for s in diff.symbols
                    if s.kind in ("function", "method") and s.change != ChangeType.REMOVED
                    and not _is_test(s.file)})
    if not logic or any(f.kind == FileKind.TEST for f in diff.files):
        return []
    return [ChecklistItem(
        "NO_TESTS", Severity.MED,
        f"Functions added or changed in {_listing(logic)} but no test files were touched.",
        logic,
    )]


@rule
def deps_changed(diff, blast, facts):
    items = []
    for f in diff.files:
        if f.kind != FileKind.REQUIREMENTS:
            continue
        added = [l.strip() for l in f.added_lines if l.strip() and not l.strip().startswith("#")]
        removed = [l.strip() for l in f.removed_lines if l.strip() and not l.strip().startswith("#")]
        changes = [f"+{a}" for a in added] + [f"-{r}" for r in removed]
        items.append(ChecklistItem(
            "DEPS_CHANGED", Severity.MED,
            f"Dependencies changed in {f.path} ({_listing(changes) or 'content changed'}) — rebuild the image "
            f"and note that rolling back code also means rolling back this image.",
            [f.path],
        ))
    return items


@rule
def deploy_config_changed(diff, blast, facts):
    changed = [f.path for f in diff.files if f.kind in (FileKind.DOCKERFILE, FileKind.K8S, FileKind.WORKFLOW)]
    if not changed:
        return []
    return [ChecklistItem(
        "DEPLOY_CONFIG_CHANGED", Severity.MED,
        f"Deploy configuration changed ({_listing(changed)}) — `git revert` alone won't undo what's already "
        f"applied to the cluster/pipeline; review these by hand.",
        changed,
    )]


@rule
def parse_errors(diff, blast, facts):
    return [
        ChecklistItem("PARSE_ERROR", Severity.HIGH,
                      f"{f.path} does not parse at head ({f.parse_error}) — blast radius for it is unknown.", [f.path])
        for f in diff.files if f.parse_error
    ]


def build_checklist(diff: DiffResult, blast: BlastRadius, facts: RollbackFacts) -> list[ChecklistItem]:
    items: list[ChecklistItem] = []
    for r in RULES:
        items.extend(r(diff, blast, facts))
    order = {Severity.HIGH: 0, Severity.MED: 1, Severity.LOW: 2}
    return sorted(items, key=lambda i: order[i.severity])  # stable: rule order within a severity
