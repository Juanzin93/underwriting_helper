"""Minimal client for a LOCAL Ollama server.

Local-only safeguards:
* The base URL must be http:// on a loopback address (validated on construction).
* ``trust_env=False``: HTTP(S)_PROXY environment variables are ignored, so
  requests cannot be routed through a proxy to another machine.
* Redirects are not followed.
* Models that Ollama reports as remote/cloud (``remote_host``/``remote_model``
  metadata, or ``:cloud`` / ``-cloud`` tags) are refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from uwhelper.config import validate_ollama_url


class OllamaError(RuntimeError):
    code = "error"


class OllamaUnavailable(OllamaError):
    code = "unavailable"


class OllamaTimeout(OllamaError):
    code = "timeout"


class OllamaModelMissing(OllamaError):
    code = "model_missing"


class OllamaRemoteModelRefused(OllamaError):
    code = "remote_model_refused"


class OllamaBadResponse(OllamaError):
    code = "bad_response"


@dataclass
class ModelInfo:
    name: str
    digest: str | None
    size_bytes: int | None
    parameter_size: str | None
    quantization: str | None
    family: str | None


def is_cloud_model_name(name: str) -> bool:
    n = name.lower()
    return n.endswith(":cloud") or n.endswith("-cloud") or ":cloud-" in n or "-cloud:" in n


class OllamaClient:
    def __init__(self, base_url: str, model: str, *, timeout: float = 180.0, connect_timeout: float = 5.0,
                 num_ctx: int = 8192, transport: httpx.BaseTransport | None = None):
        self.base_url = validate_ollama_url(base_url)
        self.model = model
        self.num_ctx = num_ctx
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
        try:
            resp = self._client.request(method, path, json=payload)
        except httpx.TimeoutException as exc:
            raise OllamaTimeout("The local model did not respond within the configured timeout.") from exc
        except httpx.TransportError as exc:
            raise OllamaUnavailable(
                f"Cannot reach Ollama at {self.base_url}. Is it running? ('ollama serve' or the Ollama app)") from exc
        if resp.status_code == 404:
            raise OllamaModelMissing(f"Model {self.model!r} is not installed locally. Run: ollama pull {self.model}")
        if 300 <= resp.status_code < 400:
            raise OllamaBadResponse("Ollama returned a redirect; refusing to follow it.")
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str(resp.json().get("error", ""))[:300]
            except ValueError:
                pass
            raise OllamaBadResponse(f"Ollama returned HTTP {resp.status_code}. {detail}".strip())
        try:
            data = resp.json()
        except ValueError as exc:
            raise OllamaBadResponse("Ollama returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise OllamaBadResponse("Unexpected response shape from Ollama.")
        return data

    # ------------------------------------------------------------------ api
    def version(self) -> str:
        return str(self._request("GET", "/api/version").get("version", "unknown"))

    def list_models(self) -> list[dict[str, Any]]:
        models = self._request("GET", "/api/tags").get("models") or []
        return [m for m in models if isinstance(m, dict)]

    def ensure_local_model(self) -> ModelInfo:
        if is_cloud_model_name(self.model):
            raise OllamaRemoteModelRefused(f"{self.model!r} is a cloud model; only local models are allowed.")
        models = self.list_models()
        entry = next((m for m in models if m.get("name") == self.model or m.get("model") == self.model), None)
        if entry is None and ":" not in self.model:
            entry = next((m for m in models if m.get("name") == f"{self.model}:latest"), None)
        if entry is None:
            raise OllamaModelMissing(f"Model {self.model!r} is not installed locally. Run: ollama pull {self.model}")
        if entry.get("remote_host") or entry.get("remote_model") or is_cloud_model_name(str(entry.get("name", ""))):
            raise OllamaRemoteModelRefused(f"{self.model!r} is served remotely; only local models are allowed.")
        show = self._request("POST", "/api/show", {"model": self.model})
        if show.get("remote_host") or show.get("remote_model"):
            raise OllamaRemoteModelRefused(f"{self.model!r} is served remotely; only local models are allowed.")
        details = entry.get("details") or show.get("details") or {}
        return ModelInfo(
            name=str(entry.get("name") or self.model), digest=entry.get("digest"), size_bytes=entry.get("size"),
            parameter_size=details.get("parameter_size"), quantization=details.get("quantization_level"),
            family=details.get("family"),
        )

    def chat_json(self, system: str, user: str, schema: dict[str, Any], *, seed: int = 0) -> str:
        """Run one non-streaming chat turn constrained to a JSON schema; returns the raw content string."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "think": False,
            "keep_alive": "10m",
            "options": {"temperature": 0, "seed": seed, "num_ctx": self.num_ctx},
        }
        try:
            data = self._request("POST", "/api/chat", payload)
        except OllamaBadResponse as exc:
            if "think" in str(exc).lower():  # older servers / models without a thinking switch
                payload.pop("think", None)
                data = self._request("POST", "/api/chat", payload)
            else:
                raise
        msg = data.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), str):
            raise OllamaBadResponse("Ollama response has no message content.")
        return msg["content"]

    def chat_text(self, system: str, user: str, *, seed: int = 0) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False, "think": False, "keep_alive": "10m",
            "options": {"temperature": 0.1, "seed": seed, "num_ctx": self.num_ctx},
        }
        try:
            data = self._request("POST", "/api/chat", payload)
        except OllamaBadResponse as exc:
            if "think" in str(exc).lower():
                payload.pop("think", None)
                data = self._request("POST", "/api/chat", payload)
            else:
                raise
        msg = data.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, str):
            raise OllamaBadResponse("Ollama response has no message content.")
        return content


def client_from_settings(settings, transport: httpx.BaseTransport | None = None) -> OllamaClient:
    return OllamaClient(settings.ollama_url, settings.ollama_model, timeout=settings.ollama_timeout_seconds,
                        connect_timeout=settings.ollama_connect_timeout_seconds, num_ctx=settings.ollama_num_ctx,
                        transport=transport)


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse model output into a JSON object, tolerating code fences / leading prose."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise OllamaBadResponse("The model returned malformed JSON.") from None
        try:
            obj = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise OllamaBadResponse("The model returned malformed JSON.") from exc
    if not isinstance(obj, dict):
        raise OllamaBadResponse("The model returned JSON that is not an object.")
    return obj
