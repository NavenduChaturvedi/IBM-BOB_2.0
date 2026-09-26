"""CLI entry point: orchestrates the pipeline."""
from __future__ import annotations

import argparse
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from . import gitio
from .artifacts import scan_artifacts
from .blast_radius import compute_blast_radius
from .checklist import build_checklist
from .diff_parser import parse_diff
from .dotenv import diagnose, load_dotenv
from .models import Report, Severity
from .report import render_json, render_markdown
from .runbook import generate_runbook
from .runbook.backends import BobBackend, FileBackend, LLMBackend, LLMError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="blastradius",
        description="Trace a diff's blast radius and generate a pre-flight checklist and rollback runbook.",
    )
    p.add_argument("--diff", help="git range, e.g. main...feature/refund-fix")
    p.add_argument("--repo", default=".", help="path to the target repo (default: cwd)")
    p.add_argument("--out", help="write report to this file instead of stdout")
    p.add_argument("--format", choices=["md", "json", "html"], default="md",
                   help="html = the dashboard as one self-contained file")
    p.add_argument("--hops", type=int, choices=[1, 2], default=2, help="caller search depth")
    p.add_argument("--plain", action="store_true", help="print raw markdown even on a terminal")
    p.add_argument("--fail-on", choices=["high", "med", "low"],
                   help="exit with code 1 if any checklist item is at or above this severity (for CI)")
    llm = p.add_mutually_exclusive_group()
    llm.add_argument("--no-llm", action="store_true", help="skip Bob; use the deterministic runbook")
    llm.add_argument("--bob-output", help="use a saved Bob response file instead of calling the API")
    p.add_argument("--dump-prompt", help="write the Bob prompt to this file (works with --no-llm)")
    p.add_argument("--bob-check", action="store_true", help="check Bob API connectivity and list models, then exit")
    p.add_argument("--serve", action="store_true", help="open the web dashboard for --repo")
    p.add_argument("--port", type=int, default=8765, help="dashboard port (default 8765)")
    p.add_argument("--no-browser", action="store_true", help="with --serve: don't open a browser")
    return p


def ensure_utf8() -> None:
    """Windows consoles and redirected output default to cp1252, which can't encode → ✓ ✗."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def _env_files() -> list[Path]:
    return list(dict.fromkeys([Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]))


def load_env() -> None:
    """Load .env from the cwd and the project root, unless disabled (tests set BLASTRADIUS_NO_DOTENV)."""
    if os.environ.get("BLASTRADIUS_NO_DOTENV"):
        return
    for candidate in _env_files():
        load_dotenv(candidate)


def bob_check() -> int:
    try:
        bob = BobBackend.from_env()
        print(f"endpoint: {bob.url}  model: {bob.model}  auth: {bob.auth_scheme}  "
              f"instance id: {'set' if bob.instance_id else 'not set'}", file=sys.stderr)
        try:
            models = bob.list_models()
            print(f"model/info: {len(models)} models: {', '.join(models) or '(none listed)'}", file=sys.stderr)
        except LLMError as e:
            print(f"model/info: {e}", file=sys.stderr)
        reply = bob.generate("Reply with exactly: OK")
    except LLMError as e:
        print(f"Bob check FAILED: {e}", file=sys.stderr)
        if "BOB_API_KEY is not set" in str(e):
            print(f".env check: {diagnose(_env_files(), 'BOB_API_KEY')}", file=sys.stderr)
        return 1
    print(f"chat/completions OK: {reply.strip()[:80]!r}", file=sys.stderr)
    return 0


def pick_backend(no_llm: bool = False, bob_output: str | None = None) -> LLMBackend | None:
    if no_llm:
        return None
    if bob_output:
        return FileBackend(bob_output)
    try:
        return BobBackend.from_env()
    except LLMError as e:
        print(f"warning: {e}; falling back to template runbook", file=sys.stderr)
        return None


@contextmanager
def progress(enabled: bool) -> Iterator[Callable[[str], None]]:
    """Spinner on stderr naming the current stage; a no-op when stderr isn't a terminal."""
    if not enabled:
        yield lambda _: None
        return
    from rich.console import Console
    with Console(stderr=True).status("", spinner="dots") as status:
        yield lambda msg: status.update(f"[bold]{msg}…[/]")


def analyze(
    repo: Path,
    diff_range: str,
    hops: int = 2,
    backend: LLMBackend | None = None,
    dump_prompt: Path | None = None,
    on_stage: Callable[[str], None] = lambda _: None,
) -> tuple[Report, dict[str, float]]:
    """Run the whole pipeline. Returns the report and per-phase timings (seconds)."""
    started = time.perf_counter()
    base, head = gitio.parse_range(diff_range)
    on_stage("Parsing diff")
    diff = parse_diff(repo, base, head)
    on_stage("Tracing callers through the import graph")
    blast = compute_blast_radius(repo, diff, hops=hops)
    on_stage("Scanning deploy artifacts")
    facts = scan_artifacts(repo, diff)
    on_stage("Building pre-flight checklist")
    checklist = build_checklist(diff, blast, facts)
    timings = {"analysis": time.perf_counter() - started}

    llm_started = time.perf_counter()
    runbook = generate_runbook(diff, facts, checklist, blast, backend, dump_prompt=dump_prompt, on_stage=on_stage)
    if isinstance(backend, BobBackend):
        timings["Bob"] = time.perf_counter() - llm_started
    return Report(diff=diff, blast=blast, checklist=checklist, facts=facts, runbook=runbook), timings


def _fails(report: Report, threshold: str | None) -> bool:
    if threshold is None:
        return False
    rank = {Severity.HIGH: 0, Severity.MED: 1, Severity.LOW: 2}
    limit = rank[Severity(threshold)]
    return any(rank[i.severity] <= limit for i in report.checklist)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ensure_utf8()
    load_env()
    if args.bob_check:
        return bob_check()
    if args.serve:
        from .web import serve
        try:
            repo = gitio.repo_root(Path(args.repo).resolve())
        except gitio.GitError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return serve(repo, port=args.port, open_browser=not args.no_browser)
    if not args.diff:
        parser.error("--diff is required")

    rich_view = not args.out and args.format == "md" and not args.plain and sys.stdout.isatty()
    try:
        repo = gitio.repo_root(Path(args.repo).resolve())
        with progress(sys.stderr.isatty()) as on_stage:
            report, timings = analyze(
                repo, args.diff, hops=args.hops,
                backend=pick_backend(args.no_llm, args.bob_output),
                dump_prompt=Path(args.dump_prompt) if args.dump_prompt else None,
                on_stage=on_stage,
            )
    except gitio.GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if rich_view:
        from rich.console import Console
        from .terminal import render
        render(report, Console(), timings)
    else:
        if args.format == "html":
            from .viewmodel import build_view
            from .web import export_html
            output = export_html(build_view(report, timings))
        else:
            output = render_json(report) if args.format == "json" else render_markdown(report)
        if args.out:
            Path(args.out).write_text(output, encoding="utf-8")
            print(f"report written to {args.out}", file=sys.stderr)
        else:
            print(output)
        sys.stdout.flush()
        print("done in " + ", ".join(f"{k} {v:.1f}s" for k, v in timings.items()), file=sys.stderr)
    return 1 if _fails(report, args.fail_on) else 0


if __name__ == "__main__":
    sys.exit(main())
