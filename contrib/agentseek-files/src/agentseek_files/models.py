from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

FileDirection = Literal["inbound", "outbound"]
ExtractStatus = Literal["not_started", "pending", "running", "done", "failed"]


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
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileRecord:
        return cls(**data)


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
