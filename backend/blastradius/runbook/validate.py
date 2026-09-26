"""Reject any runbook that references identifiers not present in RollbackFacts.

Every command in a fenced code block is checked:
- tool must be backed by an artifact (kubectl needs a k8s workload, alembic an
  alembic migration, docker a Dockerfile/compose file, manage.py a Django migration)
- kubectl workload refs and namespaces must match a scanned manifest; env vars set
  with `kubectl set env` must be ones the repo declares or the PR introduces
- alembic/django targets must be known revisions (or a relative step that matches
  the number of migrations in this PR)
- any hex id (commit SHA / revision) must match one we know
- `<placeholder>` tokens are rejected: the runbook must be runnable as written
Also checks coverage: the runbook can't silently drop the revert, the schema
downgrade, or the workload rollback.
"""
from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

from ..models import RollbackFacts

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")
_WORKLOAD_ALIASES = {"deployment": "deployment", "deploy": "deployment", "deployments": "deployment",
                     "statefulset": "statefulset", "sts": "statefulset", "statefulsets": "statefulset",
                     "daemonset": "daemonset", "ds": "daemonset", "daemonsets": "daemonset"}
_ROUTE_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[\w/{}:.-]*)")
_SAFE_TOOLS = {"echo", "cd", "grep", "head", "tail", "wc"}  # read-only; safe with any input


def extract_commands(markdown: str) -> list[str]:
    commands = []
    for block in _FENCE_RE.findall(markdown):
        pending = ""
        for raw in block.splitlines():
            line = raw.strip()
            if line.startswith("$ "):
                line = line[2:]
            if not line or line.startswith("#"):
                continue
            if line.endswith("\\"):
                pending += line[:-1].rstrip() + " "
                continue
            commands.append((pending + line).strip())
            pending = ""
        if pending:
            commands.append(pending.strip())
    return commands


def _split_chain(command: str) -> list[list[str]]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    parts, current = [], []
    for t in tokens:
        if t in ("&&", "||", ";", "|"):
            if current:
                parts.append(current)
            current = []
        else:
            current.append(t.rstrip(";"))
    if current:
        parts.append(current)
    return parts


class _Checker:
    def __init__(self, facts: RollbackFacts):
        self.facts = facts
        self.known_ids = {facts.base_sha, facts.head_sha, *facts.commits} | {m.revision for m in facts.migrations}
        self.env_names = set(facts.declared_env) | {e.name for e in facts.new_env_vars}
        self.alembic = [m for m in facts.migrations if m.tool == "alembic"]
        self.django = [m for m in facts.migrations if m.tool == "django"]
        self.seen: set[str] = set()  # coverage: revert | schema | workload:<kind>/<name>

    # --- generic ---------------------------------------------------------------
    def _generic(self, tokens: list[str]) -> list[str]:
        errors = []
        for t in tokens:
            if re.search(r"<[^>]+>", t):
                errors.append(f"placeholder `{t}` — commands must be runnable as written")
            value = t.split("=", 1)[-1]
            if _HEX_RE.match(value) and not any(k.startswith(value) for k in self.known_ids):
                errors.append(f"unknown commit/revision id `{value}`")
        return errors

    def check(self, tokens: list[str]) -> list[str]:
        if not tokens:
            return []
        errors = self._generic(tokens)
        tool = PurePosixPath(tokens[0]).name
        handler = {
            "git": self._git, "kubectl": self._kubectl, "alembic": self._alembic,
            "docker": self._docker, "docker-compose": self._compose, "pytest": self._pytest,
            "python": self._python, "python3": self._python,
        }.get(tool)
        if handler:
            errors += handler(tokens)
        elif tool not in _SAFE_TOOLS:
            errors.append(f"`{tool}` has no backing artifact in this repo")
        return errors

    # --- tools ---------------------------------------------------------------------
    def _git(self, tokens):
        if len(tokens) > 1 and tokens[1] == "revert":
            self.seen.add("revert")
        return []

    def _kubectl(self, tokens):
        if not self.facts.k8s_deployments:
            return ["`kubectl` used but the repo has no Kubernetes workloads"]
        errors = []
        if "--" in tokens:  # kubectl exec ... -- <inner command>
            i = tokens.index("--")
            errors += self.check(tokens[i + 1:])
            tokens = tokens[:i]

        namespace = None
        for i, t in enumerate(tokens):
            if t in ("-n", "--namespace") and i + 1 < len(tokens):
                namespace = tokens[i + 1]
            elif t.startswith("--namespace="):
                namespace = t.split("=", 1)[1]

        refs = []
        for i, t in enumerate(tokens[1:], 1):
            if "/" in t and t.split("/", 1)[0].lower() in _WORKLOAD_ALIASES:
                kind, name = t.split("/", 1)
                refs.append((_WORKLOAD_ALIASES[kind.lower()], name))
            elif t.lower() in _WORKLOAD_ALIASES and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                refs.append((_WORKLOAD_ALIASES[t.lower()], tokens[i + 1]))

        for kind, name in refs:
            match = [w for w in self.facts.k8s_deployments if w.kind.lower() == kind and w.name == name]
            if not match:
                errors.append(f"{kind} `{name}` not found in any scanned manifest")
                continue
            w = match[0]
            if namespace is not None and namespace != w.namespace:
                errors.append(f"{kind}/{name} is in namespace `{w.namespace}`, not `{namespace}`")
            if "undo" in tokens:
                self.seen.add(f"workload:{kind}/{name}")

        if "env" in tokens[:3]:
            for t in tokens:
                if "=" in t and not t.startswith("-"):
                    var = t.split("=", 1)[0]
                    if var not in self.env_names:
                        errors.append(f"env var `{var}` isn't declared in the repo or introduced by this PR")
        return errors

    def _alembic(self, tokens):
        if not self.alembic:
            return ["`alembic` used but the repo has no alembic migrations"]
        if len(tokens) < 3 or tokens[1] not in ("downgrade", "upgrade"):
            return []
        target = tokens[2]
        in_diff = [m for m in self.alembic if m.in_diff]
        if tokens[1] == "downgrade":
            self.seen.add("schema")
        if re.fullmatch(r"-\d+", target):
            if tokens[1] == "downgrade" and int(target[1:]) != len(in_diff):
                return [f"`alembic downgrade {target}` undoes {target[1:]} migration(s); this PR adds {len(in_diff)}"]
            return []
        if target in ("base", "head", "heads") or any(m.revision.startswith(target) for m in self.alembic):
            return []
        return [f"alembic revision `{target}` not found in the repo"]

    def _python(self, tokens):
        if len(tokens) > 2 and tokens[1] == "-m":
            return self.check(tokens[2:])
        if len(tokens) > 1 and PurePosixPath(tokens[1]).name == "manage.py":
            if not self.django:
                return ["`manage.py` used but the repo has no Django migrations"]
            if len(tokens) > 2 and tokens[2] == "migrate" and len(tokens) > 4:
                app, target = tokens[3], tokens[4]
                self.seen.add("schema")
                known = {m.revision for m in self.django if m.app == app}
                if not known:
                    return [f"Django app `{app}` has no migrations in the repo"]
                if target != "zero" and target not in known:
                    return [f"Django migration `{app}.{target}` not found"]
            return []
        return [f"`{' '.join(tokens[:2])}` isn't grounded in a repo artifact"]

    def _docker(self, tokens):
        if len(tokens) > 1 and tokens[1] == "compose":
            return self._compose(tokens[1:])
        if len(tokens) > 1 and tokens[1] == "build" and self.facts.dockerfiles:
            return []
        return [f"`docker {tokens[1] if len(tokens) > 1 else ''}` isn't grounded in a repo artifact"]

    def _compose(self, tokens):
        if not self.facts.compose_services:
            return ["docker compose used but the repo has no compose file"]
        args = [t for t in tokens[2:] if not t.startswith("-")]
        return [f"compose service `{s}` not found" for s in args if s not in self.facts.compose_services]

    def _pytest(self, tokens):
        errors = []
        for t in tokens[1:]:
            if t.startswith("-"):
                continue
            path = t.split("::", 1)[0].rstrip("/")
            if not any(f == path or f.startswith(path + "/") for f in self.facts.test_files):
                errors.append(f"test path `{t}` doesn't exist in the repo")
        return errors


def validate(markdown: str, facts: RollbackFacts) -> list[str]:
    """Return human-readable rejection reasons; empty list means valid."""
    checker = _Checker(facts)
    rejections = []
    commands = extract_commands(markdown)
    if not commands:
        return ["no commands found in fenced code blocks"]
    for command in commands:
        for tokens in _split_chain(command):
            rejections += [f"`{command}`: {e}" for e in checker.check(tokens)]
    # facts never include HTTP routes, so any method + path in the runbook was invented
    for method, path in _ROUTE_RE.findall(markdown):
        rejections.append(f"`{method} {path}`: HTTP route isn't in the repo facts (invented endpoint)")

    if "revert" not in checker.seen:
        rejections.append("missing step: `git revert` of the PR's commits")
    if any(m.in_diff and m.has_downgrade for m in facts.migrations) and "schema" not in checker.seen:
        rejections.append("missing step: schema downgrade for this PR's migration")
    for w in facts.k8s_deployments:
        if f"workload:{w.kind.lower()}/{w.name}" not in checker.seen:
            rejections.append(f"missing step: `kubectl rollout undo {w.kind.lower()}/{w.name}`")
    return list(dict.fromkeys(rejections))
