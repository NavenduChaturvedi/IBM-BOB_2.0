from blastradius import gitio


def test_parse_range():
    assert gitio.parse_range("main...feature/x") == ("main", "feature/x")
    assert gitio.parse_range("main..feature/x") == ("main", "feature/x")
    assert gitio.parse_range("main") == ("main", "HEAD")


def test_name_status_and_file_diff(tmp_repo):
    tmp_repo.commit("init", {"a.py": "x = 1\n", "b.py": "y = 1\n"})
    tmp_repo.branch("feature")
    tmp_repo.commit("change", {"a.py": "x = 2\n", "c.py": "z = 1\n"})

    changes = gitio.name_status(tmp_repo.path, "main", "feature")
    assert sorted(changes) == [("A", "c.py", None), ("M", "a.py", None)]

    added, removed = gitio.file_diff(tmp_repo.path, "main", "feature", "a.py")
    assert added == ["x = 2"]
    assert removed == ["x = 1"]

    assert gitio.show_file(tmp_repo.path, "main", "c.py") is None
    assert len(gitio.commits_between(tmp_repo.path, "main", "feature")) == 1
