"""Application settings.

Settings come from (lowest to highest precedence):
1. Built-in defaults.
2. ``uwhelper.toml`` in the data directory (optional).
3. Environment variables prefixed with ``UWH_``.

Network-related settings are validated so that the app cannot be pointed at
a non-loopback LLM endpoint or bound to a public interface by accident.
"""

from __future__ import annotations

import ipaddress
import os
import sys
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = Path(__file__).resolve().parent

LOOPBACK_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


class ConfigError(ValueError):
    """Raised when a configuration value is unsafe or invalid."""


def default_data_dir() -> Path:
    """Per-user data directory that is outside the source repository."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "UWHelper"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "uwhelper" if xdg else Path.home() / ".local" / "share" / "uwhelper"


def is_loopback_host(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if host in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_ollama_url(url: str) -> str:
    """Only allow plain-HTTP loopback Ollama endpoints."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http"}:
        raise ConfigError("Ollama URL must use http:// on a loopback address (e.g. http://127.0.0.1:11434).")
    if not parsed.hostname or not is_loopback_host(parsed.hostname):
        raise ConfigError(
            f"Refusing non-loopback Ollama host {parsed.hostname!r}. "
            "This application only performs local inference on 127.0.0.1."
        )
    if parsed.username or parsed.password:
        raise ConfigError("Ollama URL must not contain credentials.")
    return url.rstrip("/")


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = 8765
    allow_non_loopback_bind: bool = False

    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    ollama_timeout_seconds: float = 180.0
    ollama_connect_timeout_seconds: float = 5.0
    ollama_num_ctx: int = 8192
    ollama_retries: int = 1
    extraction_max_chars_per_chunk: int = 6000

    tesseract_cmd: str = "tesseract"
    tesseract_lang: str = "eng"
    ocr_dpi: int = 300
    ocr_timeout_seconds: float = 120.0
    ocr_min_text_chars: int = 25

    max_upload_mb: int = 50
    max_pages: int = 300
    policies_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "policies")

    # Optional explicit locations (default: inside data_dir).
    db_file: Path | None = None
    documents_path: Path | None = None
    db_busy_timeout_ms: int = 30000
    log_level: str = "INFO"
    ollama_health_timeout_seconds: float = 2.0

    # Optional external integrations: OFF unless both the master switch and the named integration are enabled.
    external_integrations_allowed: bool = False
    enabled_integrations: str = ""

    @property
    def db_path(self) -> Path:
        return self.db_file or (self.data_dir / "uwhelper.db")

    @property
    def documents_dir(self) -> Path:
        return self.documents_path or (self.data_dir / "documents")

    @property
    def integration_names(self) -> frozenset[str]:
        return frozenset(n.strip().lower() for n in self.enabled_integrations.split(",") if n.strip())

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def validate(self) -> Settings:
        validate_ollama_url(self.ollama_url)
        if not self.allow_non_loopback_bind and not is_loopback_host(self.host):
            raise ConfigError(
                f"Refusing to bind to {self.host!r}. The app binds to 127.0.0.1 by default; "
                "set UWH_ALLOW_NON_LOOPBACK_BIND=1 only if you fully understand the risk."
            )
        if not (1 <= self.port <= 65535):
            raise ConfigError("Port must be between 1 and 65535.")
        if self.max_upload_mb <= 0 or self.max_upload_mb > 500:
            raise ConfigError("max_upload_mb must be between 1 and 500.")
        if "cloud" in self.ollama_model.lower():
            raise ConfigError("Cloud-hosted Ollama models are not allowed; choose a locally downloaded model.")
        if not 1000 <= self.db_busy_timeout_ms <= 600000:
            raise ConfigError("db_busy_timeout_ms must be between 1000 and 600000.")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ConfigError("log_level must be DEBUG, INFO, WARNING or ERROR.")
        if self.integration_names and not self.external_integrations_allowed:
            raise ConfigError("enabled_integrations is set but external_integrations_allowed is false. "
                              "Enable both deliberately, or clear enabled_integrations.")
        static_dir = (PACKAGE_DIR / "web" / "static").resolve()
        for d in (self.data_dir, self.documents_dir, self.db_path.parent):
            r = Path(d).resolve()
            if r == static_dir or static_dir in r.parents:
                raise ConfigError("Data, database and document directories must not be inside the web static folder.")
        return self

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.documents_dir, self.reports_dir, self.db_path.parent):
            d.mkdir(parents=True, exist_ok=True)


_BOOL_TRUE = {"1", "true", "yes", "on"}


def _coerce(name: str, raw: object, current: object) -> object:
    if isinstance(current, bool):
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in _BOOL_TRUE
    if isinstance(current, int):
        return int(raw)  # type: ignore[arg-type]
    if isinstance(current, float):
        return float(raw)  # type: ignore[arg-type]
    if isinstance(current, Path) or name in ("db_file", "documents_path"):
        return Path(str(raw)).expanduser() if raw not in (None, "") else None
    return str(raw)


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader: KEY=VALUE lines; only UWH_* keys are used; no shell expansion."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key.startswith("UWH_"):
            out[key] = value
    return out


def load_settings(**overrides: object) -> Settings:
    """Precedence: defaults < uwhelper.toml (data dir) < .env file < environment variables < explicit overrides.

    The .env file is ``UWH_ENV_FILE`` if set, otherwise ``.env`` in the current directory (keep it out of Git).
    """
    env_file = Path(os.environ.get("UWH_ENV_FILE") or ".env")
    file_env = read_env_file(env_file)
    environ = {**file_env, **{k: v for k, v in os.environ.items() if k.startswith("UWH_")}}
    base = Settings()
    env_data_dir = environ.get("UWH_DATA_DIR")
    if env_data_dir:
        base = replace(base, data_dir=Path(env_data_dir).expanduser())
    if "data_dir" in overrides:
        base = replace(base, data_dir=Path(str(overrides["data_dir"])))

    values: dict[str, object] = {}
    toml_path = base.data_dir / "uwhelper.toml"
    if toml_path.is_file():
        # utf-8-sig tolerates a BOM written by some Windows editors.
        values.update(tomllib.loads(toml_path.read_text(encoding="utf-8-sig")))
    for f in fields(Settings):
        env = environ.get(f"UWH_{f.name.upper()}")
        if env is not None:
            values[f.name] = env
    values.update(overrides)

    kwargs = {}
    for f in fields(Settings):
        if f.name in values:
            kwargs[f.name] = _coerce(f.name, values[f.name], getattr(base, f.name))
    return replace(base, **kwargs).validate()
