import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from blastradius.cli import analyze, main
from blastradius.runbook.backends import FileBackend
from blastradius.viewmodel import build_view
from blastradius.web import Dashboard, _handler, export_html

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))
import seed_demo  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "demo" / "bob_samples"


@pytest.fixture(scope="module")
def demo_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("demo")
    seed_demo.seed(repo, force=True)
    return repo


def _view(repo, branch, backend=None):
    report, timings = analyze(repo, f"main...{branch}", backend=backend)
    return build_view(report, timings)


def test_view_pr3(demo_repo):
    v = _view(demo_repo, "pr3/discount-tier")
    assert v["stats"]["call_sites"] == 6 and v["stats"]["broken_calls"] == 2 and v["stats"]["high"] == 1
    assert v["risk"]["score"] == 25 + 8 + 2 * 2 + 5 * 2 and v["risk"]["level"] == "Elevated"
    root = v["tree"][0]
    assert root["symbol"] == "app.payments.pricing.calculate_discount" and root["breaks"] == 2
    verdicts = {(n["file"], n["line"]): n["verdict"] for n in root["callers"]}
    assert verdicts[("tests/test_pricing.py", 5)] == "breaks" and verdicts[("app/api/cart.py", 5)] == "fits"
    top = v["symbols"][0]
    assert (top["name"], top["callers"], top["breaks"]) == ("calculate_discount", 4, 2)
    assert v["by_file"][0]["calls"] == 2


def test_view_pr2_facts(demo_repo):
    v = _view(demo_repo, "pr2/refund-status")
    assert v["stats"]["call_sites"] == 0 and v["tree"] == []
    assert v["facts"]["migrations"][0]["revision"] == seed_demo.REFUND_REV
    assert v["facts"]["env_vars"] == [{"name": "REFUND_WEBHOOK_URL", "declared": False, "at_import": True}]
    assert v["risk"]["level"] == "High"


def test_runbook_html_escapes_untrusted_markup(demo_repo, tmp_path):
    draft = tmp_path / "draft.md"
    draft.write_text((SAMPLES / "pr3_valid.md").read_text(encoding="utf-8") + "\n<script>alert(1)</script>\n",
                     encoding="utf-8")
    v = _view(demo_repo, "pr3/discount-tier", FileBackend(str(draft)))
    assert v["runbook"]["status"] == "validated"
    assert "<script>" not in v["runbook"]["html"] and "&lt;script&gt;" in v["runbook"]["html"]


def test_export_embeds_data_safely(demo_repo):
    v = _view(demo_repo, "pr3/discount-tier")
    v["runbook"]["markdown"] += "</script><script>alert(1)</script>"
    html = export_html(v)
    assert '<script id="report-data" type="application/json">' in html
    data = html.split('<script id="report-data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(data)["range"] == "main...pr3/discount-tier"


def test_cli_html_format(demo_repo, tmp_path):
    out = tmp_path / "r.html"
    assert main(["--repo", str(demo_repo), "--diff", "main...pr1/pagination-fix", "--no-llm",
                 "--format", "html", "--out", str(out)]) == 0
    assert "report-data" in out.read_text(encoding="utf-8")


@pytest.fixture
def server(demo_repo, monkeypatch):
    monkeypatch.delenv("BOB_API_KEY", raising=False)
    monkeypatch.delenv("IBM_BOB_API_KEY", raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(Dashboard(demo_repo)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.status, r.headers.get("Content-Type"), r.read().decode("utf-8")


def _events(body):
    return [(b.split("\n")[0][7:], json.loads(b.split("\n")[1][6:])) for b in body.strip().split("\n\n")]


def test_server_meta_and_index(server):
    status, ctype, body = _get(server + "/")
    assert status == 200 and "text/html" in ctype and "blastradius" in body
    meta = json.loads(_get(server + "/api/meta")[2])
    assert meta["default_base"] == "main" and "pr3/discount-tier" in meta["branches"]
    assert meta["samples"] == ["pr3/discount-tier"]
    assert meta["bob"]["configured"] is False


def test_server_streams_stages_then_result(server):
    _, ctype, body = _get(server + "/api/analyze?base=main&head=pr3/discount-tier&mode=sample")
    events = _events(body)
    assert "text/event-stream" in ctype
    assert [e for e, _ in events][-1] == "result"
    assert any("Validating" in d["message"] for e, d in events if e == "stage")
    assert events[-1][1]["runbook"]["status"] == "rejected"
    assert "text/html" in _get(server + "/api/export")[1]


def test_server_bob_mode_without_key_falls_back(server):
    _, _, body = _get(server + "/api/analyze?base=main&head=pr1/pagination-fix&mode=bob")
    result = _events(body)[-1]
    assert result[0] == "result" and result[1]["runbook"]["status"] == "unavailable"


def test_server_bad_branch_reports_error(server):
    _, _, body = _get(server + "/api/analyze?base=main&head=nope&mode=template")
    assert _events(body)[-1][0] == "error"
