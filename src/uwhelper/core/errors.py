"""One error format for every JSON API response.

    {"error": {"code": "validation_error", "message": "...", "details": [...], "request_id": "..."}}

Messages never echo submitted values or document content (validation details report field
locations and rule messages only).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_RID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")


def request_id_for(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid:
        return rid
    incoming = request.headers.get(REQUEST_ID_HEADER, "")
    rid = incoming if _VALID_RID.match(incoming) else uuid.uuid4().hex
    request.state.request_id = rid
    return rid


def is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/") or request.url.path in ("/api", "/health")


def error_response(request: Request, status: int, code: str, message: str,
                   details: list[dict[str, Any]] | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    rid = request_id_for(request)
    body = {"error": {"code": code, "message": message, "details": details or [], "request_id": rid}}
    return JSONResponse(body, status_code=status, headers={REQUEST_ID_HEADER: rid, **(headers or {})})


def validation_details(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip submitted input values from Pydantic/FastAPI errors (they may contain sensitive data)."""
    out = []
    for e in errors:
        loc = [str(x) for x in e.get("loc", ()) if x not in ("body",)]
        out.append({"field": ".".join(loc) or None, "message": str(e.get("msg", "invalid")).replace("Value error, ", ""),
                    "type": e.get("type")})
    return out
