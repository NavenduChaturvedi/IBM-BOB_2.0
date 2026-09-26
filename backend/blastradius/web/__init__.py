"""Local web dashboard: `python blastradius.py --serve`.

Stdlib only, bound to 127.0.0.1, works offline (no CDN assets).
  GET /                    the dashboard (index.html)
  GET /api/meta            repo, branches, Bob status, demo samples
  GET /api/analyze?base=&head=&mode=template|bob|sample
                           Invalid requests get a 4xx JSON {error, message} before any work starts.
                           Otherwise Server-Sent Events: `stage` while running; for live Bob, a
                           `partial` (deterministic result) before Bob starts; then `result` or `error`.
  GET /api/export          the last result as a self-contained HTML file

The /api routes send CORS headers, so the frontend can also be hosted on its own
(file://, GitHub Pages) and point at a deployed backend via its blastradius-api meta tag.
Only existing branch names are accepted, and live Bob analyses run one at a time (later
ones queue), because a public deployment spends the operator's Bob API key.
"""
from __future__ import annotations

import json
from collections import OrderedDict
import re
import sys
import threading
import time
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


_API_META_RE = re.compile(r'(<meta name="blastradius-api" content=")[^"]*(")')


def page_html() -> bytes:
    """index.html as served by this backend: API calls go to this same origin."""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return _API_META_RE.sub(r"\1\2", html).encode("utf-8")


def export_html(view: dict) -> str:
    """The dashboard as one file with the result embedded; opens anywhere, no server needed."""
    data = json.dumps(view).replace("</", "<\\/")
    tag = f'<script id="report-data" type="application/json">{data}</script>'
    return (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("<!--REPORT_DATA-->", tag)


MODES = ("template", "bob", "sample")
BOB_QUEUE_TIMEOUT = 120  # seconds a Bob request waits for the one running ahead of it
RECENT_RESULTS = 32  # results kept for per-request export


class RequestError(Exception):
    """A request the server won't run, with an HTTP status and a machine-readable kind for the UI."""

    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


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
        self.recent: OrderedDict[tuple[str, str, str], dict] = OrderedDict()  # per-request exports
        self._bob_lock = threading.Lock()

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

    def validate(self, base: str, head: str, mode: str) -> None:
        """Reject bad requests before any work starts, with a status code and a user-facing reason."""
        if mode not in MODES:
            raise RequestError(400, "bad_mode", f"Unknown mode {mode!r}; use one of: {', '.join(MODES)}.")
        if not base or not head:
            raise RequestError(400, "missing_branch", "Pick both a base and a head branch.")
        # only real branch names reach git: a value like "--output=x" would otherwise be read as an option
        branches = self.branches()
        for name in (base, head):
            if name not in branches:
                raise RequestError(404, "unknown_branch",
                                   f"Unknown branch {name!r}. Available: {', '.join(branches)}.")
        if base == head:
            raise RequestError(400, "same_branch", f"Base and head are both {base!r}; pick two different branches.")
        if mode == "sample" and not self.sample_for(head):
            demos = ", ".join(b for b in branches if self.sample_for(b)) or "none"
            raise RequestError(400, "no_sample", f"The rejection demo only has a canned Bob draft for: {demos}.")
        merge_base = gitio.merge_base(self.repo, base, head)
        if not gitio.commits_between(self.repo, merge_base, head):
            swapped = bool(gitio.commits_between(self.repo, merge_base, base))
            raise RequestError(422, "empty_diff",
                               f"{head!r} has no commits that aren't already in {base!r}, so there's nothing to analyze."
                               + (f" Did you mean {head}...{base}? Base and head look swapped." if swapped else ""))

    def analyze(self, base: str, head: str, mode: str, on_stage, on_partial=lambda view: None) -> dict:
        """Run an analysis. Call validate() first. For live Bob, the deterministic result is
        passed to on_partial before Bob starts, so the UI never waits on Bob for the facts."""
        from ..cli import analyze  # late import: cli imports this package for --serve
        from ..runbook import generate_runbook

        backend = None
        if mode == "bob":
            try:
                backend = BobBackend.from_env()
            except LLMError as e:
                backend = _Unavailable(str(e))
        elif mode == "sample":
            backend = FileBackend(str(self.sample_for(head)))

        if not isinstance(backend, BobBackend):  # template, sample, or Bob not configured: all instant
            report, timings = analyze(self.repo, f"{base}...{head}", backend=backend, on_stage=on_stage)
        else:
            report, timings = analyze(self.repo, f"{base}...{head}", backend=None, on_stage=on_stage)
            partial = build_view(report, timings)
            partial["mode"] = mode
            partial["runbook_pending"] = True
            on_partial(partial)

            if not self._bob_lock.acquire(blocking=False):  # queue behind the running Bob analysis
                on_stage("Waiting for another Bob analysis to finish")
                if not self._bob_lock.acquire(timeout=BOB_QUEUE_TIMEOUT):
                    raise RequestError(503, "bob_busy", "Bob is busy with other analyses; the deterministic "
                                       "runbook is shown. Try Bob again in a minute.")
            try:
                started = time.perf_counter()
                report.runbook = generate_runbook(report.diff, report.facts, report.checklist, report.blast,
                                                  backend, on_stage=on_stage)
                timings["Bob"] = time.perf_counter() - started
            finally:
                self._bob_lock.release()

        view = build_view(report, timings)
        view["mode"] = mode
        self.last_view = view
        self.recent[(base, head, mode)] = view
        self.recent.move_to_end((base, head, mode))
        while len(self.recent) > RECENT_RESULTS:
            self.recent.popitem(last=False)
        return view

    def export_view(self, base: str, head: str, mode: str) -> dict | None:
        """The result a visitor is looking at, not whatever ran last on this shared server.
        Deterministic modes are recomputed on a cache miss; a live Bob result must already exist."""
        if not base:
            return self.last_view
        if (base, head, mode) in self.recent:
            return self.recent[(base, head, mode)]
        if mode == "bob":
            return None
        self.validate(base, head, mode)
        return self.analyze(base, head, mode, lambda _: None)


def _handler(app: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
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
                    self._send(200, page_html(), "text/html; charset=utf-8")
                elif url.path == "/api/meta":
                    self._json(app.meta())
                elif url.path == "/api/analyze":
                    self._stream(q.get("base", ""), q.get("head", ""), q.get("mode", "template"))
                elif url.path == "/api/export":
                    try:
                        view = app.export_view(q.get("base", ""), q.get("head", ""), q.get("mode", "template"))
                    except RequestError as e:
                        return self._json({"error": e.kind, "message": e.message}, e.status)
                    if view is None:
                        self._json({"error": "not_found", "message": "Run this analysis first, then export it."}, 404)
                    else:
                        name = view["head"].replace("/", "-")
                        self._send(200, export_html(view).encode("utf-8"), "text/html; charset=utf-8",
                                   {"Content-Disposition": f'attachment; filename="blastradius-{name}.html"'})
                else:
                    self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _stream(self, base: str, head: str, mode: str):
            try:
                app.validate(base, head, mode)
            except RequestError as e:
                return self._json({"error": e.kind, "message": e.message}, e.status)
            except gitio.GitError as e:
                return self._json({"error": "git", "message": str(e)}, 500)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")  # stream through proxies instead of buffering
            self.send_header("Connection", "close")
            self.end_headers()

            def event(name: str, data) -> None:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
                self.wfile.flush()

            try:
                view = app.analyze(base, head, mode, lambda msg: event("stage", {"message": msg}),
                                   lambda partial: event("partial", partial))
                event("result", view)
            except RequestError as e:
                event("error", {"kind": e.kind, "message": e.message})
            except gitio.GitError as e:
                event("error", {"kind": "git", "message": str(e)})
            except Exception as e:  # surface anything else in the UI rather than a dead spinner
                event("error", {"kind": "internal", "message": f"{type(e).__name__}: {e}"})

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
