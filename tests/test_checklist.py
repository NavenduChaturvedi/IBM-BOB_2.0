import sys
from pathlib import Path

import pytest

from blastradius.artifacts import scan_artifacts
from blastradius.blast_radius import compute_blast_radius
from blastradius.checklist import build_checklist, call_breaks, parse_signature
from blastradius.diff_parser import parse_diff
from blastradius.models import CallShape, Severity

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))
import seed_demo  # noqa: E402


def _call(n=0, kw=(), star=False, starkw=False):
    return CallShape(n, list(kw), star, starkw, "")


@pytest.mark.parametrize("sig,call,broken", [
    ("f(a, b, c)", _call(2), "missing required `c`"),
    ("f(a, b, c=1)", _call(2), None),
    ("f(a, b)", _call(3), "passes 3 positional args, signature takes 2"),
    ("f(a, *args)", _call(5), None),
    ("f(a, b)", _call(1, ["b"]), None),
    ("f(a, b)", _call(1, ["a"]), "`a` given twice"),
    ("f(a)", _call(1, ["z"]), "unknown keyword `z`"),
    ("f(a, **kw)", _call(1, ["z"]), None),
    ("f(a, *, k)", _call(1), "missing required `k`"),
    ("f(a, *, k=1)", _call(1), None),
    ("f(a, /, b)", _call(0, ["a", "b"]), "unknown keyword `a`"),
    ("f(a, b, c)", _call(1, star=True), None),  # can't judge *args
    ("async f(a) -> int", _call(1), None),
])
def test_call_breaks(sig, call, broken):
    reason = call_breaks(parse_signature(sig), call)
    assert (reason is None) if broken is None else (reason is not None and broken in reason)


def test_call_breaks_method_drops_self():
    args = parse_signature("total(self, currency)")
    assert call_breaks(args, _call(1), drop_self=True) is None
    assert "missing required `currency`" in call_breaks(args, _call(0), drop_self=True)


def _run(repo, branch):
    diff = parse_diff(repo, "main", branch)
    blast = compute_blast_radius(repo, diff)
    facts = scan_artifacts(repo, diff)
    return build_checklist(diff, blast, facts)


def _ids(items, severity=None):
    return [i.rule_id for i in items if severity is None or i.severity == severity]


def test_removed_symbol_with_callers(tmp_repo):
    tmp_repo.commit("init", {
        "pkg/__init__.py": "", "pkg/lib.py": "def f():\n    pass\n\ndef h():\n    pass\n",
        "app.py": "from pkg.lib import h\n\nh()\n",
    })
    tmp_repo.branch("feature")
    tmp_repo.commit("remove h", {"pkg/lib.py": "def f():\n    pass\n"})
    items = _run(tmp_repo.path, "feature")
    removed = [i for i in items if i.rule_id == "SYMBOL_REMOVED"]
    assert removed and removed[0].severity == Severity.HIGH and "app.py:3" in removed[0].text


def test_env_var_declared_vs_undeclared(tmp_repo):
    tmp_repo.commit("init", {
        "app.py": "x = 1\n",
        ".env.example": "KNOWN=1\n",
    })
    tmp_repo.branch("feature")
    tmp_repo.commit("env", {"app.py": (
        "import os\n"
        "x = 1\n"
        "KNOWN = os.environ['KNOWN']\n"
        "def later():\n"
        "    return os.getenv('OPTIONAL', 'x')\n"
    )})
    items = {i.text.split("`")[1]: i for i in _run(tmp_repo.path, "feature") if i.rule_id == "NEW_ENV_VAR"}
    assert items["KNOWN"].severity == Severity.LOW
    assert items["OPTIONAL"].severity == Severity.MED


def test_risky_migration_and_missing_downgrade(tmp_repo):
    tmp_repo.commit("init", {"app.py": "x = 1\n"})
    tmp_repo.branch("feature")
    tmp_repo.commit("migration", {"alembic/versions/abc_drop.py": (
        "from alembic import op\n"
        "revision = 'abc'\n"
        "down_revision = 'prev'\n"
        "def upgrade():\n"
        "    op.drop_column('users', 'legacy')\n"
        "def downgrade():\n"
        "    pass\n"
    )})
    items = _run(tmp_repo.path, "feature")
    assert "MIGRATION" in _ids(items, Severity.HIGH)
    assert "MIGRATION_NO_DOWNGRADE" in _ids(items, Severity.HIGH)


def test_deps_and_deploy_config(tmp_repo):
    tmp_repo.commit("init", {"requirements.txt": "flask==3.0\n", "Dockerfile": "FROM python:3.11\n"})
    tmp_repo.branch("feature")
    tmp_repo.commit("bump", {"requirements.txt": "flask==3.1\n", "Dockerfile": "FROM python:3.12\n"})
    items = {i.rule_id: i for i in _run(tmp_repo.path, "feature")}
    assert "+flask==3.1" in items["DEPS_CHANGED"].text
    assert "Dockerfile" in items["DEPLOY_CONFIG_CHANGED"].text


# --- demo scenarios -----------------------------------------------------------

@pytest.fixture(scope="module")
def demo_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("demo")
    seed_demo.seed(repo, force=True)
    return repo


def test_pr1_does_not_cry_wolf(demo_repo):
    items = _run(demo_repo, "pr1/pagination-fix")
    assert all(i.severity == Severity.LOW for i in items)
    assert _ids(items) == ["BEHAVIOR_CHANGED"]


def test_pr2_flags_env_var_migration_and_flag(demo_repo):
    items = _run(demo_repo, "pr2/refund-status")
    by_id = {i.rule_id: i for i in items}
    assert by_id["NEW_ENV_VAR"].severity == Severity.HIGH
    assert "crash on startup" in by_id["NEW_ENV_VAR"].text
    assert seed_demo.REFUND_REV in by_id["MIGRATION"].text
    assert "drop_column payments.refund_status" in by_id["MIGRATION_ROLLBACK_DATA_LOSS"].text
    assert "FLAG_NEW_REFUNDS" in by_id["FEATURE_FLAG"].text


def test_pr3_flags_stale_test_callers_only(demo_repo):
    items = _run(demo_repo, "pr3/discount-tier")
    high = [i for i in items if i.severity == Severity.HIGH]
    assert len(high) == 1 and high[0].rule_id == "SIGNATURE_CHANGED"
    assert "2 of 4 known call sites" in high[0].text
    assert "tests/test_pricing.py:5" in high[0].text and "app/api/checkout.py" not in high[0].text
    compatible = [i for i in items if i.rule_id == "SIGNATURE_CHANGED" and i.severity == Severity.LOW]
    assert {i.text.split("(")[0].strip("`") for i in compatible} == {"checkout_total", "preview_price"}
