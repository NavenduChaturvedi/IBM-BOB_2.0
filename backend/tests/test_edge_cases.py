"""Stress cases from the QA notes: non-Python diffs, deletions, renames, binaries, huge files."""
import os

from blastradius.cli import analyze
from blastradius.gitio import numstat

LIB = "def f():\n    pass\n\ndef h():\n    pass\n"
BASE = {
    "pkg/__init__.py": "", "pkg/lib.py": LIB,
    "app.py": "from pkg.lib import h\nh()\n",
    "README.md": "x\n",
    "tests/test_a.py": "def test_x():\n    assert 1\n",
}


def _branch(tmp_repo, name, change):
    tmp_repo.commit("init", BASE)
    tmp_repo.branch(name)
    change(tmp_repo.path)
    tmp_repo.commit(name)
    report, _ = analyze(tmp_repo.path, f"main...{name}")
    return report


def _rules(report):
    return [i.rule_id for i in report.checklist]


def test_docs_only_diff_is_quiet(tmp_repo):
    report = _branch(tmp_repo, "docs", lambda p: (p / "README.md").write_text("y\n"))
    assert report.diff.symbols == [] and report.blast.callers == [] and report.checklist == []


def test_deleted_module_flags_remaining_callers(tmp_repo):
    report = _branch(tmp_repo, "delete", lambda p: (p / "pkg/lib.py").unlink())
    removed = [i for i in report.checklist if i.rule_id == "SYMBOL_REMOVED"]
    assert removed and "app.py:2" in removed[0].text


def test_moved_module_flags_stale_imports_but_not_missing_tests(tmp_repo):
    report = _branch(tmp_repo, "move", lambda p: (p / "pkg/lib.py").rename(p / "pkg/lib2.py"))
    assert report.diff.files[0].pure_rename
    assert "SYMBOL_REMOVED" in _rules(report)  # app.py still imports pkg.lib
    assert "NO_TESTS" not in _rules(report)    # moving code adds no logic to test


def test_binary_file_is_counted_not_read(tmp_repo):
    report = _branch(tmp_repo, "bin", lambda p: (p / "data.bin").write_bytes(os.urandom(4000)))
    [f] = report.diff.files
    assert f.binary and f.added_lines == [] and report.checklist == []


def test_huge_text_diff_is_summarized(tmp_repo):
    lock = "".join(f"pkg{i}==1.0.{i}\n" for i in range(6000))
    report = _branch(tmp_repo, "lock", lambda p: (p / "big.lock").write_text(lock))
    [f] = report.diff.files
    assert f.summarized and f.added_lines == []
    assert "LARGE_DIFF" in _rules(report)


def test_numstat_handles_renames_and_binaries(tmp_repo):
    tmp_repo.commit("init", BASE)
    tmp_repo.branch("mix")
    (tmp_repo.path / "pkg/lib.py").rename(tmp_repo.path / "pkg/moved.py")
    (tmp_repo.path / "img.bin").write_bytes(b"\x00\x01" * 100)
    tmp_repo.commit("mix")
    stats = numstat(tmp_repo.path, "main", "mix")
    assert stats["pkg/moved.py"] == (0, 0) and stats["img.bin"] == (None, None)
