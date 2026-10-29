"""Health endpoints.

GET /health and /api/health
* 200 when the application and database work — even if optional components (Ollama, OCR) are unavailable;
  overall status is then "degraded".
* 503 when the database is unavailable.
* ``?checks=basic`` skips the Ollama probe (fast liveness check). The probe uses a short timeout and is cached
  for 30 seconds.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from uwhelper import __version__
from uwhelper.core.errors import REQUEST_ID_HEADER, request_id_for
from uwhelper.documents.ocr import resolve_tesseract
from uwhelper.integrations import status as integrations_status
from uwhelper.llm.client import OllamaClient, OllamaError

router = APIRouter()
CACHE_SECONDS = 30.0


def _ollama(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    cache = getattr(request.app.state, "ollama_health", None)
    if cache and time.monotonic() - cache[0] < CACHE_SECONDS:
        return cache[1]
    result: dict[str, Any] = {"optional": True, "url": settings.ollama_url, "model": settings.ollama_model}
    try:
        t = settings.ollama_health_timeout_seconds
        with OllamaClient(settings.ollama_url, settings.ollama_model, timeout=t, connect_timeout=t,
                          transport=getattr(request.app.state, "ollama_transport", None)) as client:
            info = client.ensure_local_model()
        result.update(status="available", model_digest=(info.digest or "")[:12], quantization=info.quantization)
    except OllamaError as exc:
        result.update(status=exc.code, detail=str(exc)[:200])
    request.app.state.ollama_health = (time.monotonic(), result)
    return result


@router.get("/health")
@router.get("/api/health")
def health(request: Request, checks: str = "full") -> JSONResponse:
    settings = request.app.state.settings
    components: dict[str, Any] = {"application": {"status": "ok", "version": __version__}}
    try:
        components["database"] = {"status": "ok", **request.app.state.db.check()}
    except Exception as exc:  # report the class only; never paths or data
        components["database"] = {"status": "unavailable", "detail": exc.__class__.__name__}
    components["ocr"] = {"status": "available" if resolve_tesseract(settings.tesseract_cmd) else "unavailable",
                         "optional": True}
    components["ollama"] = _ollama(request) if checks != "basic" else {"status": "not_checked", "optional": True}
    components["integrations"] = integrations_status(settings)
    db_ok = components["database"]["status"] == "ok"
    optional_ok = all(components[k]["status"] in ("available", "not_checked") for k in ("ocr", "ollama"))
    overall = "unavailable" if not db_ok else ("ok" if optional_ok else "degraded")
    body = {"status": overall, "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "bind": {"host": settings.host, "port": settings.port}, "components": components}
    return JSONResponse(body, status_code=200 if db_ok else 503, headers={REQUEST_ID_HEADER: request_id_for(request)})
