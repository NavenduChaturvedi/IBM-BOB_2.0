"""LLM backends for the runbook step. Bob is called over its OpenAI-compatible HTTP API.

Config (env vars or .env; see .env.example):
  BOB_API_KEY       required (IBM_BOB_API_KEY also accepted)
  BOB_API_URL       default https://api.us-east.bob.ibm.com/inference/v1
  BOB_MODEL         default "premium"
  BOB_INSTANCE_ID   sent as x-instance-id (required by Bob 2.x accounts)
  BOB_TEAM_ID       sent as x-team-id (optional)
  BOB_AUTH_SCHEME   "Apikey" (default) or "Bearer"
  BOB_TIMEOUT_SECONDS  default 60
  BOB_USER_AGENT    default "blastradius/0.1 (IBM Bob hackathon)"

API shape per the pi-bob adapter (github.com/songlining/pi-bob): POST
{base}/chat/completions with `Authorization: Apikey <key>`; GET {base}/model/info
lists available models.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol

DEFAULT_URL = "https://api.us-east.bob.ibm.com/inference/v1"
DEFAULT_MODEL = "premium"
# The API sits behind Cloudflare, which blocks generic client user agents (Python-urllib,
# curl, browsers) with an HTML 403 page. An agent that identifies this tool as a Bob client passes.
DEFAULT_USER_AGENT = "blastradius/0.1 (IBM Bob hackathon)"


class LLMError(RuntimeError):
    pass


class LLMBackend(Protocol):
    name: str

    def generate(self, prompt: str) -> str: ...


def _env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return default


class BobBackend:
    name = "bob"

    def __init__(self, url: str, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 60,
                 instance_id: str | None = None, team_id: str | None = None, auth_scheme: str = "Apikey",
                 system_prompt: str | None = None):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.instance_id = instance_id
        self.team_id = team_id
        self.auth_scheme = auth_scheme
        self.system_prompt = system_prompt

    @classmethod
    def from_env(cls) -> "BobBackend":
        key = _env("BOB_API_KEY", "IBM_BOB_API_KEY", "IBM_BOB_KEY")
        if not key:
            raise LLMError("BOB_API_KEY is not set (in the environment or .env); pass --no-llm to skip Bob")
        return cls(
            url=_env("BOB_API_URL", "IBM_BOB_BASE_URL", default=DEFAULT_URL),
            api_key=key,
            model=_env("BOB_MODEL", "IBM_BOB_MODEL", default=DEFAULT_MODEL),
            timeout=float(_env("BOB_TIMEOUT_SECONDS", default="60")),
            instance_id=_env("BOB_INSTANCE_ID", "IBM_BOB_INSTANCE_ID"),
            team_id=_env("BOB_TEAM_ID", "IBM_BOB_TEAM_ID"),
            auth_scheme=_env("BOB_AUTH_SCHEME", "IBM_BOB_AUTH_SCHEME", default="Apikey"),
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"{self.auth_scheme} {self.api_key}",
            "User-Agent": _env("BOB_USER_AGENT", default=DEFAULT_USER_AGENT),
        }
        if self.instance_id:
            headers["x-instance-id"] = self.instance_id
        if self.team_id:
            headers["x-team-id"] = self.team_id
        return headers

    def _request(self, path: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{self.url}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers=self._headers(),
            method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if "cloudflare" in body.lower() and "<html" in body.lower():
                raise LLMError(f"Bob API {path} blocked by Cloudflare (HTTP {e.code}); "
                               f"try a different BOB_USER_AGENT") from e
            body = body[:300]
            hint = ""
            if e.code == 400 and not self.instance_id:
                hint = " (Bob 2.x accounts need BOB_INSTANCE_ID)"
            elif e.code in (401, 403):
                hint = " (check BOB_API_KEY / BOB_AUTH_SCHEME)"
            raise LLMError(f"Bob API {path} returned HTTP {e.code}{hint}: {body}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise LLMError(f"Bob request to {path} failed: {e}") from e

    def _build_payload(self, prompt: str) -> dict:
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})
        return {"model": self.model, "messages": messages, "temperature": 0.2}

    @staticmethod
    def _extract_text(body: dict) -> str:
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"Unrecognized Bob response shape: keys={list(body)}") from None
        if isinstance(content, list):  # content-parts style
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            raise LLMError("Bob returned an empty response")
        return content

    def generate(self, prompt: str) -> str:
        return self._extract_text(self._request("/chat/completions", self._build_payload(prompt)))

    def list_models(self) -> list[str]:
        body = self._request("/model/info")
        entries = body.get("data", body) if isinstance(body, dict) else body
        names = []
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict) and e.get("exposed", True) is not False:
                names.append(str(e.get("model_name") or e.get("id") or e.get("name") or e))
        return names


class FileBackend:
    """Reads a pre-generated runbook from disk. Useful for tests and offline demo rehearsal."""
    name = "file"

    def __init__(self, path: str):
        self.path = path

    def generate(self, prompt: str) -> str:
        try:
            with open(self.path, encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            raise LLMError(f"Could not read {self.path}: {e}") from e
