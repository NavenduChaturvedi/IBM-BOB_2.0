"""Shared fixtures: throwaway git repos built from a dict of files."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class TmpRepo:
    def __init__(self, path: Path):
        self.path = path
        _git(path, "init", "-q", "-b", "main")
        _git(path, "config", "user.email", "test@example.com")
        _git(path, "config", "user.name", "test")

    def write(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            p = self.path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    def commit(self, message: str, files: dict[str, str] | None = None) -> None:
        if files:
            self.write(files)
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "-m", message)

    def branch(self, name: str) -> None:
        _git(self.path, "checkout", "-q", "-b", name)

    def checkout(self, name: str) -> None:
        _git(self.path, "checkout", "-q", name)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Never let a developer's real .env (and Bob API key) leak into tests."""
    monkeypatch.setenv("BLASTRADIUS_NO_DOTENV", "1")


@pytest.fixture
def tmp_repo(tmp_path: Path) -> TmpRepo:
    return TmpRepo(tmp_path)
