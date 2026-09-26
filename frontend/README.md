# frontend

Single-file web dashboard for **blastradius** — `index.html`.

No build step, no bundler, no external CDN. Everything is inline.

## How it is served

The backend HTTP server (`backend/blastradius/web/__init__.py`) reads this
file and serves it at `GET /`.  The JS inside talks to the backend over
Server-Sent Events (`/api/analyze`) and JSON (`/api/meta`).

## Static export

The backend can also embed the analysis result directly into `index.html` and
write a fully self-contained file that opens anywhere without a server:

```bash
python backend/main.py --repo demo_repo --diff main...pr3/discount-tier \
    --no-llm --format html --out report.html
```

## Pages

| Page | Description |
|---|---|
| Overview | Stats, bar chart, risk gauge, changed symbols, rollback targets |
| Blast radius | Full caller tree with hop-2 nesting and ✓/✕ verdicts |
| Checklist | Pre-flight items with severity and evidence |
| Runbook | Numbered steps, dark code blocks, copy buttons |
| PR 1 — Bugfix | Auto-run of `pr1/pagination-fix` demo scenario |
| PR 2 — Migration | Auto-run of `pr2/refund-status` demo scenario |
| PR 3 — Sig change | Auto-run of `pr3/discount-tier` demo scenario |
| Bob rejection | PR 3 with a hallucinated Bob draft — shows the validator |
| Error | Shown on any API or git failure |
