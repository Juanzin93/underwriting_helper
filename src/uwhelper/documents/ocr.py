"""Local Tesseract OCR via subprocess (no network, no shell)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


class OCRUnavailable(RuntimeError):
    pass


class OCRFailed(RuntimeError):
    pass


_WIN_DEFAULTS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


def resolve_tesseract(cmd: str) -> str | None:
    if cmd and Path(cmd).is_file():
        return cmd
    found = shutil.which(cmd or "tesseract")
    if found:
        return found
    if sys.platform.startswith("win"):
        for candidate in _WIN_DEFAULTS:
            if Path(candidate).is_file():
                return candidate
    return None


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0


def tesseract_version(cmd: str) -> str | None:
    exe = resolve_tesseract(cmd)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, timeout=15, creationflags=_creationflags())
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or out.stderr).decode("utf-8", "replace").splitlines()
    return text[0].strip() if text else None


def ocr_image_bytes(png: bytes, cmd: str, lang: str = "eng", timeout: float = 120.0) -> str:
    exe = resolve_tesseract(cmd)
    if not exe:
        raise OCRUnavailable("Tesseract is not installed or not on PATH (set UWH_TESSERACT_CMD).")
    if not lang.replace("+", "").isalnum():
        raise OCRFailed("invalid OCR language setting")
    env = dict(os.environ)
    env.setdefault("OMP_THREAD_LIMIT", "2")
    try:
        proc = subprocess.run(
            [exe, "stdin", "stdout", "-l", lang, "--psm", "3"],
            input=png, capture_output=True, timeout=timeout, env=env, creationflags=_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise OCRFailed("OCR timed out") from exc
    except OSError as exc:
        raise OCRUnavailable(f"Could not start Tesseract: {exc.__class__.__name__}") from exc
    if proc.returncode != 0:
        raise OCRFailed(f"Tesseract exited with code {proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")
