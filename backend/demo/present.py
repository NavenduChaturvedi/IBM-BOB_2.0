"""Stage runner: plays the three demo PRs with title cards and pauses.

  python demo/present.py                 # live Bob, press Enter between scenarios
  python demo/present.py --no-llm        # deterministic runbooks only (no network)
  python demo/present.py --only pr3      # just the centerpiece
  python demo/present.py --rejection     # PR 3 with a hallucinated Bob draft, to show the validator
  python demo/present.py --svg docs      # export each rendered report to docs/<pr>.svg, no pauses

Stage-safe Bob: record live drafts once, review them, then replay exactly (no network):
  python demo/present.py --save-drafts demo/bob_drafts
  python demo/present.py --replay demo/bob_drafts

Rebuilds demo_repo/ first if it's missing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

from rich import box  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.text import Text  # noqa: E402

import seed_demo  # noqa: E402
from blastradius.cli import analyze, ensure_utf8, load_env, pick_backend, progress  # noqa: E402
from blastradius.terminal import render  # noqa: E402

SCENARIOS = {
    "pr1": ("pr1/pagination-fix", "A one-line bugfix",
            "Off-by-one fix in paginate(). A good tool stays quiet about small changes."),
    "pr2": ("pr2/refund-status", "A migration, a new env var, and a feature flag",
            "Adds a refund_status column, a webhook URL from the environment, and a flag.\n"
            "Watch the checklist catch a startup crash before it ships."),
    "pr3": ("pr3/discount-tier", "A breaking signature change",
            "calculate_discount(price, code) gains a required user_tier.\n"
            "Two callers were updated. One file was forgotten."),
}
REJECTION_DRAFT = ROOT / "demo" / "bob_samples" / "pr3_hallucinated.md"


def title_card(console: Console, key: str, backend_label: str) -> None:
    branch, title, story = SCENARIOS[key]
    body = Text.assemble(
        (title, "bold"), "\n\n", (story, ""), "\n\n",
        ("$ ", "dim"), (f"python blastradius.py --diff main...{branch}", "bold cyan"),
        (f"   # runbook: {backend_label}", "dim"),
    )
    console.print(Panel(body, title=f"[bold magenta]{key.upper()}[/]", title_align="left",
                        border_style="magenta", box=box.HEAVY, padding=(1, 3)))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--only", choices=list(SCENARIOS), action="append")
    p.add_argument("--no-llm", action="store_true", help="deterministic runbooks, no network")
    p.add_argument("--rejection", action="store_true", help="PR 3 with a hallucinated Bob draft")
    p.add_argument("--svg", type=Path, help="export rendered reports as SVG into this directory")
    p.add_argument("--save-drafts", type=Path, help="save each validated live Bob runbook to DIR/<pr>.md")
    p.add_argument("--replay", type=Path, help="replay saved Bob runbooks from DIR/<pr>.md instead of calling Bob")
    p.add_argument("--width", type=int, default=110)
    args = p.parse_args()

    ensure_utf8()
    load_env()
    repo = ROOT / "demo_repo"
    if not (repo / ".git").exists():
        seed_demo.seed(repo, force=True)

    keys = args.only or (["pr3"] if args.rejection else list(SCENARIOS))
    interactive = args.svg is None and sys.stdin.isatty()
    if args.svg:
        args.svg.mkdir(parents=True, exist_ok=True)

    for i, key in enumerate(keys):
        branch = SCENARIOS[key][0]
        if args.rejection:
            backend, label = pick_backend(bob_output=str(REJECTION_DRAFT)), "Bob draft (hallucinated sample)"
        elif args.replay and (args.replay / f"{key}.md").exists():
            backend, label = pick_backend(bob_output=str(args.replay / f"{key}.md")), "Bob (recorded), validated"
        else:
            backend = pick_backend(no_llm=args.no_llm)
            label = "deterministic template" if backend is None else "Bob, validated"

        console = Console(width=args.width, record=bool(args.svg))
        if interactive:
            console.clear()
        title_card(console, key, label)
        if interactive:
            console.input("[dim]Press Enter to run…[/]")

        with progress(interactive) as on_stage:
            report, timings = analyze(repo, f"main...{branch}", backend=backend, on_stage=on_stage)
        render(report, console, timings)

        if args.save_drafts and report.runbook.source == "bob (validated)":
            args.save_drafts.mkdir(parents=True, exist_ok=True)
            (args.save_drafts / f"{key}.md").write_text(report.runbook.markdown, encoding="utf-8")
            print(f"saved Bob draft to {args.save_drafts / f'{key}.md'}", file=sys.stderr)

        if args.svg:
            name = f"{key}-rejection" if args.rejection else key
            console.save_svg(str(args.svg / f"{name}.svg"), title=f"blastradius · main...{branch}")
            print(f"wrote {args.svg / f'{name}.svg'}", file=sys.stderr)
        elif interactive and i < len(keys) - 1:
            console.input("\n[dim]Press Enter for the next scenario…[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
