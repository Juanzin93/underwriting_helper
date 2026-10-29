"""Safe file intake: type allow-list, magic-byte check, size limit, SHA-256.

Stored files are named by their hash (never by the user-supplied name), so
path traversal and name collisions are impossible. File content is never
executed or rendered as HTML.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".txt"}
_MAGIC = {
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
}
CHUNK = 1024 * 1024


class UploadRejected(ValueError):
    """User-facing, content-free reason for refusing a file."""


@dataclass
class StagedFile:
    path: Path
    sha256: str
    size: int
    ext: str
    display_name: str


def safe_display_name(name: str | None) -> str:
    name = (name or "document").replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", name).strip(" .") or "document"
    return name[:180]


def _check_magic(ext: str, head: bytes) -> None:
    if ext == ".txt":
        if b"\x00" in head:
            raise UploadRejected("Text file contains binary data.")
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            try:
                head[:-4].decode("utf-8")  # multibyte char cut at buffer edge
            except UnicodeDecodeError as exc:
                raise UploadRejected("Text files must be UTF-8.") from exc
        return
    if not any(head.startswith(m) for m in _MAGIC[ext]):
        # PDFs may have a little junk before %PDF- (allowed by spec within 1 KB).
        if ext == ".pdf" and b"%PDF-" in head[:1024]:
            return
        raise UploadRejected(f"File content does not match its {ext} extension.")


def stage_upload(stream: BinaryIO, filename: str | None, staging_dir: Path, max_bytes: int) -> StagedFile:
    display = safe_display_name(filename)
    ext = Path(display).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadRejected(f"Unsupported file type {ext or '(none)'}. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}.")
    staging_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=staging_dir, suffix=".part")
    h, size, head = hashlib.sha256(), 0, b""
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UploadRejected(f"File exceeds the {max_bytes // (1024 * 1024)} MB limit.")
                if len(head) < 2048:
                    head += chunk[: 2048 - len(head)]
                h.update(chunk)
                out.write(chunk)
        if size == 0:
            raise UploadRejected("File is empty.")
        _check_magic(ext, head)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return StagedFile(Path(tmp), h.hexdigest(), size, ".jpg" if ext == ".jpeg" else (".tif" if ext == ".tiff" else ext), display)


def stored_name(sha256: str, ext: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("invalid hash")
    return f"{sha256[:2]}/{sha256}{ext}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()
