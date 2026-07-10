from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

FileDirection = Literal["inbound", "outbound"]
ExtractStatus = Literal["not_started", "pending", "running", "done", "failed"]
BackgroundOcrStatus = Literal["not_started", "pending", "running", "done", "failed", "skipped"]


@dataclass(frozen=True)
class FileScope:
    tenant_key: str
    employee_key: str
    session_key: str
    channel: str = "unknown"
    chat_id: str | None = None
    message_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileRecord:
    file_id: str
    direction: FileDirection
    tenant_key: str
    employee_key: str
    session_key: str
    date: str
    filename: str
    sanitized_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    relative_dir: str
    created_at: str
    expires_at: str | None = None
    channel: str = "unknown"
    chat_id: str | None = None
    message_id: str | None = None
    extract_status: ExtractStatus = "not_started"
    extract_provider: str | None = None
    extract_task_id: str | None = None
    extract_chars: int = 0
    notify_on_done: bool = False
    notified_at: str | None = None
    mixed_pdf_bg_ocr: bool = False
    bg_ocr_status: BackgroundOcrStatus = "not_started"
    bg_ocr_task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileRecord:
        payload = dict(data)
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            metadata = dict(metadata)
            for field_name in ("mixed_pdf_bg_ocr", "bg_ocr_status", "bg_ocr_task_id"):
                if field_name not in payload and field_name in metadata:
                    payload[field_name] = metadata.pop(field_name)
            payload["metadata"] = metadata
        return cls(**payload)


@dataclass
class ExtractResult:
    file_id: str
    provider: str
    status: ExtractStatus
    text: str = ""
    markdown: str = ""
    chars: int = 0
    provider_task_id: str | None = None
    provider_trace_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
