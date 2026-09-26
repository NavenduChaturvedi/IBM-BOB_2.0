import json
import sys
import threading
import urllib.error
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


def _get_status(url):
    """(status, parsed JSON body) for requests expected to be rejected."""
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize("query,status,kind", [
    ("base=main&head=nope&mode=template", 404, "unknown_branch"),
    ("base=main&head=main&mode=template", 400, "same_branch"),
    ("base=main&head=pr1/pagination-fix&mode=garbage", 400, "bad_mode"),
    ("base=&head=pr1/pagination-fix&mode=template", 400, "missing_branch"),
    ("base=main&head=pr1/pagination-fix&mode=sample", 400, "no_sample"),
    ("base=pr3/discount-tier&head=main&mode=template", 422, "empty_diff"),
])
def test_server_rejects_bad_requests_with_real_status(server, query, status, kind):
    code, body = _get_status(server + "/api/analyze?" + query)
    assert (code, body["error"]) == (status, kind)
    assert body["message"]


def test_reversed_range_suggests_swap(server):
    _, body = _get_status(server + "/api/analyze?base=pr3/discount-tier&head=main&mode=template")
    assert "swapped" in body["message"]


def test_api_sends_cors_headers(server):
    with urllib.request.urlopen(server + "/api/meta", timeout=30) as r:
        assert r.headers.get("Access-Control-Allow-Origin") == "*"
    with urllib.request.urlopen(server + "/api/analyze?base=main&head=pr1/pagination-fix&mode=template", timeout=30) as r:
        assert r.headers.get("Access-Control-Allow-Origin") == "*"


@pytest.mark.parametrize("head", ["--output=/tmp/x", "main;ls", "HEAD~1"])
def test_only_existing_branches_reach_git(server, head):
    from urllib.parse import quote
    code, body = _get_status(server + f"/api/analyze?base=main&head={quote(head)}&mode=template")
    assert (code, body["error"]) == (404, "unknown_branch")


def test_served_page_uses_same_origin_but_file_keeps_cloud_url(server):
    from blastradius.web import WEB_DIR
    served = _get(server + "/")[2]
    assert '<meta name="blastradius-api" content="">' in served
    assert '<meta name="blastradius-api" content="' in (WEB_DIR / "index.html").read_text(encoding="utf-8")


def _fake_bob(monkeypatch, reply_file):
    monkeypatch.setenv("BOB_API_KEY", "k")
    monkeypatch.setattr("blastradius.runbook.backends.BobBackend.generate",
                        lambda self, prompt: reply_file.read_text(encoding="utf-8"))


def test_live_bob_streams_partial_before_result(demo_repo, monkeypatch):
    _fake_bob(monkeypatch, SAMPLES / "pr3_valid.md")
    events = []
    view = Dashboard(demo_repo).analyze("main", "pr3/discount-tier", "bob",
                                        lambda m: events.append(("stage", m)),
                                        lambda v: events.append(("partial", v)))
    kinds = [k for k, _ in events]
    partial = next(v for k, v in events if k == "partial")
    assert kinds.index("partial") < max(i for i, k in enumerate(kinds) if k == "stage")  # partial before Bob stages
    assert partial["runbook_pending"] and partial["stats"]["broken_calls"] == 2
    assert view["runbook"]["status"] == "validated" and "Bob" in view["timings"]


def test_bob_requests_queue_instead_of_failing(demo_repo, monkeypatch):
    import blastradius.web as web
    _fake_bob(monkeypatch, SAMPLES / "pr3_valid.md")
    app = Dashboard(demo_repo)
    stages = []

    def on_stage(msg):
        stages.append(msg)
        if msg.startswith("Waiting"):  # the "other" analysis finishes while this one waits
            app._bob_lock.release()

    app._bob_lock.acquire()
    view = app.analyze("main", "pr3/discount-tier", "bob", on_stage)
    assert "Waiting for another Bob analysis to finish" in stages
    assert view["runbook"]["status"] == "validated"

    monkeypatch.setattr(web, "BOB_QUEUE_TIMEOUT", 0.2)
    app._bob_lock.acquire()
    try:
        with pytest.raises(web.RequestError) as err:
            app.analyze("main", "pr3/discount-tier", "bob", lambda _: None)
        assert err.value.kind == "bob_busy"
    finally:
        app._bob_lock.release()


def test_export_returns_the_requested_analysis(server):
    _get(server + "/api/analyze?base=main&head=pr3/discount-tier&mode=template")
    _get(server + "/api/analyze?base=main&head=pr1/pagination-fix&mode=template")  # someone else's later run
    _, ctype, html = _get(server + "/api/export?base=main&head=pr3/discount-tier&mode=template")
    assert "text/html" in ctype and '"range": "main...pr3/discount-tier"' in html
    code, body = _get_status(server + "/api/export?base=main&head=nope&mode=template")
    assert (code, body["error"]) == (404, "unknown_branch")
