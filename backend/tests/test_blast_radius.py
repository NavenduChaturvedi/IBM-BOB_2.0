import sys
from pathlib import Path

import pytest

from blastradius.blast_radius import compute_blast_radius
from blastradius.callers import Target, find_callers
from blastradius.diff_parser import parse_diff
from blastradius.import_graph import build_import_graph, resolve_relative

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))
import seed_demo  # noqa: E402

LIB = "def f(x):\n    return x\n"


def _graph(tmp_repo, files):
    tmp_repo.commit("init", {"pkg/__init__.py": "", "pkg/lib.py": LIB, **files})
    return build_import_graph(tmp_repo.path, "HEAD")


def _sites(callers):
    return {(c.file, c.line) for c in callers}


def test_resolve_relative():
    assert resolve_relative("pkg.sub.mod", False, 1, "lib") == "pkg.sub.lib"
    assert resolve_relative("pkg.sub.mod", False, 2, "lib") == "pkg.lib"
    assert resolve_relative("pkg.sub", True, 1, "lib") == "pkg.sub.lib"
    assert resolve_relative("pkg.sub.mod", False, 1, None) == "pkg.sub"


@pytest.mark.parametrize("source,line", [
    ("from pkg.lib import f\n\nf(1)\n", 3),
    ("from pkg.lib import f as g\n\ng(1)\n", 3),
    ("from pkg import lib\n\nlib.f(1)\n", 3),
    ("import pkg.lib\n\npkg.lib.f(1)\n", 3),
    ("import pkg.lib as L\n\nL.f(1)\n", 3),
    ("from pkg.lib import *\n\nf(1)\n", 3),
    ("def run():\n    from pkg.lib import f\n    return f(1)\n", 3),
])
def test_import_styles(tmp_repo, source, line):
    graph = _graph(tmp_repo, {"app.py": source})
    callers = find_callers(graph, Target("pkg.lib", "f"))
    assert _sites(callers) == {("app.py", line)}


def test_relative_import(tmp_repo):
    graph = _graph(tmp_repo, {"pkg/user.py": "from .lib import f\n\nf(1)\n"})
    assert _sites(find_callers(graph, Target("pkg.lib", "f"))) == {("pkg/user.py", 3)}


def test_reexport_through_package_init(tmp_repo):
    graph = _graph(tmp_repo, {
        "pkg/__init__.py": "from .lib import f\n",
        "app.py": "from pkg import f\n\nf(1)\n",
    })
    assert ("app.py", 3) in _sites(find_callers(graph, Target("pkg.lib", "f")))


def test_unrelated_same_name_is_ignored(tmp_repo):
    graph = _graph(tmp_repo, {
        "other.py": "def f(x):\n    return x\n\nf(1)\n",
        "app.py": "from other import f\n\nf(1)\n# f(2) in a comment\n",
    })
    assert find_callers(graph, Target("pkg.lib", "f")) == []


def test_references_and_call_shape(tmp_repo):
    graph = _graph(tmp_repo, {"app.py": "from pkg.lib import f\n\nhandlers = [f]\nf(1, *rest, k=2, **kw)\n"})
    by_line = {c.line: c for c in find_callers(graph, Target("pkg.lib", "f"))}
    assert by_line[3].usage == "reference"
    call = by_line[4].call
    assert (call.n_positional, call.keywords, call.has_star_args, call.has_star_kwargs) == (1, ["k"], True, True)


def test_methods(tmp_repo):
    graph = _graph(tmp_repo, {
        "pkg/cart.py": (
            "class Cart:\n"
            "    def total(self):\n"
            "        return 1\n"
            "    def summary(self):\n"
            "        return self.total()\n"
        ),
        "app.py": "from pkg.cart import Cart\n\nCart.total(None)\nc = Cart()\nc.total()\n",
    })
    callers = {(c.file, c.line): c for c in find_callers(graph, Target("pkg.cart", "Cart.total"))}
    assert callers[("pkg/cart.py", 5)].confidence == "high"
    assert callers[("pkg/cart.py", 5)].enclosing_symbol == "Cart.summary"
    assert callers[("app.py", 3)].confidence == "high"
    assert callers[("app.py", 5)].confidence == "low"


def test_recursion_is_not_a_caller(tmp_repo):
    graph = _graph(tmp_repo, {"pkg/rec.py": "def g(n):\n    return g(n - 1) if n else 0\n"})
    assert find_callers(graph, Target("pkg.rec", "g")) == []


def test_uses_head_revision_not_working_tree(tmp_repo):
    tmp_repo.commit("init", {"pkg/__init__.py": "", "pkg/lib.py": LIB})
    tmp_repo.branch("feature")
    tmp_repo.commit("change", {"pkg/lib.py": "def f(x, y):\n    return x\n", "app.py": "from pkg.lib import f\nf(1)\n"})
    tmp_repo.checkout("main")  # working tree has no app.py
    diff = parse_diff(tmp_repo.path, "main", "feature")
    blast = compute_blast_radius(tmp_repo.path, diff)
    assert _sites(blast.callers) == {("app.py", 2)}


def test_removed_symbol_finds_leftover_callers(tmp_repo):
    tmp_repo.commit("init", {
        "pkg/__init__.py": "", "pkg/lib.py": LIB + "\ndef h():\n    pass\n",
        "app.py": "from pkg.lib import h\n\nh()\n",
    })
    tmp_repo.branch("feature")
    tmp_repo.commit("remove h", {"pkg/lib.py": LIB})
    blast = compute_blast_radius(tmp_repo.path, parse_diff(tmp_repo.path, "main", "feature"))
    assert _sites(blast.callers) == {("app.py", 3)}


def test_hop2_and_hops_flag(tmp_repo):
    tmp_repo.commit("init", {
        "pkg/__init__.py": "", "pkg/lib.py": LIB,
        "pkg/svc.py": "from pkg.lib import f\n\ndef service():\n    return f(1)\n",
        "pkg/api.py": "from pkg.svc import service\n\ndef handler():\n    return service()\n",
        "tests/test_svc.py": "from pkg.svc import service\n\ndef test_it():\n    service()\n",
    })
    tmp_repo.branch("feature")
    tmp_repo.commit("change f", {"pkg/lib.py": "def f(x):\n    return x * 2\n"})
    diff = parse_diff(tmp_repo.path, "main", "feature")

    blast = compute_blast_radius(tmp_repo.path, diff)
    hop2 = [c for c in blast.callers if c.hop == 2]
    assert _sites(hop2) == {("pkg/api.py", 4), ("tests/test_svc.py", 4)}
    assert all(c.root_symbol == "pkg.lib.f" and c.via_symbol == "pkg.svc.service" for c in hop2)

    assert all(c.hop == 1 for c in compute_blast_radius(tmp_repo.path, diff, hops=1).callers)


# --- demo scenarios -----------------------------------------------------------

@pytest.fixture(scope="module")
def demo_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("demo")
    seed_demo.seed(repo, force=True)
    return repo


def test_pr1_is_small(demo_repo):
    blast = compute_blast_radius(demo_repo, parse_diff(demo_repo, "main", "pr1/pagination-fix"))
    non_test = {c.file for c in blast.callers if not c.file.startswith("tests/")}
    assert non_test == {"app/api/routes.py"}


def test_pr3_finds_all_calculate_discount_callers(demo_repo):
    blast = compute_blast_radius(demo_repo, parse_diff(demo_repo, "main", "pr3/discount-tier"))
    direct = [c for c in blast.callers if c.via_symbol == "app.payments.pricing.calculate_discount"]
    assert _sites(direct) == {
        ("app/api/checkout.py", 7),
        ("app/api/cart.py", 5),
        ("tests/test_pricing.py", 5),
        ("tests/test_pricing.py", 9),
    }
    stale = [c for c in direct if c.call.n_positional == 2]
    assert {c.file for c in stale} == {"tests/test_pricing.py"}
