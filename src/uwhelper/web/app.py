"""FastAPI application factory and security middleware."""

from __future__ import annotations

import hmac
import logging
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from uwhelper import __version__
from uwhelper.bootstrap import initialise
from uwhelper.config import Settings, load_settings
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

    async def dispatch(self, request: Request, call_next):
        raw_host = request.headers.get("host") or ""
        host = raw_host[: raw_host.find("]") + 1] if raw_host.startswith("[") else raw_host.split(":")[0]
        settings: Settings = request.app.state.settings
        if host not in ALLOWED_HOSTS and not settings.allow_non_loopback_bind:
            return PlainTextResponse("Invalid Host header", status_code=400)
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                o_host = urlparse(origin).hostname or ""
                if origin == "null" or (o_host not in ALLOWED_HOSTS and f"[{o_host}]" not in ALLOWED_HOSTS):
                    return PlainTextResponse("Cross-origin request refused", status_code=403)
            # Multipart uploads carry the token in the query string; urlencoded forms in the body.
            token = request.headers.get("x-csrf-token") or request.query_params.get("csrf_token")
            if token is None and "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
                body = await request.body()
                token = (parse_qs(body.decode("utf-8", "replace")).get("csrf_token") or [None])[0]
            if not token or not hmac.compare_digest(str(token), request.app.state.csrf_token):
                return PlainTextResponse("Missing or invalid CSRF token. Reload the page and try again.", status_code=403)
        response = await call_next(request)
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db, report = initialise(settings)
    app = FastAPI(title="UW Helper", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.policy_errors = report.errors
    app.add_middleware(SecurityMiddleware)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    from uwhelper.web.routes import analysis, cases, documents, facts, reports, system

    for module in (cases, documents, facts, analysis, reports, system):
        app.include_router(module.router)

    from uwhelper.repo.cases import NotFound, ValidationFailure

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        return render(request, "error.html", 404, title="Not found", message=str(exc))

    @app.exception_handler(ValidationFailure)
    async def _invalid(request: Request, exc: ValidationFailure):
        return render(request, "error.html", 400, title="Invalid input", message=str(exc))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # Log the exception type only; never echo document contents or values to the page.
        log.error("Unhandled error on %s %s: %s", request.method, request.url.path, exc.__class__.__name__, exc_info=exc)
        return render(request, "error.html", 500, title="Unexpected error",
                      message="Something went wrong. Details were written to the local log only.")

    return app


def get_db(request: Request) -> Database:
    return request.app.state.db
