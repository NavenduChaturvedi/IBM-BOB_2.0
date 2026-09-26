"""Deterministic runbook built only from RollbackFacts — the fallback that always works.

Step order:
  1. flag off            fastest, no deploy needed
  2. schema downgrade    must run from the NEW code: the old image doesn't contain the
                         migration file, so alembic/django can't downgrade from it
  3. app rollback        kubectl rollout undo (k8s) — or after the revert for compose/pipeline
  4. git revert          so the next deploy doesn't bring the change back
  5. verify              rollout status, tests that cover the blast radius, callers to re-check
"""
from __future__ import annotations

from ..diff_parser import classify
from ..models import (BlastRadius, ChangeType, ChecklistItem, DiffResult, FileKind, K8sDeployment,
                      Migration, RollbackFacts, Severity)


def _workload_ref(w: K8sDeployment) -> str:
    return f"{w.kind.lower()}/{w.name}"


def _ns(w: K8sDeployment) -> str:
    return f"-n {w.namespace}"


def _bash(*lines: str) -> str:
    return "```bash\n" + "\n".join(lines) + "\n```"


def _migrations_to_undo(facts: RollbackFacts) -> list[Migration]:
    return [m for m in facts.migrations if m.in_diff]


def _alembic_target(migrations: list[Migration]) -> str:
    """Revision to downgrade to: the down_revision of the oldest PR migration in the chain."""
    revs = {m.revision for m in migrations}
    oldest = [m for m in migrations if m.down_revision not in revs]
    down = oldest[0].down_revision if oldest else None
    return down if down else "base"


def _flag_env(facts: RollbackFacts, flag: str) -> tuple[str, K8sDeployment] | None:
    key = flag.upper().replace("-", "_").replace(".", "_")
    for w in facts.k8s_deployments:
        for env in w.env:
            if key in env:
                return env, w
    return None


def _triggers(diff: DiffResult, checklist: list[ChecklistItem], facts: RollbackFacts) -> list[str]:
    out = []
    workloads = ", ".join(f"`{_workload_ref(w)}`" for w in facts.k8s_deployments) or "the service"
    for item in checklist:
        if item.severity != Severity.HIGH:
            continue
        name = item.text.split("`")[1] if "`" in item.text else ""
        if item.rule_id == "NEW_ENV_VAR":
            out.append(f"{workloads} pods crash-loop or log `KeyError: '{name}'`")
        elif item.rule_id == "SIGNATURE_CHANGED":
            out.append(f"`TypeError` raised from calls to `{name.split('(')[0]}`")
        elif item.rule_id == "SYMBOL_REMOVED":
            out.append(f"`ImportError`/`AttributeError` mentioning `{name.rsplit('.', 1)[-1]}`")
        elif item.rule_id == "MIGRATION":
            out.append("database errors from pods still running the previous version during rollout")
    changed = sorted({s.qualname for s in diff.symbols
                      if s.change != ChangeType.ADDED and s.kind in ("function", "method") and classify(s.file) != FileKind.TEST})
    if changed:
        out.append("error rate or latency rises on paths through " + ", ".join(f"`{c}`" for c in changed[:4]))
    else:
        out.append("error rate or latency rises after the deploy")
    return list(dict.fromkeys(out))


def render_template(
    diff: DiffResult,
    facts: RollbackFacts,
    checklist: list[ChecklistItem],
    blast: BlastRadius | None = None,
) -> str:
    steps: list[str] = []
    k8s = facts.k8s_deployments
    migrations = _migrations_to_undo(facts)
    commits = facts.commits  # newest first — the order git revert needs

    # 1. feature flags
    startup_killers = [e.name for e in facts.new_env_vars
                       if e.required and e.at_import and e.name not in facts.declared_env]
    crash_note = (
        f" This won't help if pods are crash-looping on startup because "
        f"{', '.join(f'`{n}`' for n in startup_killers)} is unset — go straight to the rollback."
        if startup_killers else ""
    )
    for flag in facts.feature_flags:
        match = _flag_env(facts, flag.name)
        if match:
            env, w = match
            steps.append(
                f"**Turn off flag `{flag.name}`** — fastest mitigation; the new code path "
                f"({flag.file}:{flag.line}) stops running without a code rollback.{crash_note}\n\n"
                + _bash(f"kubectl set env {_workload_ref(w)} {_ns(w)} {env}=false",
                        f"kubectl rollout status {_workload_ref(w)} {_ns(w)}")
            )
        else:
            steps.append(
                f"**Turn off flag `{flag.name}`** in your flag service — the new code path "
                f"({flag.file}:{flag.line}) stops running without a code rollback.{crash_note}"
            )

    # 2. schema
    alembic = [m for m in migrations if m.tool == "alembic"]
    django = [m for m in migrations if m.tool == "django"]
    if alembic:
        target = _alembic_target(alembic)
        undo = ", ".join(f"`{m.revision}` ({'; '.join(str(o) for o in m.upgrade_ops) or m.description})" for m in alembic)
        lossy = [str(o) for m in alembic for o in m.downgrade_ops if o.risky]
        if all(m.has_downgrade for m in alembic):
            cmd = f"alembic downgrade {target}"
            lines = []
            if k8s:
                w = k8s[0]
                container = f" -c {w.containers[0]}" if len(w.containers) > 1 else ""
                lines.append(f"kubectl exec {_ns(w)} {_workload_ref(w)}{container} -- {cmd}")
            steps.append(
                f"**Downgrade the schema** — undoes {undo}. Run this *before* rolling back the app: "
                f"the previous image doesn't contain the migration file, so alembic can't downgrade from it."
                + (f" ⚠ Data loss: {', '.join(lossy)}." if lossy else "")
                + ("\n\n" + _bash(*lines) + "\n\nIf the new pods aren't running, from a checkout of the PR head:\n\n"
                   if lines else "\n\n")
                + _bash("# DATABASE_URL must point at the target database",
                        f"git checkout {facts.head_sha[:12]}", cmd)
            )
        else:
            steps.append(
                f"**Schema rollback needs a manual restore** — {undo} has no working `downgrade()`. "
                f"Restore the database from the backup taken before migrating."
            )
    for m in django:
        target = m.down_revision or "zero"
        steps.append(
            f"**Downgrade the schema** — `{m.app}.{m.revision}` "
            f"({'; '.join(str(o) for o in m.upgrade_ops)}). Run from the new code, before rolling back the app."
            + ("\n\n" + _bash(f"python manage.py migrate {m.app} {target}") if m.has_downgrade
               else " It is not reversible (RunPython/RunSQL without a reverse) — restore from backup.")
        )

    revert = _bash(f"git revert --no-edit {' '.join(c[:12] for c in commits)}", "git push")
    revert_text = (f"**Revert the PR's {len(commits)} commit{'s' if len(commits) != 1 else ''}** so the next "
                   f"deploy doesn't bring the change back. If it was merged with a merge commit, revert that "
                   f"instead with `git revert -m 1` on the merge.\n\n{revert}")

    # 3 + 4. app rollback and revert
    if k8s:
        for w in k8s:
            steps.append(
                f"**Roll back `{_workload_ref(w)}`** (namespace `{w.namespace}`, {w.file}) to the previous "
                f"ReplicaSet.\n\n"
                + _bash(f"kubectl rollout undo {_workload_ref(w)} {_ns(w)}",
                        f"kubectl rollout status {_workload_ref(w)} {_ns(w)}")
            )
        steps.append(revert_text)
    elif facts.compose_services:
        steps.append(revert_text)
        steps.append(
            "**Rebuild and restart the reverted code** with docker compose.\n\n"
            + _bash(f"docker compose up -d --build {' '.join(facts.compose_services)}")
        )
    else:
        pipeline = (f" Pushing the revert triggers {', '.join(facts.workflows)}, which redeploys it."
                    if facts.workflows else " Redeploy the reverted commit through your usual pipeline.")
        steps.append(revert_text + "\n\n" + pipeline.strip())

    # 5. verify
    verify: list[str] = []
    if blast:
        tests = sorted({c.file for c in blast.callers if c.file in facts.test_files})
        tests += [f for f in (d.path for d in diff.files) if f in facts.test_files and f not in tests]
        if tests:
            verify.append("Run the tests that cover the blast radius:\n\n" + _bash("pytest " + " ".join(tests)))
        sig_changed = {s.fq_name: s for s in diff.symbols if s.change == ChangeType.SIGNATURE_CHANGED}
        for fq, sym in sig_changed.items():
            touched = {f.path for f in diff.files}
            updated = [c for c in blast.callers_of(fq) if c.file in touched and c.usage == "call"]
            if updated:
                verify.append(
                    f"`{sym.qualname}` is back to `{sym.old_sig}` after the revert. These callers were changed in "
                    f"this PR and must be reverted with it — if any later commit touched them, they'll break: "
                    + ", ".join(f"{c.file}:{c.line}" for c in updated) + "."
                )
    for env in facts.new_env_vars:
        verify.append(f"If `{env.name}` was set for this deploy it can stay — the previous version doesn't read it.")
    if verify:
        steps.append("**Verify the rollback**\n\n" + "\n\n".join("- " + v.replace("\n", "\n  ") for v in verify))

    # indent continuation lines so code blocks stay inside their list item
    body = "\n\n".join(f"{i}. " + s.replace("\n", "\n   ") for i, s in enumerate(steps, 1))
    body = "\n".join(line.rstrip() for line in body.splitlines())
    triggers = "\n".join(f"- {t}" for t in _triggers(diff, checklist, facts))
    return f"**Roll back if:**\n\n{triggers}\n\n{body}\n"
