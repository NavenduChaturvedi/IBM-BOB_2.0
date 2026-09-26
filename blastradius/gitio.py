"""Thin wrappers around the git CLI."""
from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def parse_range(spec: str) -> tuple[str, str]:
    """Split 'main...feature/x' (or 'main..feature/x') into (base, head)."""
    for sep in ("...", ".."):
        if sep in spec:
            base, head = spec.split(sep, 1)
            return base or "HEAD", head or "HEAD"
    return spec, "HEAD"


def rev_parse(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", rev).strip()


def merge_base(repo: Path, base: str, head: str) -> str:
    return git(repo, "merge-base", base, head).strip()


def commits_between(repo: Path, base: str, head: str) -> list[str]:
    """Commits on head not on base, newest first."""
    out = git(repo, "rev-list", f"{base}..{head}")
    return [line for line in out.splitlines() if line]


def name_status(repo: Path, base: str, head: str) -> list[tuple[str, str, str | None]]:
    """Return (status, path, old_path) for each changed file."""
    out = git(repo, "diff", "--name-status", "-M", f"{base}...{head}")
    result = []
    for line in out.splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        if status == "R":
            result.append((status, parts[2], parts[1]))
        else:
            result.append((status, parts[1], None))
    return result


def file_diff(repo: Path, base: str, head: str, path: str) -> tuple[list[str], list[str]]:
    """Return (added_lines, removed_lines) for one file, without the +/- prefix."""
    out = git(repo, "diff", "-U0", f"{base}...{head}", "--", path)
    added, removed = [], []
    for line in out.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def show_file(repo: Path, rev: str, path: str) -> str | None:
    """File contents at a revision, or None if it doesn't exist there."""
    try:
        return git(repo, "show", f"{rev}:{path}")
    except GitError:
        return None


def list_files(repo: Path, rev: str) -> list[str]:
    out = git(repo, "ls-tree", "-r", "--name-only", "-z", rev)
    return [p for p in out.split("\0") if p]


def read_files(repo: Path, rev: str, paths: list[str]) -> dict[str, str]:
    """Read many files at a revision with one `git cat-file --batch` process."""
    if not paths:
        return {}
    request = "".join(f"{rev}:{p}\n" for p in paths).encode("utf-8")
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"], input=request, capture_output=True)
    if proc.returncode != 0:
        raise GitError(f"git cat-file failed: {proc.stderr.decode('utf-8', 'replace').strip()}")

    data, pos, result = proc.stdout, 0, {}
    for path in paths:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].split()
        pos = nl + 1
        if header[-1] == b"missing":
            continue
        size = int(header[2])
        result[path] = data[pos:pos + size].decode("utf-8", "replace")
        pos += size + 1
    return result


def repo_root(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip())
