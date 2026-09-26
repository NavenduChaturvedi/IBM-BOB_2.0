import io
import json
import urllib.error

import pytest

from blastradius.dotenv import load_dotenv
from blastradius.runbook.backends import DEFAULT_MODEL, DEFAULT_URL, BobBackend, LLMError


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req)
        if req.full_url.endswith("/model/info"):
            return _Resp(json.dumps({"data": [{"model_name": "premium"}, {"model_name": "hidden", "exposed": False}]}).encode())
        return _Resp(json.dumps({"choices": [{"message": {"content": "**Roll back if:**"}}]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


@pytest.fixture
def bob_env(monkeypatch):
    for k in ("BOB_API_URL", "BOB_MODEL", "BOB_INSTANCE_ID", "BOB_TEAM_ID", "BOB_AUTH_SCHEME",
              "IBM_BOB_API_KEY", "IBM_BOB_BASE_URL", "IBM_BOB_MODEL", "IBM_BOB_INSTANCE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BOB_API_KEY", "test-key")


def test_defaults_and_request_shape(bob_env, captured, monkeypatch):
    monkeypatch.setenv("BOB_INSTANCE_ID", "inst-1")
    bob = BobBackend.from_env()
    assert (bob.url, bob.model) == (DEFAULT_URL, DEFAULT_MODEL)

    assert bob.generate("hello") == "**Roll back if:**"
    req = captured[0]
    assert req.full_url == f"{DEFAULT_URL}/chat/completions"
    assert req.get_header("Authorization") == "Apikey test-key"
    assert req.get_header("X-instance-id") == "inst-1"
    body = json.loads(req.data)
    assert body["model"] == "premium" and body["messages"][-1] == {"role": "user", "content": "hello"}


def test_list_models_skips_unexposed(bob_env, captured):
    assert BobBackend.from_env().list_models() == ["premium"]


def test_missing_key(monkeypatch):
    for k in ("BOB_API_KEY", "IBM_BOB_API_KEY", "IBM_BOB_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(LLMError, match="BOB_API_KEY"):
        BobBackend.from_env()


def test_http_error_hint(bob_env, monkeypatch):
    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b"missing instance"))

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(LLMError, match="BOB_INSTANCE_ID"):
        BobBackend.from_env().generate("x")


def test_content_parts_response():
    body = {"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}]}
    assert BobBackend._extract_text(body) == "ab"


def test_load_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("ALREADY", "shell")
    monkeypatch.delenv("NEW_ONE", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    monkeypatch.delenv("EMPTY", raising=False)
    env = tmp_path / ".env"
    env.write_text("# c\nNEW_ONE=1  # note\nexport QUOTED='a b'\nALREADY=file\nEMPTY=\n", encoding="utf-8")
    assert sorted(load_dotenv(env)) == ["NEW_ONE", "QUOTED"]
    import os
    assert (os.environ["NEW_ONE"], os.environ["QUOTED"], os.environ["ALREADY"]) == ("1", "a b", "shell")
    assert "EMPTY" not in os.environ


@pytest.mark.parametrize("content,encoding", [
    ("$env:WIN_KEY=\"v1\"\n", "utf-8"),
    ("set WIN_KEY=v1\n", "utf-8"),
    ("WIN_KEY: v1\n", "utf-8"),
    ("WIN_KEY=v1\n", "utf-16"),       # PowerShell `>` output, with BOM
    ("\ufeffWIN_KEY=v1\n", "utf-8"),  # BOM
])
def test_load_dotenv_windows_formats(tmp_path, monkeypatch, content, encoding):
    import os
    monkeypatch.delenv("WIN_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_bytes(content.encode(encoding))
    load_dotenv(env)
    assert os.environ.get("WIN_KEY") == "v1"
    monkeypatch.delenv("WIN_KEY")


@pytest.mark.parametrize("content,expected", [
    ("OTHER=1\n", "no line mentions BOB_API_KEY"),
    ("# BOB_API_KEY=secret\n", "commented out"),
    ("BOB_API_KEY=\n", "value is empty"),
    ("BOB_API_KEY=secret\n", "present with a value"),
])
def test_diagnose_never_reveals_value(tmp_path, content, expected):
    from blastradius.dotenv import diagnose
    env = tmp_path / ".env"
    env.write_text(content, encoding="utf-8")
    message = diagnose([env], "BOB_API_KEY")
    assert expected in message and "secret" not in message
