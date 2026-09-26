"""Local web dashboard: `python blastradius.py --serve`.

Stdlib only, bound to 127.0.0.1, works offline (no CDN assets).
  GET /                    the dashboard (index.html)
  GET /api/meta            repo, branches, Bob status, demo samples
  GET /api/analyze?base=&head=&mode=template|bob|sample
                           Server-Sent Events: `stage` events while running, then `result` (or `error`)
  GET /api/export          the last result as a self-contained HTML file
"""
from __future__ import annotations

import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import gitio
from ..runbook.backends import BobBackend, FileBackend, LLMError
from ..viewmodel import build_view

# backend/blastradius/web/  →  go up 4 levels to repo root, then into frontend/
_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent  # …/backend
PROJECT_ROOT = _BACKEND_ROOT.parent                            # repo root
WEB_DIR = PROJECT_ROOT / "frontend"
SAMPLES_DIR = _BACKEND_ROOT / "demo" / "bob_samples"


def export_html(view: dict) -> str:
    """The dashboard as one file with the result embedded; opens anywhere, no server needed."""
    data = json.dumps(view).replace("</", "<\\/")
    tag = f'<script id="report-data" type="application/json">{data}</script>'
    return (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("<!--REPORT_DATA-->", tag)


class _Unavailable:
    """Backend stand-in when Bob isn't configured: the runbook falls back and says why."""
    name = "bob"

    def __init__(self, reason: str):
        self.reason = reason

    def generate(self, prompt: str) -> str:
        raise LLMError(self.reason)


class Dashboard:
    def __init__(self, repo: Path):
        self.repo = repo
        self.last_view: dict | None = None

    def branches(self) -> list[str]:
        out = gitio.git(self.repo, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads")
        return [b for b in out.splitlines() if b]

    def sample_for(self, head: str) -> Path | None:
        path = SAMPLES_DIR / f"{head.split('/')[0]}_hallucinated.md"
        return path if path.is_file() else None

    def meta(self) -> dict:
        branches = self.branches()
        base = next((b for b in ("main", "master") if b in branches), branches[-1] if branches else "")
        heads = [b for b in branches if b != base]
        try:
            bob = BobBackend.from_env()
            bob_info = {"configured": True, "model": bob.model, "host": bob.url.split("/")[2]}
        except LLMError:
            bob_info = {"configured": False, "model": None, "host": None}
        return {
            "repo": self.repo.name,
            "branches": branches,
            "default_base": base,
            "default_head": heads[0] if heads else base,
            "samples": [h for h in heads if self.sample_for(h)],
            "bob": bob_info,
        }

    def analyze(self, base: str, head: str, mode: str, on_stage) -> dict:
        from ..cli import analyze  # late import: cli imports this package for --serve

        backend = None
        if mode == "bob":
            try:
                backend = BobBackend.from_env()
            except LLMError as e:
                backend = _Unavailable(str(e))
        elif mode == "sample" and (sample := self.sample_for(head)):
            backend = FileBackend(str(sample))
        report, timings = analyze(self.repo, f"{base}...{head}", backend=backend, on_stage=on_stage)
        view = build_view(report, timings)
        view["mode"] = mode
        self.last_view = view
        return view


def _handler(app: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200):
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/meta":
                    self._json(app.meta())
                elif url.path == "/api/analyze":
                    self._stream(q.get("base", ""), q.get("head", ""), q.get("mode", "template"))
                elif url.path == "/api/export":
                    if app.last_view is None:
                        self._json({"error": "run an analysis first"}, 404)
                    else:
                        name = app.last_view["head"].replace("/", "-")
                        self._send(200, export_html(app.last_view).encode("utf-8"), "text/html; charset=utf-8",
                                   {"Content-Disposition": f'attachment; filename="blastradius-{name}.html"'})
                else:
                    self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _stream(self, base: str, head: str, mode: str):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            def event(name: str, data) -> None:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
                self.wfile.flush()

            try:
                view = app.analyze(base, head, mode, lambda msg: event("stage", {"message": msg}))
                event("result", view)
            except gitio.GitError as e:
                event("error", {"message": str(e)})
            except Exception as e:  # surface anything else in the UI rather than a dead spinner
                event("error", {"message": f"{type(e).__name__}: {e}"})

    return Handler


def serve(repo: Path, port: int = 8765, open_browser: bool = True) -> int:
    import os
    port = int(os.environ.get("PORT", port))
    host = "0.0.0.0"  # must bind to all interfaces on Render / any cloud host
    app = Dashboard(repo)
    server = ThreadingHTTPServer((host, port), _handler(app))
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"blastradius dashboard for {repo} at {url}  (Ctrl+C to stop)", file=sys.stderr)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
