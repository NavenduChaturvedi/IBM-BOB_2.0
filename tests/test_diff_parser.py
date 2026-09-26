import sys
from pathlib import Path

import pytest

from blastradius.diff_parser import classify, diff_symbols, parse_diff, path_to_module
from blastradius.models import ChangeType, FileKind

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))
import seed_demo  # noqa: E402


@pytest.mark.parametrize("path,content,kind", [
    ("app/payments/pricing.py", None, FileKind.PYTHON),
    ("tests/test_pricing.py", None, FileKind.TEST),
    ("app/pricing_test.py", None, FileKind.TEST),
    ("alembic/versions/abc_add_col.py", None, FileKind.MIGRATION),
    ("db/versions/abc.py", "down_revision = 'x'", FileKind.MIGRATION),
    ("shop/migrations/0002_add_field.py", None, FileKind.MIGRATION),
    ("shop/migrations/__init__.py", None, FileKind.PYTHON),
    ("Dockerfile", None, FileKind.DOCKERFILE),
    ("docker/api.Dockerfile", None, FileKind.DOCKERFILE),
    ("requirements-dev.txt", None, FileKind.REQUIREMENTS),
    ("pyproject.toml", None, FileKind.REQUIREMENTS),
    (".github/workflows/deploy.yml", None, FileKind.WORKFLOW),
    ("k8s/deployment.yaml", None, FileKind.K8S),
    ("infra/api.yaml", "apiVersion: apps/v1\nkind: Deployment\n", FileKind.K8S),
    ("docker-compose.yml", "services: {}", FileKind.CONFIG),
    ("config/settings.toml", None, FileKind.CONFIG),
    ("README.md", None, FileKind.OTHER),
])
def test_classify(path, content, kind):
    assert classify(path, content) == kind


def test_path_to_module():
    assert path_to_module("app/payments/pricing.py") == "app.payments.pricing"
    assert path_to_module("app/__init__.py") == "app"
    assert path_to_module("src/pkg/mod.py") == "pkg.mod"


def _by_name(changes):
    return {c.qualname: c for c in changes}


def test_signature_vs_body_change():
    old = "def f(a, b):\n    return a + b\n\ndef g(x):\n    return x\n"
    new = "def f(a, b, c=1):\n    return a + b\n\ndef g(x):\n    return x * 2\n"
    changes = _by_name(diff_symbols("m.py", "m.py", old, new))
    assert changes["f"].change == ChangeType.SIGNATURE_CHANGED
    assert changes["f"].old_sig == "f(a, b)"
    assert changes["f"].new_sig == "f(a, b, c=1)"
    assert changes["g"].change == ChangeType.BODY_CHANGED


def test_formatting_and_moves_are_not_changes():
    old = "def f(a):\n    return a+1\n\ndef g():\n    pass\n"
    new = "def g():\n    pass\n\n\ndef f(a):\n    # comment\n    return (a + 1)\n"
    assert diff_symbols("m.py", "m.py", old, new) == []


def test_methods_classes_and_variables():
    old = "X = 1\n\nclass C:\n    y = 1\n    def m(self):\n        return 1\n"
    new = "X = 2\nZ = 3\n\nclass C:\n    y = 1\n    def m(self, k):\n        return 1\n"
    changes = _by_name(diff_symbols("m.py", "m.py", old, new))
    assert changes["X"].change == ChangeType.BODY_CHANGED
    assert changes["Z"].change == ChangeType.ADDED
    assert changes["C.m"].change == ChangeType.SIGNATURE_CHANGED
    assert changes["C.m"].kind == "method"
    assert "C" not in changes  # only a method changed, not the class body


def test_added_and_deleted_files():
    src = "def f():\n    pass\n"
    assert [c.change for c in diff_symbols(None, "m.py", None, src)] == [ChangeType.ADDED]
    assert [c.change for c in diff_symbols("m.py", None, src, None)] == [ChangeType.REMOVED]


def test_rename_marks_old_module_removed():
    src = "def f():\n    pass\n"
    changes = diff_symbols("pkg/old.py", "pkg/new.py", src, src)
    assert {(c.module, c.change) for c in changes} == {
        ("pkg.old", ChangeType.REMOVED),
        ("pkg.new", ChangeType.ADDED),
    }


def test_syntax_error_is_recorded_not_raised(tmp_repo):
    tmp_repo.commit("init", {"m.py": "def f():\n    pass\n"})
    tmp_repo.branch("broken")
    tmp_repo.commit("break", {"m.py": "def f(:\n"})
    result = parse_diff(tmp_repo.path, "main", "broken")
    assert result.files[0].parse_error is not None
    assert result.symbols == []


# --- demo scenarios -----------------------------------------------------------

@pytest.fixture(scope="module")
def demo_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("demo")
    seed_demo.seed(repo, force=True)
    return repo


def _summary(result):
    return {(s.module, s.qualname, s.change) for s in result.symbols}


def test_pr1(demo_repo):
    result = parse_diff(demo_repo, "main", "pr1/pagination-fix")
    assert _summary(result) == {
        ("app.utils.pagination", "paginate", ChangeType.BODY_CHANGED),
        ("tests.test_pagination", "test_first_page_starts_at_zero", ChangeType.ADDED),
    }
    assert len(result.commits) == 1


def test_pr2(demo_repo):
    result = parse_diff(demo_repo, "main", "pr2/refund-status")
    kinds = {f.path: f.kind for f in result.files}
    assert kinds[f"alembic/versions/{seed_demo.REFUND_REV}_add_refund_status.py"] == FileKind.MIGRATION
    summary = _summary(result)
    assert ("app.models", "Payment", ChangeType.BODY_CHANGED) in summary
    assert ("app.payments.refunds", "REFUND_WEBHOOK_URL", ChangeType.ADDED) in summary
    assert ("app.payments.refunds", "mark_refunded", ChangeType.ADDED) in summary
    refunds = next(f for f in result.files if f.path == "app/payments/refunds.py")
    assert any("REFUND_WEBHOOK_URL" in line for line in refunds.added_lines)
    assert len(result.commits) == 2


def test_pr3(demo_repo):
    result = parse_diff(demo_repo, "main", "pr3/discount-tier")
    sym = {s.qualname: s for s in result.symbols}
    assert sym["calculate_discount"].change == ChangeType.SIGNATURE_CHANGED
    assert sym["calculate_discount"].old_sig == "calculate_discount(price, code)"
    assert sym["calculate_discount"].new_sig == "calculate_discount(price, code, user_tier)"
    assert sym["checkout_total"].change == ChangeType.SIGNATURE_CHANGED
    assert sym["preview_price"].change == ChangeType.SIGNATURE_CHANGED
    assert sym["TIER_BONUS"].change == ChangeType.ADDED
    assert not any(f.kind == FileKind.TEST for f in result.files)  # the stale test
