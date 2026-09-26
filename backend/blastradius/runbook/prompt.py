"""Constrained prompt for Bob: facts + template in, improved runbook out.

Bob is asked to improve the explanation and ordering of a runbook that already
works, not to invent one. Everything it may reference is in the FACTS block; the
validator enforces that afterwards.
"""
from __future__ import annotations

import json

from ..checklist import call_verdicts
from ..models import BlastRadius, ChecklistItem, DiffResult, RollbackFacts

SYSTEM_RULES = """\
You are writing a rollback runbook for a specific pull request.
Rules:
- Use ONLY the commit SHAs, deployment names, namespaces, migration revisions,
  env vars, services, test files, and feature flags listed in FACTS. Never invent
  identifiers, hostnames, URLs, or tools.
- If FACTS has no Kubernetes workloads, do not emit kubectl commands. Same for
  docker compose, alembic, Django, and feature flags.
- Every command must be runnable as written: no <placeholders>.
- Refer to code by function and file names. Do not invent HTTP routes or
  endpoint paths (e.g. "POST /checkout"), dashboards, or metric names.
- Do not contradict the pre-flight checklist: a signature change it marks as
  backward compatible is not breaking.
- Each blast-radius call site is tagged [fits new signature] or [BREAKS: ...].
  These tags come from static analysis: only [BREAKS] sites can raise errors
  from the signature change. Never describe a [fits new signature] site as failing.
- Schema downgrades must run from the NEW code: the previous image does not
  contain this PR's migration files. If you tell the reader to roll back the
  workload before downgrading, the downgrade must use the local-checkout command.
- Put every command in a fenced ```bash block.
- Keep every step of the TEMPLATE (flag off, schema downgrade, workload rollback,
  git revert). Keep its order unless you state why you changed it.
- Output markdown only, starting with "**Roll back if:**". No preamble.
"""

TASK = """\
Improve the TEMPLATE runbook for an on-call engineer who has never seen this PR:
- Sharpen the "Roll back if" signals using the checklist and blast radius
  (which endpoints/functions will fail first, what the error looks like).
- For each step, add one line on what to check before moving on.
- Call out decision points (e.g. flag off is enough vs. full rollback).
- Keep it short: numbered steps, no more than 2 sentences of prose per step.
"""

MAX_CALLERS = 20


def _facts_json(facts: RollbackFacts, relevant_tests: set[str]) -> str:
    data = {
        "base_sha": facts.base_sha,
        "head_sha": facts.head_sha,
        "pr_commits_newest_first": facts.commits,
        "k8s_workloads": [
            {"kind": w.kind, "name": w.name, "namespace": w.namespace, "containers": w.containers,
             "env": w.env, "file": w.file}
            for w in facts.k8s_deployments
        ],
        "migrations_in_this_pr": [
            {"tool": m.tool, "app": m.app, "revision": m.revision, "down_revision": m.down_revision,
             "has_downgrade": m.has_downgrade, "upgrade_ops": [str(o) for o in m.upgrade_ops],
             "downgrade_ops": [str(o) for o in m.downgrade_ops], "file": m.file}
            for m in facts.migrations if m.in_diff
        ],
        "all_migration_revisions": [m.revision for m in facts.migrations],
        "compose_services": facts.compose_services,
        "dockerfiles": facts.dockerfiles,
        "ci_workflows": facts.workflows,
        "feature_flags_in_new_code": [{"name": f.name, "file": f.file, "line": f.line} for f in facts.feature_flags],
        "new_env_vars": [{"name": e.name, "required": e.required, "read_at_import": e.at_import,
                          "declared_in": facts.declared_env.get(e.name, [])} for e in facts.new_env_vars],
        "declared_env_vars": sorted(facts.declared_env),
        "test_files": sorted(relevant_tests),
    }
    return json.dumps(data, indent=2)


def _call_verdicts(diff: DiffResult, blast: BlastRadius) -> dict[tuple[str, int], str]:
    return {key: f"BREAKS: {reason}" if reason else "fits new signature"
            for key, reason in call_verdicts(diff, blast).items()}


def build_prompt(
    diff: DiffResult,
    facts: RollbackFacts,
    checklist: list[ChecklistItem],
    template_md: str,
    blast: BlastRadius | None = None,
) -> str:
    changed = "\n".join(
        f"- {s.fq_name} ({s.change.value}" + (f": {s.old_sig} -> {s.new_sig}" if s.new_sig and s.old_sig and s.old_sig != s.new_sig else "") + ")"
        for s in diff.symbols
    ) or "- (no Python symbols changed)"
    other = [f"{f.path} ({f.kind.value})" for f in diff.files if f.kind.value not in ("python", "test")]

    callers = "- (none found)"
    relevant_tests = {f.path for f in diff.files if f.path in facts.test_files}
    if blast and blast.callers:
        verdicts = _call_verdicts(diff, blast)
        callers = "\n".join(
            f"- {c.file}:{c.line} in {c.enclosing_symbol or 'module'} (hop {c.hop}) -> {c.via_symbol}"
            + (f": {c.call.text}" if c.call else "")
            + (f" [{verdicts[(c.file, c.line)]}]" if (c.file, c.line) in verdicts else "")
            for c in blast.callers[:MAX_CALLERS]
        )
        if len(blast.callers) > MAX_CALLERS:
            callers += f"\n- ... and {len(blast.callers) - MAX_CALLERS} more"
        relevant_tests |= {c.file for c in blast.callers if c.file in facts.test_files}

    # all severities: LOW items carry verdicts like "backward compatible" that Bob must not contradict
    checks = "\n".join(f"- [{i.severity.value.upper()}] {i.text}" for i in checklist) or "- (no items)"

    return "\n".join([
        SYSTEM_RULES,
        f"## PR\n\nRange: {diff.base}...{diff.head}\n",
        f"## Changed code\n\n{changed}" + (f"\n\nOther files: {', '.join(other)}" if other else "") + "\n",
        f"## Blast radius (heuristic)\n\n{callers}\n",
        f"## Pre-flight checklist\n\n{checks}\n",
        f"## FACTS (the only identifiers you may use)\n\n```json\n{_facts_json(facts, relevant_tests)}\n```\n",
        f"## TEMPLATE\n\n{template_md.strip()}\n",
        f"## Task\n\n{TASK}",
    ])
