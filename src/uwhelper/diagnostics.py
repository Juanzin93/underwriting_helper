"""Local-only / environment diagnostics used by the System page and ``uwhelper doctor``."""

from __future__ import annotations

from typing import Any

from uwhelper.config import Settings, is_loopback_host
from uwhelper.documents.ocr import resolve_tesseract, tesseract_version
from uwhelper.llm.client import OllamaError, client_from_settings


def run_checks(settings: Settings, check_ollama: bool = True) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    add("App bind address is loopback", is_loopback_host(settings.host),
        f"host={settings.host} port={settings.port}")
    add("LLM endpoint is loopback", True, settings.ollama_url)
    add("No telemetry / analytics code", True,
        "The app has no telemetry, analytics or remote logging. Uvicorn access logs stay on this machine.")
    add("Data directory", settings.data_dir.is_dir(), str(settings.data_dir))
    if check_ollama:
        try:
            with client_from_settings(settings) as client:
                version = client.version()
                info = client.ensure_local_model()
            add("Ollama reachable on loopback", True, f"Ollama {version} at {settings.ollama_url}")
            add("Configured model installed locally (not cloud)", True,
                f"{info.name} {info.parameter_size or ''} {info.quantization or ''} digest {str(info.digest)[:12]}")
        except OllamaError as exc:
            add("Ollama / local model", False, f"{exc.code}: {exc}")
    exe = resolve_tesseract(settings.tesseract_cmd)
    add("Tesseract OCR available", bool(exe), tesseract_version(settings.tesseract_cmd) or
        "Not found — scanned PDFs cannot be read (text PDFs still work).")
    return checks
