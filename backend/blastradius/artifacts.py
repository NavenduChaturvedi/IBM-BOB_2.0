"""Component 4a: scan the repo at head for deploy artifacts and build RollbackFacts.

Reads from git at the head revision (like the import graph), never the working tree.
Finds: alembic + Django migrations (with what each one does), k8s workloads and
the env vars they set, Dockerfiles, compose services, CI workflows, and the env
vars / feature flags referenced by lines this diff adds.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path, PurePosixPath

import yaml

from . import gitio
from .callers import dotted_name
from .diff_parser import classify
from .models import (DiffResult, EnvVar, FeatureFlag, FileKind, K8sDeployment, Migration, MigrationOp,
                     RollbackFacts)

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
ENV_EXAMPLE_NAMES = {".env.example", ".env.sample", ".env.template", "env.example"}

_ENV_RE = re.compile(
    r"""(?:os\.)?environ\s*\[\s*["'](?P<req>\w+)["']\s*\]"""
    r"""|(?:os\.)?environ\.get\(\s*["'](?P<get>\w+)["']"""
    r"""|getenv\(\s*["'](?P<getenv>\w+)["']"""
)
_FLAG_RE = re.compile(
    r"""\b(?:is_enabled|is_active|feature_enabled|flag_enabled|is_feature_enabled|get_flag|variation)"""
    r"""\(\s*["'](?P<name>[\w.-]+)["']"""
)
_DOCKER_ENV_RE = re.compile(r"^\s*ENV\s+(\w+)", re.MULTILINE | re.IGNORECASE)


# --- helpers -----------------------------------------------------------------

def _const(node: ast.AST | None):
    return node.value if isinstance(node, ast.Constant) else None


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _module_assign(tree: ast.Module, name: str) -> ast.AST | None:
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return node.value
    return None


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    return next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name), None)


def _is_empty_body(fn: ast.FunctionDef) -> bool:
    for stmt in fn.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):  # docstring / ...
            continue
        return False
    return True


# --- alembic -----------------------------------------------------------------

def _alembic_op(call: ast.Call) -> MigrationOp | None:
    dotted = dotted_name(call.func) or ""
    if not dotted.startswith("op."):
        return None
    name = dotted[3:]
    args = [_const(a) for a in call.args]
    first = args[0] if args and isinstance(args[0], str) else "?"

    if name == "add_column":
        col = call.args[1] if len(call.args) > 1 and isinstance(call.args[1], ast.Call) else None
        col_name = _const(col.args[0]) if col and col.args else "?"
        nullable = _const(_kwarg(col, "nullable")) if col else None
        has_default = col is not None and (_kwarg(col, "server_default") is not None)
        if nullable is False and not has_default:
            return MigrationOp(name, f"{first}.{col_name}", True, "NOT NULL, no server_default")
        return MigrationOp(name, f"{first}.{col_name}", False, "nullable" if nullable is not False else "NOT NULL, has default")
    if name in ("drop_column", "alter_column"):
        col = args[1] if len(args) > 1 and isinstance(args[1], str) else "?"
        return MigrationOp(name, f"{first}.{col}", True)
    if name in ("create_table", "create_index"):
        return MigrationOp(name, first, False)
    if name in ("drop_table", "drop_index", "rename_table", "drop_constraint"):
        return MigrationOp(name, first, True)
    if name == "execute":
        return MigrationOp(name, "raw SQL", True)
    return MigrationOp(name, first, False)


def _ops_in(fn: ast.FunctionDef | None, parse) -> list[MigrationOp]:
    if fn is None:
        return []
    return [op for node in ast.walk(fn) if isinstance(node, ast.Call) and (op := parse(node))]


def _parse_alembic(path: str, source: str, in_diff: bool) -> Migration | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    revision = _const(_module_assign(tree, "revision"))
    if not isinstance(revision, str):
        return None
    down = _module_assign(tree, "down_revision")
    down_revision = _const(down)
    if isinstance(down, (ast.Tuple, ast.List)):  # merge migration
        down_revision = ",".join(str(_const(e)) for e in down.elts)
    downgrade = _function(tree, "downgrade")
    doc = ast.get_docstring(tree) or ""
    return Migration(
        tool="alembic",
        file=path,
        revision=revision,
        down_revision=down_revision,
        has_downgrade=downgrade is not None and not _is_empty_body(downgrade),
        in_diff=in_diff,
        description=doc.splitlines()[0] if doc else "",
        upgrade_ops=_ops_in(_function(tree, "upgrade"), _alembic_op),
        downgrade_ops=_ops_in(downgrade, _alembic_op),
    )


# --- django ------------------------------------------------------------------

_DJANGO_RISKY_PREFIXES = ("Remove", "Delete", "Rename", "Alter", "RunSQL", "RunPython")


def _django_op(call: ast.Call) -> MigrationOp | None:
    dotted = dotted_name(call.func) or ""
    if not dotted.startswith("migrations."):
        return None
    name = dotted.split(".", 1)[1]
    model = _const(_kwarg(call, "model_name")) or _const(_kwarg(call, "name")) or "?"
    field_name = _const(_kwarg(call, "name")) if _kwarg(call, "model_name") is not None else None
    target = f"{model}.{field_name}" if field_name else str(model)
    if name == "AddField":
        f = _kwarg(call, "field")
        null = isinstance(f, ast.Call) and _const(_kwarg(f, "null")) is True
        has_default = isinstance(f, ast.Call) and _kwarg(f, "default") is not None
        risky = not null and not has_default
        return MigrationOp(name, target, risky, "NOT NULL, no default" if risky else "")
    return MigrationOp(name, target, name.startswith(_DJANGO_RISKY_PREFIXES))


def _parse_django(path: str, source: str, in_diff: bool) -> Migration | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    p = PurePosixPath(path)
    app, name = p.parent.parent.name, p.stem
    ops = [op for node in ast.walk(tree) if isinstance(node, ast.Call) and (op := _django_op(node))]

    down = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "dependencies" for t in node.targets):
            deps = [(_const(e.elts[0]), _const(e.elts[1])) for e in getattr(node.value, "elts", [])
                    if isinstance(e, ast.Tuple) and len(e.elts) == 2]
            same_app = [d[1] for d in deps if d[0] == app]
            down = same_app[-1] if same_app else None

    irreversible = any(
        isinstance(n, ast.Call) and (dotted_name(n.func) or "").endswith(("RunPython", "RunSQL"))
        and len(n.args) < 2 and not any(k.arg in ("reverse_code", "reverse_sql") for k in n.keywords)
        for n in ast.walk(tree)
    )
    return Migration(
        tool="django", file=path, revision=name, down_revision=down, has_downgrade=not irreversible,
        in_diff=in_diff, app=app, upgrade_ops=ops,
    )


# --- k8s / compose / env declarations ------------------------------------------

def _yaml_docs(source: str) -> list[dict]:
    try:
        return [d for d in yaml.safe_load_all(source) if isinstance(d, dict)]
    except yaml.YAMLError:
        return []  # helm templates etc.


def _k8s_workloads(path: str, source: str) -> list[K8sDeployment]:
    found = []
    for doc in _yaml_docs(source):
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        meta = doc.get("metadata") or {}
        containers = (((doc.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
        found.append(K8sDeployment(
            name=str(meta.get("name", "?")),
            namespace=str(meta.get("namespace", "default")),
            file=path,
            kind=doc["kind"],
            containers=[str(c.get("name")) for c in containers if isinstance(c, dict)],
            env=[str(e["name"]) for c in containers if isinstance(c, dict)
                 for e in (c.get("env") or []) if isinstance(e, dict) and "name" in e],
        ))
    return found


def _compose_services(source: str) -> tuple[list[str], list[str]]:
    """Return (service names, env var names)."""
    services, env = [], []
    for doc in _yaml_docs(source):
        for name, svc in (doc.get("services") or {}).items():
            services.append(str(name))
            environment = (svc or {}).get("environment") or []
            if isinstance(environment, dict):
                env.extend(environment)
            else:
                env.extend(str(e).split("=", 1)[0] for e in environment)
    return services, env


def _env_file_keys(source: str) -> list[str]:
    keys = []
    for line in source.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].removeprefix("export ").strip())
    return keys


# --- diff-scoped scans (env vars + flags in added lines) ------------------------

def _function_spans(source: str) -> list[tuple[int, int]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [(n.lineno, n.end_lineno or n.lineno) for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]


def _added_line_numbers(source: str, added: list[str]) -> list[tuple[int, str]]:
    added_set = {a.strip() for a in added if a.strip()}
    return [(i, line) for i, line in enumerate(source.splitlines(), 1) if line.strip() in added_set]


def _scan_added_code(diff: DiffResult, head_sources: dict[str, str]) -> tuple[list[EnvVar], list[FeatureFlag]]:
    removed_env = {m.group("req") or m.group("get") or m.group("getenv")
                   for f in diff.files for line in f.removed_lines for m in _ENV_RE.finditer(line)}
    env_vars: dict[str, EnvVar] = {}
    flags: dict[str, FeatureFlag] = {}
    for f in diff.files:
        if f.kind != FileKind.PYTHON or f.path not in head_sources:
            continue
        source = head_sources[f.path]
        spans = _function_spans(source)
        for lineno, line in _added_line_numbers(source, f.added_lines):
            for m in _ENV_RE.finditer(line):
                name = m.group("req") or m.group("get") or m.group("getenv")
                if name in removed_env or name in env_vars:
                    continue
                env_vars[name] = EnvVar(
                    name=name, file=f.path, line=lineno, required=m.group("req") is not None,
                    at_import=not any(start <= lineno <= end for start, end in spans),
                )
            for m in _FLAG_RE.finditer(line):
                flags.setdefault(m.group("name"), FeatureFlag(m.group("name"), f.path, lineno))
    return list(env_vars.values()), list(flags.values())


# --- entry -------------------------------------------------------------------

def _is_artifact(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name.lower()
    return (
        p.suffix.lower() in (".yml", ".yaml")
        or name.startswith("dockerfile") or name.endswith(".dockerfile")
        or name in ENV_EXAMPLE_NAMES
        or classify(path) == FileKind.MIGRATION
    )


def scan_artifacts(repo: Path, diff: DiffResult) -> RollbackFacts:
    all_files = gitio.list_files(repo, diff.head_sha)
    changed_py = [f.path for f in diff.files if f.kind == FileKind.PYTHON and f.status != "D"]
    sources = gitio.read_files(repo, diff.head_sha, [f for f in all_files if _is_artifact(f)] + changed_py)
    in_diff = {f.path for f in diff.files if f.status != "D"}

    facts = RollbackFacts(base_sha=diff.base_sha, head_sha=diff.head_sha, commits=diff.commits)
    declared: dict[str, list[str]] = {}

    def declare(names, path):
        for n in names:
            declared.setdefault(n, [])
            if path not in declared[n]:
                declared[n].append(path)

    for path in sorted(sources):
        source = sources[path]
        name = PurePosixPath(path).name.lower()
        kind = classify(path, source)
        if kind == FileKind.MIGRATION:
            parser = _parse_django if "migrations" in PurePosixPath(path).parts else _parse_alembic
            if (m := parser(path, source, path in in_diff)) is not None:
                facts.migrations.append(m)
        elif kind == FileKind.DOCKERFILE:
            facts.dockerfiles.append(path)
            declare(_DOCKER_ENV_RE.findall(source), path)
        elif kind == FileKind.WORKFLOW:
            facts.workflows.append(path)
        elif name in ENV_EXAMPLE_NAMES:
            declare(_env_file_keys(source), path)
        elif name.startswith(("docker-compose", "compose")) and name.endswith((".yml", ".yaml")):
            services, env = _compose_services(source)
            facts.compose_services.extend(services)
            declare(env, path)
        elif path.endswith((".yml", ".yaml")):
            for w in _k8s_workloads(path, source):
                facts.k8s_deployments.append(w)
                declare(w.env, path)

    facts.new_env_vars, facts.feature_flags = _scan_added_code(diff, sources)
    facts.declared_env = declared
    facts.test_files = [f for f in all_files if f.endswith(".py") and classify(f) == FileKind.TEST]
    return facts
