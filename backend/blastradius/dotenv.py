"""Minimal .env loader (no dependency). Existing environment variables win.

Tolerates the formats Windows users end up with: UTF-16 files (PowerShell `>`),
BOMs, `export NAME=v`, `set NAME=v`, `$env:NAME="v"`, and `NAME: v`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_LINE_RE = re.compile(r"^(?:export\s+|set\s+|\$env:)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(?P<value>.*)$")


def _read(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if len(raw) > 1 and raw[1:2] == b"\x00":  # UTF-16 LE without BOM
        return raw.decode("utf-16-le")
    return raw.decode("utf-8-sig", errors="replace")


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    m = _LINE_RE.match(line)
    if not m:
        return None
    value = m.group("value").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return m.group("key"), value


def load_dotenv(path: Path) -> list[str]:
    """Load KEY=VALUE lines into os.environ without overriding. Returns the names loaded."""
    if not path.is_file():
        return []
    loaded = []
    for line in _read(path).splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if value and not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded


def diagnose(paths: list[Path], name: str) -> str:
    """Explain why `name` wasn't loaded — without ever revealing a value."""
    notes = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = _read(path)
        except OSError as e:
            notes.append(f"{path}: can't read ({e})")
            continue
        mentions = [line for line in text.splitlines() if name in line]
        if not mentions:
            notes.append(f"{path}: no line mentions {name} (is the file saved?)")
            continue
        for line in mentions:
            parsed = _parse_line(line)
            if line.strip().startswith("#"):
                notes.append(f"{path}: the {name} line is commented out")
            elif parsed is None or parsed[0] != name:
                notes.append(f"{path}: a line mentions {name} but isn't in NAME=value form")
            elif not parsed[1]:
                notes.append(f"{path}: {name} is present but its value is empty (is the file saved?)")
            else:
                notes.append(f"{path}: {name} is present with a value")
    return "; ".join(dict.fromkeys(notes)) or f"no .env file found in {', '.join(str(p.parent) for p in paths)}"
