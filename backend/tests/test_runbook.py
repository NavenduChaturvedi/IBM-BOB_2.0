import sys
from pathlib import Path

import pytest

from blastradius.artifacts import scan_artifacts
from blastradius.blast_radius import compute_blast_radius
from blastradius.checklist import build_checklist
from blastradius.cli import main
from blastradius.diff_parser import parse_diff
from blastradius.runbook import generate_runbook, unwrap
from blastradius.runbook.backends import FileBackend, LLMError
from blastradius.runbook.prompt import build_prompt
from blastradius.runbook.template import render_template
from blastradius.runbook.validate import extract_commands, validate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))
import seed_demo  # noqa: E402


@pytest.fixture(scope="module")
def demo_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("demo")
    seed_demo.seed(repo, force=True)
    return repo


def _pipeline(repo, branch):
    diff = parse_diff(repo, "main", branch)
    blast = compute_blast_radius(repo, diff)
    facts = scan_artifacts(repo, diff)
    return diff, blast, facts, build_checklist(diff, blast, facts)


@pytest.fixture(scope="module")
def pr2(demo_repo):
    return _pipeline(demo_repo, "pr2/refund-status")


def _bash(*lines):
    return "```bash\n" + "\n".join(lines) + "\n```\n"


def test_extract_commands():
    md = "text\n```bash\n# comment\n$ kubectl rollout undo \\\n  deployment/api\n\ngit push\n```\n`inline` ignored\n"
    assert extract_commands(md) == ["kubectl rollout undo deployment/api", "git push"]


@pytest.mark.parametrize("branch", ["pr1/pagination-fix", "pr2/refund-status", "pr3/discount-tier"])
def test_templates_pass_their_own_validation(demo_repo, branch):
    diff, blast, facts, checklist = _pipeline(demo_repo, branch)
    assert validate(render_template(diff, facts, checklist, blast), facts) == []


def test_pr1_runbook_is_minimal(demo_repo):
    diff, blast, facts, checklist = _pipeline(demo_repo, "pr1/pagination-fix")
    md = render_template(diff, facts, checklist, blast)
    assert "alembic" not in md and "set env" not in md
    assert f"git revert --no-edit {facts.commits[0][:12]}" in md


def test_pr2_order_downgrade_before_rollout_undo(pr2):
    diff, blast, facts, checklist = pr2
    md = render_template(diff, facts, checklist, blast)
    flag, down, undo, revert = (md.index(s) for s in (
        "FLAG_NEW_REFUNDS=false", "alembic downgrade 4f1a2b3c5d6e", "rollout undo", "git revert"))
    assert flag < down < undo < revert
    assert "crash-looping" in md  # flag won't help if REFUND_WEBHOOK_URL kills startup
    assert "Data loss: drop_column payments.refund_status" in md


def test_pr3_lists_callers_to_recheck(demo_repo):
    diff, blast, facts, checklist = _pipeline(demo_repo, "pr3/discount-tier")
    md = render_template(diff, facts, checklist, blast)
    assert "app/api/cart.py:5" in md and "app/api/checkout.py:7" in md
    assert "pytest tests/test_pricing.py" in md


# --- validator rejects hallucinations -------------------------------------------

GOOD_PR2 = [
    "kubectl exec -n payments deployment/api -- alembic downgrade 4f1a2b3c5d6e",
    "kubectl rollout undo deployment/api -n payments",
]


def _good_pr2(facts, *extra):
    return _bash(*GOOD_PR2, f"git revert --no-edit {facts.commits[0][:10]} {facts.commits[1][:10]}", *extra)


def test_valid_bob_output_passes(pr2):
    facts = pr2[2]
    assert validate(_good_pr2(facts, "kubectl rollout status deploy/api --namespace=payments"), facts) == []


@pytest.mark.parametrize("bad,reason", [
    ("kubectl rollout undo deployment/web -n payments", "deployment `web` not found"),
    ("kubectl rollout undo deployment/api -n default", "namespace `payments`, not `default`"),
    ("kubectl rollout undo statefulset/api -n payments", "statefulset `api` not found"),
    ("git revert deadbeef123", "unknown commit/revision id `deadbeef123`"),
    ("helm rollback api 3", "`helm` has no backing artifact"),
    ("git revert <merge-sha>", "placeholder"),
    ("alembic downgrade -2", "undoes 2 migration(s); this PR adds 1"),
    ("alembic downgrade 0001_init", "alembic revision `0001_init` not found"),
    ("kubectl exec deployment/api -n payments -- alembic downgrade abcdef123456", "unknown commit/revision id"),
    ("kubectl set env deployment/api -n payments MADE_UP=1", "env var `MADE_UP`"),
    ("docker compose restart api", "no compose file"),
    ("pytest tests/test_refunds.py", "test path `tests/test_refunds.py` doesn't exist"),
    ("python manage.py migrate payments zero", "no Django migrations"),
    ("curl -X POST https://status.example.com/rollback", "`curl` has no backing artifact"),
    ("# 500s on POST /checkout", "`POST /checkout`: HTTP route isn't in the repo facts"),
])
def test_hallucinations_rejected(pr2, bad, reason):
    facts = pr2[2]
    rejections = validate(_good_pr2(facts, bad), facts)
    assert any(reason in r for r in rejections), rejections


@pytest.mark.parametrize("dropped,reason", [
    (0, "missing step: schema downgrade"),
    (1, "missing step: `kubectl rollout undo deployment/api`"),
])
def test_missing_critical_steps_rejected(pr2, dropped, reason):
    facts = pr2[2]
    kept = [c for i, c in enumerate(GOOD_PR2) if i != dropped]
    md = _bash(*kept, f"git revert {facts.commits[0][:10]}")
    assert any(reason in r for r in validate(md, facts))


def test_missing_revert_rejected(pr2):
    facts = pr2[2]
    assert "missing step: `git revert` of the PR's commits" in validate(_bash(*GOOD_PR2), facts)


def test_alembic_relative_step_accepted_when_it_matches(pr2):
    facts = pr2[2]
    md = _bash("kubectl exec -n payments deployment/api -- alembic downgrade -1",
               "kubectl rollout undo deployment/api -n payments", f"git revert {facts.commits[0][:8]}")
    assert validate(md, facts) == []


# --- generate_runbook fallbacks ---------------------------------------------------

class _Failing:
    name = "failing"

    def generate(self, prompt):
        raise LLMError("timeout")


def test_generate_runbook_sources(pr2, tmp_path, monkeypatch):
    diff, blast, facts, checklist = pr2
    monkeypatch.setattr("blastradius.runbook.build_prompt", lambda *a: "prompt")

    good = tmp_path / "good.md"
    good.write_text(_good_pr2(facts), encoding="utf-8")
    assert generate_runbook(diff, facts, checklist, blast, FileBackend(str(good))).source == "bob (validated)"

    bad = tmp_path / "bad.md"
    bad.write_text(_good_pr2(facts, "kubectl rollout undo deployment/web -n payments"), encoding="utf-8")
    rb = generate_runbook(diff, facts, checklist, blast, FileBackend(str(bad)))
    assert rb.source == "template (bob rejected)" and rb.rejections
    assert "alembic downgrade 4f1a2b3c5d6e" in rb.markdown  # fell back to template

    assert generate_runbook(diff, facts, checklist, blast, _Failing()).source == "template (bob unavailable)"
    assert generate_runbook(diff, facts, checklist, blast, None).source == "template"


# --- prompt + samples ---------------------------------------------------------------

SAMPLES = Path(__file__).resolve().parent.parent / "demo" / "bob_samples"


def test_prompt_contains_facts_and_template(pr2):
    diff, blast, facts, checklist = pr2
    template = render_template(diff, facts, checklist, blast)
    prompt = build_prompt(diff, facts, checklist, template, blast)
    assert '"down_revision": "4f1a2b3c5d6e"' in prompt
    assert '"name": "api"' in prompt and '"namespace": "payments"' in prompt
    assert facts.commits[0] in prompt
    assert template.strip() in prompt
    assert "Do not invent HTTP routes" in prompt


def test_unwrap():
    assert unwrap("```markdown\n**Roll back if:**\n- x\n```\n") == "**Roll back if:**\n- x\n"
    assert unwrap("**Roll back if:**\n```bash\nls\n```") == "**Roll back if:**\n```bash\nls\n```\n"
    assert unwrap("## Steps\n### Sub\n#### Deep") == "#### Steps\n#### Sub\n#### Deep\n"


def test_dump_prompt(demo_repo, tmp_path):
    prompt = tmp_path / "prompt.md"
    code = main(["--repo", str(demo_repo), "--diff", "main...pr3/discount-tier", "--no-llm",
                 "--dump-prompt", str(prompt), "--out", str(tmp_path / "r.md")])
    assert code == 0 and "## FACTS" in prompt.read_text(encoding="utf-8")


@pytest.mark.parametrize("sample,source", [
    ("pr3_valid.md", "bob (validated)"),
    ("pr3_hallucinated.md", "template (bob rejected)"),
])
def test_demo_samples(demo_repo, sample, source):
    diff, blast, facts, checklist = _pipeline(demo_repo, "pr3/discount-tier")
    rb = generate_runbook(diff, facts, checklist, blast, FileBackend(str(SAMPLES / sample)))
    assert rb.source == source
    if source.startswith("template"):
        assert len(rb.rejections) == 5
