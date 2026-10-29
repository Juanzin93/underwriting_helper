"""Form parsing and validation helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, TypeVar

from fastapi import Request
from pydantic import BaseModel, ValidationError

from uwhelper.repo.cases import ValidationFailure

M = TypeVar("M", bound=BaseModel)
CHECKBOXES = {"is_guarantor", "to_be_refinanced", "mark_verified", "is_secured", "is_synthetic", "confirmed",
              "include_narrative"}


@asynccontextmanager
async def read_form(request: Request) -> AsyncIterator[Any]:
    """Parse the form and always close it, so uploaded temporary files are released promptly."""
    form = await request.form()
    try:
        yield form
    finally:
        await form.close()


async def form_dict(request: Request, prefix: str | None = None) -> dict[str, Any]:
    async with read_form(request) as form:
        items = list(form.multi_items())
    data: dict[str, Any] = {}
    for key, value in items:
        if key == "csrf_token" or not isinstance(value, str):
            continue
        if prefix:
            if not key.startswith(prefix):
                continue
            key = key[len(prefix):]
        data[key] = value
    return data


def checkbox(data: dict[str, Any], *names: str) -> dict[str, Any]:
    for n in names:
        data[n] = data.get(n) in ("on", "true", "1", "yes")
    return data


def validate(model: type[M], data: dict[str, Any]) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        parts = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()) if x != "__root__")
            msg = str(err.get("msg", "invalid")).replace("Value error, ", "")
            parts.append(f"{loc}: {msg}" if loc else msg)
        raise ValidationFailure("; ".join(parts)) from None
