"""Build demo_repo/ with a fixed git history and three PR branches.

Usage: python demo/seed_demo.py [--force] [--dest PATH]

Branches:
  main                 baseline payments service
  pr1/pagination-fix   off-by-one fix in app/utils/pagination.py (low blast radius)
  pr2/refund-status    alembic migration + model field + new env var (checklist trigger)
  pr3/discount-tier    calculate_discount signature change, test caller left stale (centerpiece)

Commits use a fixed author and timestamps, so SHAs are identical on every run.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parent.parent
DEMO_REPO = ROOT / "demo_repo"

AUTHOR_NAME = "Demo Dev"
AUTHOR_EMAIL = "dev@example.com"
BASE_EPOCH = 1790000000  # fixed start time; each commit adds one hour

INIT_REV = "4f1a2b3c5d6e"
REFUND_REV = "9c8d7e6f5a4b"


def src(text: str) -> str:
    return dedent(text).lstrip("\n")


# --- main branch --------------------------------------------------------------

MAIN_FILES: dict[str, str] = {
    ".gitignore": "__pycache__/\n.pytest_cache/\n",
    "README.md": src("""
        # payments-api

        Small payments service used as the blastradius demo target.
        """),
    "requirements.txt": src("""
        fastapi==0.115.0
        sqlalchemy==2.0.35
        alembic==1.13.3
        uvicorn==0.30.6
        """),
    "app/__init__.py": "",
    "app/api/__init__.py": "",
    "app/payments/__init__.py": "",
    "app/utils/__init__.py": "",
    "app/flags.py": src("""
        import os

        FLAGS = {
            "new_refunds": os.getenv("FLAG_NEW_REFUNDS", "false") == "true",
        }


        def is_enabled(name):
            return FLAGS.get(name, False)
        """),
    "app/models.py": src("""
        from sqlalchemy import Column, Integer, Numeric, String
        from sqlalchemy.orm import declarative_base

        Base = declarative_base()


        class Payment(Base):
            __tablename__ = "payments"

            id = Column(Integer, primary_key=True)
            amount = Column(Numeric(10, 2), nullable=False)
            status = Column(String(32), nullable=False, default="pending")
        """),
    "app/payments/pricing.py": src("""
        DISCOUNT_CODES = {"SAVE10": 0.10, "SAVE20": 0.20}


        def calculate_discount(price, code):
            rate = DISCOUNT_CODES.get(code, 0.0)
            return round(price * (1 - rate), 2)
        """),
    "app/payments/utils.py": src("""
        def format_amount(amount):
            return f"${amount:,.2f}"
        """),
    "app/utils/pagination.py": src("""
        def paginate(items, page, per_page=20):
            \"\"\"Return one page of items. Pages are 1-indexed.\"\"\"
            start = page * per_page
            end = start + per_page
            return items[start:end]
        """),
    "app/api/checkout.py": src("""
        from app.payments.pricing import calculate_discount
        from app.payments.utils import format_amount


        def checkout_total(cart_items, promo_code):
            subtotal = sum(item["price"] * item["qty"] for item in cart_items)
            total = calculate_discount(subtotal, promo_code)
            return format_amount(total)
        """),
    "app/api/cart.py": src("""
        from app.payments import pricing


        def preview_price(item, promo_code):
            return pricing.calculate_discount(item["price"], promo_code)
        """),
    "app/api/routes.py": src("""
        from app.api.cart import preview_price
        from app.api.checkout import checkout_total
        from app.utils.pagination import paginate


        def post_checkout(request):
            return {"total": checkout_total(request["items"], request.get("promo"))}


        def get_cart_preview(request):
            return {"price": preview_price(request["item"], request.get("promo"))}


        def list_orders(request, orders):
            page = int(request.get("page", 1))
            return {"orders": paginate(orders, page)}
        """),
    "tests/__init__.py": "",
    "tests/test_pricing.py": src("""
        from app.payments.pricing import calculate_discount


        def test_known_code():
            assert calculate_discount(100, "SAVE10") == 90.0


        def test_unknown_code():
            assert calculate_discount(100, "BOGUS") == 100
        """),
    "tests/test_pagination.py": src("""
        from app.utils.pagination import paginate


        def test_page_size():
            assert len(paginate(list(range(100)), 2, per_page=10)) == 10
        """),
    "alembic.ini": src("""
        [alembic]
        script_location = alembic
        sqlalchemy.url = %(DATABASE_URL)s
        """),
    "alembic/env.py": src("""
        from alembic import context

        from app.models import Base

        target_metadata = Base.metadata


        def run_migrations_online():
            with context.begin_transaction():
                context.run_migrations()


        run_migrations_online()
        """),
    f"alembic/versions/{INIT_REV}_create_payments.py": src(f"""
        \"\"\"create payments table\"\"\"
        import sqlalchemy as sa
        from alembic import op

        revision = "{INIT_REV}"
        down_revision = None


        def upgrade():
            op.create_table(
                "payments",
                sa.Column("id", sa.Integer, primary_key=True),
                sa.Column("amount", sa.Numeric(10, 2), nullable=False),
                sa.Column("status", sa.String(32), nullable=False),
            )


        def downgrade():
            op.drop_table("payments")
        """),
    "Dockerfile": src("""
        FROM python:3.11-slim
        WORKDIR /srv
        COPY requirements.txt .
        RUN pip install --no-cache-dir -r requirements.txt
        COPY . .
        CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
        """),
    "k8s/deployment.yaml": src("""
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: api
          namespace: payments
        spec:
          replicas: 3
          selector:
            matchLabels:
              app: api
          template:
            metadata:
              labels:
                app: api
            spec:
              containers:
                - name: api
                  image: ghcr.io/example/payments-api:latest
                  ports:
                    - containerPort: 8000
                  env:
                    - name: DATABASE_URL
                      valueFrom:
                        secretKeyRef:
                          name: payments-db
                          key: url
                    - name: FLAG_NEW_REFUNDS
                      value: "false"
        """),
    ".github/workflows/deploy.yml": src("""
        name: deploy
        on:
          push:
            branches: [main]
        jobs:
          deploy:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - run: docker build -t ghcr.io/example/payments-api:${{ github.sha }} .
              - run: docker push ghcr.io/example/payments-api:${{ github.sha }}
              - run: alembic upgrade head
              - run: kubectl -n payments set image deployment/api api=ghcr.io/example/payments-api:${{ github.sha }}
        """),
}

# --- PR 1: tiny, contained bugfix ---------------------------------------------

PR1_COMMITS: list[tuple[str, dict[str, str]]] = [
    ("Fix off-by-one in paginate", {
        "app/utils/pagination.py": src("""
            def paginate(items, page, per_page=20):
                \"\"\"Return one page of items. Pages are 1-indexed.\"\"\"
                start = (page - 1) * per_page
                end = start + per_page
                return items[start:end]
            """),
        "tests/test_pagination.py": src("""
            from app.utils.pagination import paginate


            def test_page_size():
                assert len(paginate(list(range(100)), 2, per_page=10)) == 10


            def test_first_page_starts_at_zero():
                assert paginate(list(range(100)), 1, per_page=10)[0] == 0
            """),
    }),
]

# --- PR 2: migration + new env var + feature flag ------------------------------

PR2_COMMITS: list[tuple[str, dict[str, str]]] = [
    ("Add refund_status column to payments", {
        f"alembic/versions/{REFUND_REV}_add_refund_status.py": src(f"""
            \"\"\"add refund_status to payments\"\"\"
            import sqlalchemy as sa
            from alembic import op

            revision = "{REFUND_REV}"
            down_revision = "{INIT_REV}"


            def upgrade():
                op.add_column(
                    "payments",
                    sa.Column("refund_status", sa.String(32), nullable=True),
                )


            def downgrade():
                op.drop_column("payments", "refund_status")
            """),
        "app/models.py": src("""
            from sqlalchemy import Column, Integer, Numeric, String
            from sqlalchemy.orm import declarative_base

            Base = declarative_base()


            class Payment(Base):
                __tablename__ = "payments"

                id = Column(Integer, primary_key=True)
                amount = Column(Numeric(10, 2), nullable=False)
                status = Column(String(32), nullable=False, default="pending")
                refund_status = Column(String(32), nullable=True)
            """),
    }),
    ("Send refund webhooks behind new_refunds flag", {
        "app/payments/refunds.py": src("""
            import json
            import os
            import urllib.request

            from app.flags import is_enabled

            REFUND_WEBHOOK_URL = os.environ["REFUND_WEBHOOK_URL"]


            def mark_refunded(payment):
                payment.refund_status = "refunded"
                if is_enabled("new_refunds"):
                    notify_refund(payment)
                return payment


            def notify_refund(payment):
                body = json.dumps({"payment_id": payment.id, "status": payment.refund_status})
                req = urllib.request.Request(
                    REFUND_WEBHOOK_URL,
                    data=body.encode(),
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=5)
            """),
    }),
]

# --- PR 3: breaking signature change, one caller left stale --------------------

PR3_COMMITS: list[tuple[str, dict[str, str]]] = [
    ("Add user_tier to calculate_discount", {
        "app/payments/pricing.py": src("""
            DISCOUNT_CODES = {"SAVE10": 0.10, "SAVE20": 0.20}
            TIER_BONUS = {"standard": 0.0, "gold": 0.05, "platinum": 0.10}


            def calculate_discount(price, code, user_tier):
                rate = DISCOUNT_CODES.get(code, 0.0) + TIER_BONUS.get(user_tier, 0.0)
                return round(price * (1 - min(rate, 0.5)), 2)
            """),
    }),
    ("Pass user_tier through checkout and cart", {
        "app/api/checkout.py": src("""
            from app.payments.pricing import calculate_discount
            from app.payments.utils import format_amount


            def checkout_total(cart_items, promo_code, user_tier="standard"):
                subtotal = sum(item["price"] * item["qty"] for item in cart_items)
                total = calculate_discount(subtotal, promo_code, user_tier)
                return format_amount(total)
            """),
        "app/api/cart.py": src("""
            from app.payments import pricing


            def preview_price(item, promo_code, user_tier="standard"):
                return pricing.calculate_discount(item["price"], promo_code, user_tier)
            """),
        # tests/test_pricing.py intentionally NOT updated: the stale caller the demo should catch.
    }),
]

BRANCHES = [
    ("pr1/pagination-fix", PR1_COMMITS),
    ("pr2/refund-status", PR2_COMMITS),
    ("pr3/discount-tier", PR3_COMMITS),
]


# --- git plumbing -------------------------------------------------------------

class Seeder:
    def __init__(self, repo: Path):
        self.repo = repo
        self.tick = 0

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
            capture_output=True, text=True, env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def write(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)

    def commit(self, message: str, files: dict[str, str]) -> str:
        self.write(files)
        self.git("add", "-A")
        date = f"{BASE_EPOCH + self.tick * 3600} +0000"
        self.tick += 1
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": AUTHOR_NAME, "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL, "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": AUTHOR_NAME, "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL, "GIT_COMMITTER_DATE": date,
        }
        self.git("commit", "-q", "-m", message, env=env)
        return self.git("rev-parse", "HEAD")


def _force_remove(func, path, _exc):
    # git marks object files read-only, which breaks rmtree on Windows
    os.chmod(path, stat.S_IWRITE)
    func(path)


def seed(dest: Path, force: bool = False) -> dict[str, list[str]]:
    """Create the demo repo at dest. Returns branch -> list of commit SHAs (oldest first)."""
    if dest.exists() and any(dest.iterdir()):
        if not force:
            raise FileExistsError(f"{dest} already exists (use --force to rebuild)")
        # empty the directory rather than removing it: on Windows the directory itself
        # is often locked by a terminal or editor whose cwd is inside it
        for child in dest.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, onexc=_force_remove)
            else:
                os.chmod(child, stat.S_IWRITE)
                child.unlink()
    dest.mkdir(parents=True, exist_ok=True)

    s = Seeder(dest)
    s.git("init", "-q", "-b", "main")
    s.git("config", "core.autocrlf", "false")

    shas = {"main": [s.commit("Initial payments service", MAIN_FILES)]}
    for branch, commits in BRANCHES:
        s.git("checkout", "-q", "-b", branch, "main")
        shas[branch] = [s.commit(msg, files) for msg, files in commits]
    s.git("checkout", "-q", "main")
    return shas


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dest", type=Path, default=DEMO_REPO)
    p.add_argument("--force", action="store_true", help="delete and rebuild if dest exists")
    args = p.parse_args(argv)

    try:
        shas = seed(args.dest, force=args.force)
    except (FileExistsError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"demo repo created at {args.dest}")
    for branch, commits in shas.items():
        print(f"  {branch:<22} {' '.join(c[:10] for c in commits)}")
    print("\ntry: python blastradius.py --repo demo_repo --diff main...pr3/discount-tier")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
