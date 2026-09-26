import sys
from pathlib import Path

from blastradius import gitio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))
import seed_demo  # noqa: E402


def test_seed_is_deterministic(tmp_path):
    first = seed_demo.seed(tmp_path / "a")
    second = seed_demo.seed(tmp_path / "b")
    assert first == second


def test_branches_have_expected_changes(tmp_path):
    repo = tmp_path / "demo"
    seed_demo.seed(repo)

    def changed(branch):
        return {path for _, path, _ in gitio.name_status(repo, "main", branch)}

    assert changed("pr1/pagination-fix") == {"app/utils/pagination.py", "tests/test_pagination.py"}
    assert changed("pr2/refund-status") == {
        f"alembic/versions/{seed_demo.REFUND_REV}_add_refund_status.py",
        "app/models.py",
        "app/payments/refunds.py",
    }
    # test_pricing.py must stay stale for the PR 3 demo
    assert changed("pr3/discount-tier") == {
        "app/payments/pricing.py",
        "app/api/checkout.py",
        "app/api/cart.py",
    }


def test_force_rebuild(tmp_path):
    repo = tmp_path / "demo"
    seed_demo.seed(repo)
    shas = seed_demo.seed(repo, force=True)
    assert len(shas["pr3/discount-tier"]) == 2
