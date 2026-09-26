# backend

Python package + CLI for **blastradius**.

## Quickstart

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"   # Windows
# .venv/bin/python -m pip install -e ".[dev]"     # macOS / Linux

python backend/demo/seed_demo.py --force           # build the demo repo
python backend/main.py --repo demo_repo --diff main...pr3/discount-tier --no-llm
```

## Run the web dashboard

```bash
python backend/main.py --repo demo_repo --serve
```

## Run tests

```bash
cd backend
python -m pytest
```

## Layout

```
backend/
  blastradius/          # Python package
    cli.py              # entry point, orchestration, exit codes
    gitio.py            # git plumbing
    diff_parser.py      # AST diff of changed symbols
    import_graph.py     # repo-wide import graph
    callers.py          # alias-aware call-site search
    blast_radius.py     # hop-1 / hop-2 orchestration
    artifacts.py        # migrations, k8s, compose, env vars, flags
    checklist.py        # pre-flight rule registry
    report.py           # markdown / JSON output
    terminal.py         # rich terminal view
    viewmodel.py        # JSON view model for the web dashboard
    runbook/            # template, Bob prompt + backends, validator
    web/                # HTTP server (serves ../frontend/index.html)
  demo/
    seed_demo.py        # builds demo_repo/ with 3 example PR branches
    present.py          # stage runner for live demos
    bob_samples/        # hand-written good / bad Bob drafts
  tests/                # pytest suite (146 tests)
  main.py               # entry shim: python backend/main.py --diff ...
  pyproject.toml
```
