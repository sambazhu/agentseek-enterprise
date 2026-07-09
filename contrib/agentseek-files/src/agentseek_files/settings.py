from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FilesSettings:
    enabled: bool = False
    root_dir: Path = Path("runtime/files")
    max_bytes: int = 10 * 1024 * 1024
    allowed_extensions: tuple[str, ...] = (
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".amr",
        ".mp4",
        ".mp3",
        ".wav",
        ".m4a",
    )
    extract_max_chars: int = 12_000
    retention_days: int = 7
    extractor: str = "local"
    notify_on_done: bool = True
    mineru_base_url: str = "https://mineru.net"
    mineru_token: str = ""
    mineru_model_version: str = "vlm"
    mineru_language: str = "ch"
    mineru_enable_table: bool = True
    mineru_enable_formula: bool = True
    mineru_is_ocr: bool = False
    mineru_poll_timeout_s: float = 15.0
    mineru_poll_interval_s: float = 2.0

    @classmethod
    def from_env(cls) -> FilesSettings:
        return cls(
            enabled=_env_bool("AGENTSEEK_FILES_ENABLED", False),
            root_dir=Path(os.getenv("AGENTSEEK_FILES_DIR", "runtime/files")),
            max_bytes=_env_int("AGENTSEEK_FILES_MAX_BYTES", 10 * 1024 * 1024),
            allowed_extensions=_env_extensions("AGENTSEEK_FILES_ALLOWED_EXTENSIONS", cls.allowed_extensions),
            extract_max_chars=_env_int("AGENTSEEK_FILES_EXTRACT_MAX_CHARS", 12_000),
            retention_days=_env_int("AGENTSEEK_FILES_RETENTION_DAYS", 7),
            extractor=os.getenv("AGENTSEEK_FILES_EXTRACTOR", "local").strip() or "local",
            notify_on_done=_env_bool("AGENTSEEK_FILES_NOTIFY_ON_DONE", True),
            mineru_base_url=os.getenv("AGENTSEEK_MINERU_BASE_URL", "https://mineru.net").rstrip("/"),
            mineru_token=os.getenv("AGENTSEEK_MINERU_TOKEN", ""),
            mineru_model_version=os.getenv("AGENTSEEK_MINERU_MODEL_VERSION", "vlm"),
            mineru_language=os.getenv("AGENTSEEK_MINERU_LANGUAGE", "ch"),
            mineru_enable_table=_env_bool("AGENTSEEK_MINERU_ENABLE_TABLE", True),
            mineru_enable_formula=_env_bool("AGENTSEEK_MINERU_ENABLE_FORMULA", True),
            mineru_is_ocr=_env_bool("AGENTSEEK_MINERU_IS_OCR", False),
            mineru_poll_timeout_s=_env_float("AGENTSEEK_MINERU_POLL_TIMEOUT_S", 15.0),
            mineru_poll_interval_s=_env_float("AGENTSEEK_MINERU_POLL_INTERVAL_S", 2.0),
        )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value)


def _env_extensions(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return tuple(_normalize_extension(part) for part in value.split(",") if part.strip())


def _normalize_extension(value: str) -> str:
    extension = value.strip().lower()
    if not extension:
        return extension
    return extension if extension.startswith(".") else f".{extension}"
