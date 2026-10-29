"""FastAPI application factory and security middleware."""

from __future__ import annotations

import hmac
import inspect
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from uwhelper import __version__
from uwhelper.bootstrap import initialise
from uwhelper.config import Settings, load_settings
from uwhelper.core.errors import (
    REQUEST_ID_HEADER,
    error_response,
    is_api,
    request_id_for,
    validation_details,
)
from uwhelper.db.connection import Database
from uwhelper.presentation import register_filters

log = logging.getLogger("uwhelper.web")
WEB_DIR = Path(__file__).parent
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "testserver"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; connect-src 'self'")

templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
register_filters(templates.env)


class SecurityMiddleware(BaseHTTPMiddleware):
    """Host allow-list (DNS-rebinding defence), Origin check, CSRF token and security headers."""

    @staticmethod
    def _refuse(request: Request, status: int, code: str, message: str):
        if is_api(request):
            return error_response(request, status, code, message)
        return PlainTextResponse(message, status_code=status)

    async def dispatch(self, request: Request, call_next):
        request_id_for(request)
        raw_host = request.headers.get("host") or ""
        host = raw_host[: raw_host.find("]") + 1] if raw_host.startswith("[") else raw_host.split(":")[0]
        settings: Settings = request.app.state.settings
        if host not in ALLOWED_HOSTS and not settings.allow_non_loopback_bind:
            return self._refuse(request, 400, "invalid_host", "Invalid Host header")
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                o_host = urlparse(origin).hostname or ""
                if origin == "null" or (o_host not in ALLOWED_HOSTS and f"[{o_host}]" not in ALLOWED_HOSTS):
                    return self._refuse(request, 403, "cross_origin_refused", "Cross-origin request refused")
            # Multipart uploads carry the token in the query string; urlencoded forms in the body.
            token = request.headers.get("x-csrf-token") or request.query_params.get("csrf_token")
            if token is None and "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
                body = await request.body()
                token = (parse_qs(body.decode("utf-8", "replace")).get("csrf_token") or [None])[0]
            if not token or not hmac.compare_digest(str(token), request.app.state.csrf_token):
                return self._refuse(request, 403, "csrf_failed",
                                    "Missing or invalid CSRF token. Reload the page (API: send X-CSRF-Token).")
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"  # "no-referrer" makes browsers send Origin: null
        response.headers["Cache-Control"] = "no-store"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response


def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    base = {
        "request": request,
        "csrf_token": request.app.state.csrf_token,
        "settings": request.app.state.settings,
        "app_version": __version__,
        "flash": request.query_params.get("msg"),
        "flash_level": request.query_params.get("level", "info"),
        "policy_errors": request.app.state.policy_errors,
    }
    base.update(ctx)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


def redirect(url: str, msg: str | None = None, level: str = "info") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + urlencode({"msg": msg[:300], "level": level})
    return RedirectResponse(url, status_code=303)


NO_TELEMETRY = {"tracing": False, "metrics": False, "logs": False, "operation_spans": False, "auto_configure": False}


def _fastapi_kwargs() -> dict[str, Any]:
    """Newer FastAPI versions ship native OpenTelemetry instrumentation; switch it off explicitly."""
    if "telemetry" in inspect.signature(FastAPI.__init__).parameters:
        return {"telemetry": dict(NO_TELEMETRY)}
    return {}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    # Belt and braces: no OpenTelemetry SDK may export anything from this process.
    os.environ["OTEL_SDK_DISABLED"] = "true"
    os.environ.pop("FASTAPI_OTEL_AUTO_CONFIGURE", None)
    db, report = initialise(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        try:
            db.shutdown()
            log.info("Database checkpointed on shutdown")
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("Shutdown checkpoint failed: %s", exc.__class__.__name__)

    app = FastAPI(title="UW Helper", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan, **_fastapi_kwargs())
    app.state.settings = settings
    app.state.db = db
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.policy_errors = report.errors
    app.add_middleware(SecurityMiddleware)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    from uwhelper.web.routes import (
        analysis,
        api_v1,
        cases,
        crm,
        documents,
        facts,
        health,
        lifecycle,
        reports,
        servicing,
        system,
    )

    for module in (health, api_v1, cases, documents, facts, analysis, reports, system, lifecycle, servicing, crm):
        app.include_router(module.router)

    from uwhelper.integrations import IntegrationDisabled
    from uwhelper.repo.cases import NotFound, ValidationFailure

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        if is_api(request):
            return error_response(request, 404, "not_found", str(exc))
        return render(request, "error.html", 404, title="Not found", message=str(exc))

    @app.exception_handler(ValidationFailure)
    async def _invalid(request: Request, exc: ValidationFailure):
        if is_api(request):
            return error_response(request, 422, "validation_error", str(exc))
        return render(request, "error.html", 400, title="Invalid input", message=str(exc))

    @app.exception_handler(IntegrationDisabled)
    async def _integration(request: Request, exc: IntegrationDisabled):
        if is_api(request):
            return error_response(request, 403, "integration_disabled", str(exc))
        return render(request, "error.html", 403, title="Integration disabled", message=str(exc))

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError):
        details = validation_details(exc.errors())
        if is_api(request):
            return error_response(request, 422, "validation_error", "Request validation failed.", details)
        return render(request, "error.html", 400, title="Invalid input",
                      message="; ".join(f"{d['field']}: {d['message']}" for d in details))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if is_api(request):
            return error_response(request, exc.status_code, f"http_{exc.status_code}", str(exc.detail))
        title = "Not found" if exc.status_code == 404 else "Request error"
        return render(request, "error.html", exc.status_code, title=title, message=str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # Log the exception type and request id only; never echo document contents or values.
        log.error("Unhandled error %s on %s %s: %s", request_id_for(request), request.method, request.url.path,
                  exc.__class__.__name__, exc_info=exc)
        if is_api(request):
            return error_response(request, 500, "internal_error",
                                  "Unexpected error. Details were written to the local log only.")
        return render(request, "error.html", 500, title="Unexpected error",
                      message="Something went wrong. Details were written to the local log only.")

    return app


def get_db(request: Request) -> Database:
    return request.app.state.db
