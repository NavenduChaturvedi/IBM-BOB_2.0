"""Component 4b: rollback runbook — deterministic template, optional Bob pass, validator."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from ..models import BlastRadius, ChecklistItem, DiffResult, RollbackFacts, Runbook
from .backends import LLMBackend, LLMError
from .prompt import build_prompt
from .template import render_template
from .validate import validate

_WRAPPED_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.DOTALL)


def unwrap(markdown: str) -> str:
    """LLMs often wrap the whole answer in a ```markdown fence; strip it.
    Headings are demoted to ####, below the report's own ### section headings."""
    m = _WRAPPED_RE.match(markdown)
    text = m.group(1).strip() if m else markdown.strip()
    text = re.sub(r"^#{1,3} ", "#### ", text, flags=re.MULTILINE)
    return text + "\n"


def generate_runbook(
    diff: DiffResult,
    facts: RollbackFacts,
    checklist: list[ChecklistItem],
    blast: BlastRadius,
    backend: LLMBackend | None,
    dump_prompt: Path | None = None,
    on_stage: Callable[[str], None] = lambda _: None,
) -> Runbook:
    template_md = render_template(diff, facts, checklist, blast)
    if backend is None and dump_prompt is None:
        return Runbook(markdown=template_md, source="template")

    prompt = build_prompt(diff, facts, checklist, template_md, blast)
    if dump_prompt is not None:
        dump_prompt.write_text(prompt, encoding="utf-8")
    if backend is None:
        return Runbook(markdown=template_md, source="template")

    try:
        on_stage(f"Asking Bob ({getattr(backend, 'model', backend.name)}) to improve the runbook")
        bob_md = unwrap(backend.generate(prompt))
    except LLMError as e:
        return Runbook(markdown=template_md, source="template (bob unavailable)", rejections=[str(e)])

    on_stage("Validating Bob's draft against repo facts")
    rejections = validate(bob_md, facts)
    if rejections:
        return Runbook(markdown=template_md, source="template (bob rejected)", rejections=rejections)
    return Runbook(markdown=bob_md, source="bob (validated)")
