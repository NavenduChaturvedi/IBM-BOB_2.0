import io
import sys
from pathlib import Path

import pytest
from rich.console import Console

from blastradius.cli import analyze
from blastradius.runbook.backends import FileBackend
from blastradius.terminal import render

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))
import seed_demo  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "demo" / "bob_samples"


@pytest.fixture(scope="module")
def demo_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("demo")
    seed_demo.seed(repo, force=True)
    return repo


def _render(repo, branch, backend=None) -> str:
    report, timings = analyze(repo, f"main...{branch}", backend=backend)
    console = Console(file=io.StringIO(), width=120, color_system=None)
    render(report, console, timings)
    return console.file.getvalue()


def test_pr1_quiet(demo_repo):
    out = _render(demo_repo, "pr1/pagination-fix")
    assert "0 HIGH" in out and "0 MED" in out and "1 LOW" in out
    assert "Runbook: deterministic template" in out
    assert "✗" not in out


def test_pr2_shows_env_var_and_migration(demo_repo):
    out = _render(demo_repo, "pr2/refund-status")
    assert "No existing callers affected" in out
    assert "REFUND_WEBHOOK_URL" in out and "alembic downgrade 4f1a2b3c5d6e" in out


def test_pr3_tree_marks_broken_and_compatible_calls(demo_repo):
    out = _render(demo_repo, "pr3/discount-tier")
    assert "2 calls will fail" in out
    assert "✗ tests/test_pricing.py:5" in out and "✗ tests/test_pricing.py:9" in out
    assert "✓ app/api/checkout.py:7" in out and "✓ app/api/routes.py:7" in out
    assert "missing required user_tier" in out  # backticks rendered as style, not text


def test_rejection_is_shown_not_hidden(demo_repo):
    out = _render(demo_repo, "pr3/discount-tier", FileBackend(str(SAMPLES / "pr3_hallucinated.md")))
    assert "Bob's draft was rejected" in out
    assert "deployment checkout not found" in out
    assert "Bob draft rejected → deterministic template" in out


def test_validated_bob_draft(demo_repo):
    out = _render(demo_repo, "pr3/discount-tier", FileBackend(str(SAMPLES / "pr3_valid.md")))
    assert "Bob ✓ validated against repo" in out
    assert "Confirm the failure is this PR" in out
