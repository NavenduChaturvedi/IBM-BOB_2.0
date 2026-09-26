"""Component 1: turn a git range into changed files and changed Python symbols.

Symbols are compared by AST, not by text, so reformatting or moving a function
doesn't count as a change. Each symbol gets a signature string (from the args
AST) and a body fingerprint (ast.dump without line numbers).
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from . import gitio
from .models import ChangedFile, ChangedSymbol, ChangeType, DiffResult, FileKind

SYMBOL_KINDS = (FileKind.PYTHON, FileKind.TEST)
MAX_CHANGED_LINES = 5000  # per file; beyond this only line counts are kept
MAX_SOURCE_BYTES = 1_000_000  # Python files larger than this aren't AST-parsed

_K8S_KIND_RE = re.compile(
    r"^kind:\s*(Deployment|StatefulSet|DaemonSet|Service|Ingress|ConfigMap|Secret|CronJob|Job)\b",
    re.MULTILINE,
)
_DJANGO_MIGRATION_RE = re.compile(r"^\d{4}_\w+\.py$")
_REQUIREMENTS_NAMES = {"pyproject.toml", "setup.py", "setup.cfg", "pipfile", "pipfile.lock", "poetry.lock"}
_CONFIG_SUFFIXES = {".yml", ".yaml", ".toml", ".ini", ".cfg", ".json", ".env"}


# --- file classification ------------------------------------------------------

def classify(path: str, content: str | None = None) -> FileKind:
    p = PurePosixPath(path)
    name = p.name.lower()
    parts = [part.lower() for part in p.parts[:-1]]
    suffix = p.suffix.lower()

    if suffix == ".py":
        if "versions" in parts and ("alembic" in parts or (content and "down_revision" in content)):
            return FileKind.MIGRATION
        if "migrations" in parts and _DJANGO_MIGRATION_RE.match(name):
            return FileKind.MIGRATION
        if name == "setup.py":
            return FileKind.REQUIREMENTS
        if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py" or "tests" in parts:
            return FileKind.TEST
        return FileKind.PYTHON

    if name.startswith("dockerfile") or name.endswith(".dockerfile"):
        return FileKind.DOCKERFILE
    if (name.startswith("requirements") and suffix in (".txt", ".in")) or name in _REQUIREMENTS_NAMES:
        return FileKind.REQUIREMENTS
    if suffix in (".yml", ".yaml"):
        if parts[:2] == [".github", "workflows"]:
            return FileKind.WORKFLOW
        if content is not None and _K8S_KIND_RE.search(content):
            return FileKind.K8S
        if any(d in ("k8s", "kubernetes", "manifests", "helm", "deploy") for d in parts):
            return FileKind.K8S
    if suffix in _CONFIG_SUFFIXES or name.startswith(".env") or name.startswith("docker-compose"):
        return FileKind.CONFIG
    return FileKind.OTHER


def path_to_module(path: str) -> str:
    """'app/payments/pricing.py' -> 'app.payments.pricing'; 'app/__init__.py' -> 'app'."""
    p = PurePosixPath(path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# --- symbol extraction --------------------------------------------------------

@dataclass
class _Symbol:
    qualname: str
    kind: str  # function | method | class | variable
    signature: str | None
    fingerprint: str
    lineno: int


def _fingerprint(nodes: list[ast.AST]) -> str:
    dumped = "\n".join(ast.dump(n, annotate_fields=False, include_attributes=False) for n in nodes)
    return hashlib.sha1(dumped.encode("utf-8")).hexdigest()


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    sig = f"{prefix}{node.name}({ast.unparse(node.args)})"
    if node.returns is not None:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig


def _class_signature(node: ast.ClassDef) -> str:
    bases = [ast.unparse(b) for b in node.bases] + [ast.unparse(k) for k in node.keywords]
    return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"


def _assigned_names(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return []
    names = []
    for t in targets:
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, ast.Tuple):
            names.extend(e.id for e in t.elts if isinstance(e, ast.Name))
    return names


def _collect(body: list[ast.stmt], prefix: str, in_class: bool, out: dict[str, _Symbol]) -> None:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = f"{prefix}{node.name}"
            out[qualname] = _Symbol(
                qualname=qualname,
                kind="method" if in_class else "function",
                signature=_function_signature(node),
                fingerprint=_fingerprint([*node.decorator_list, *node.body]),
                lineno=node.lineno,
            )
        elif isinstance(node, ast.ClassDef):
            qualname = f"{prefix}{node.name}"
            # class body fingerprint excludes methods/nested classes; those are tracked separately
            own = [n for n in node.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            out[qualname] = _Symbol(
                qualname=qualname,
                kind="class",
                signature=_class_signature(node),
                fingerprint=_fingerprint([*node.decorator_list, *own]),
                lineno=node.lineno,
            )
            _collect(node.body, f"{qualname}.", True, out)
        elif not in_class:
            for name in _assigned_names(node):
                out[f"{prefix}{name}"] = _Symbol(
                    qualname=f"{prefix}{name}",
                    kind="variable",
                    signature=None,
                    fingerprint=_fingerprint([node.value] if node.value is not None else []),
                    lineno=node.lineno,
                )


def extract_symbols(source: str) -> dict[str, _Symbol]:
    out: dict[str, _Symbol] = {}
    _collect(ast.parse(source).body, "", False, out)
    return out


def diff_symbols(
    old_path: str | None,
    new_path: str | None,
    old_src: str | None,
    new_src: str | None,
) -> list[ChangedSymbol]:
    """Compare two versions of a file. Raises SyntaxError if either side doesn't parse."""
    old = extract_symbols(old_src) if old_src is not None else {}
    new = extract_symbols(new_src) if new_src is not None else {}
    old_module = path_to_module(old_path) if old_path else None
    new_module = path_to_module(new_path) if new_path else None

    # a rename/move changes the import path: everything in the old module is gone for its importers
    if old_module and new_module and old_module != new_module:
        removed = [_changed(old_path, old_module, s, ChangeType.REMOVED, old=s) for s in old.values()]
        added = [_changed(new_path, new_module, s, ChangeType.ADDED, new=s) for s in new.values()]
        return removed + added

    path, module = new_path or old_path, new_module or old_module
    changes: list[ChangedSymbol] = []
    for qualname in sorted(old.keys() | new.keys(), key=lambda q: (new.get(q) or old[q]).lineno):
        o, n = old.get(qualname), new.get(qualname)
        if o is None:
            changes.append(_changed(path, module, n, ChangeType.ADDED, new=n))
        elif n is None:
            changes.append(_changed(path, module, o, ChangeType.REMOVED, old=o))
        elif o.signature != n.signature:
            changes.append(_changed(path, module, n, ChangeType.SIGNATURE_CHANGED, old=o, new=n))
        elif o.fingerprint != n.fingerprint:
            changes.append(_changed(path, module, n, ChangeType.BODY_CHANGED, old=o, new=n))
    return changes


def _changed(path, module, sym: _Symbol, change: ChangeType, old: _Symbol | None = None,
             new: _Symbol | None = None) -> ChangedSymbol:
    return ChangedSymbol(
        file=path,
        module=module,
        qualname=sym.qualname,
        kind=sym.kind,
        change=change,
        lineno=sym.lineno,
        old_sig=old.signature if old else None,
        new_sig=new.signature if new else None,
    )


# --- pipeline entry -----------------------------------------------------------

def parse_diff(repo, base: str, head: str) -> DiffResult:
    base_sha = gitio.merge_base(repo, base, head)
    head_sha = gitio.rev_parse(repo, head)

    files: list[ChangedFile] = []
    symbols: list[ChangedSymbol] = []
    stats = gitio.numstat(repo, base_sha, head_sha)
    for status, path, old_path in gitio.name_status(repo, base_sha, head_sha):
        src_path = old_path or path
        n_added, n_removed = stats.get(path, (0, 0))
        binary = n_added is None
        # binary files and huge text diffs (lockfiles, data dumps) are counted, never read
        summarized = not binary and (n_added + n_removed) > MAX_CHANGED_LINES
        if binary:
            changed = ChangedFile(path=path, status=status, kind=classify(path), old_path=old_path, binary=True)
            files.append(changed)
            continue

        old_src = None if status == "A" else gitio.show_file(repo, base_sha, src_path)
        new_src = None if status == "D" else gitio.show_file(repo, head_sha, path)
        added, removed = ([], []) if summarized else gitio.file_diff(repo, base_sha, head_sha, path)

        changed = ChangedFile(
            path=path,
            status=status,
            kind=classify(path, new_src if new_src is not None else old_src),
            old_path=old_path,
            added_lines=added,
            removed_lines=removed,
            summarized=summarized,
            pure_rename=status == "R" and old_src == new_src,
        )
        too_big = max(len(old_src or ""), len(new_src or "")) > MAX_SOURCE_BYTES
        if changed.kind in SYMBOL_KINDS and too_big:
            changed.summarized = True  # symbols unknown; the checklist says so
        elif changed.kind in SYMBOL_KINDS:
            try:
                symbols.extend(diff_symbols(
                    src_path if status != "A" else None,
                    path if status != "D" else None,
                    old_src,
                    new_src,
                ))
            except SyntaxError as e:
                changed.parse_error = f"{e.msg} (line {e.lineno})"
        files.append(changed)

    return DiffResult(
        base=base,
        head=head,
        base_sha=base_sha,
        head_sha=head_sha,
        commits=gitio.commits_between(repo, base_sha, head_sha),
        files=files,
        symbols=symbols,
    )
