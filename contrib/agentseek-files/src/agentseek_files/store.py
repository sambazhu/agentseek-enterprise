from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentseek_files.models import ExtractResult, FileDirection, FileRecord, FileScope
from agentseek_files.settings import FilesSettings

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MIME_TYPE_EXTENSIONS = {"application/pdf": ".pdf"}


class FileStoreError(ValueError):
    """Raised when a file cannot be accepted into the scoped store."""


class LocalFileStore:
    """Scoped host-filesystem store for files managed by AgentSeek.

    This is not an agent filesystem backend. Callers should pass file ids and
    extracted context to the model, not host paths.
    """

    def __init__(self, settings: FilesSettings | None = None) -> None:
        self.settings = settings or FilesSettings.from_env()
        self.root_dir = self.settings.root_dir

    def store_bytes(
        self,
        *,
        scope: FileScope,
        filename: str,
        data: bytes,
        mime_type: str = "application/octet-stream",
        direction: FileDirection = "inbound",
        now: datetime | None = None,
    ) -> FileRecord:
        if direction not in {"inbound", "outbound"}:
            raise FileStoreError(f"Unsupported file direction: {direction}")
        if len(data) > self.settings.max_bytes:
            raise FileStoreError(f"File exceeds max size: {len(data)} > {self.settings.max_bytes}")

        safe_name = sanitize_filename(filename)
        extension = Path(safe_name).suffix.lower()
        if not extension:
            extension = _MIME_TYPE_EXTENSIONS.get(mime_type.partition(";")[0].strip().lower(), "")
            if extension:
                if not str(filename or "").lower().endswith(extension):
                    filename = f"{filename or Path(safe_name).stem}{extension}"
                safe_name = f"{safe_name}{extension}"
        if extension not in self.settings.allowed_extensions:
            raise FileStoreError(f"File extension is not allowed: {extension or '<none>'}")

        created_at = now or datetime.now(UTC)
        sha256 = hashlib.sha256(data).hexdigest()
        file_id = f"file_{sha256[:16]}"
        date = created_at.date().isoformat()
        relative_dir = Path(scope.tenant_key) / scope.employee_key / date / scope.session_key / direction / file_id
        target_dir = self._resolve_under_root(relative_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "original").write_bytes(data)

        expires_at = created_at + timedelta(days=self.settings.retention_days)
        record = FileRecord(
            file_id=file_id,
            direction=direction,
            tenant_key=scope.tenant_key,
            employee_key=scope.employee_key,
            session_key=scope.session_key,
            date=date,
            filename=filename,
            sanitized_filename=safe_name,
            mime_type=mime_type,
            size_bytes=len(data),
            sha256=sha256,
            relative_dir=relative_dir.as_posix(),
            created_at=created_at.isoformat(),
            expires_at=expires_at.isoformat(),
            channel=scope.channel,
            chat_id=scope.chat_id,
            message_id=scope.message_id,
            notify_on_done=self.settings.notify_on_done,
        )
        self.save_record(record)
        return record

    def save_record(self, record: FileRecord) -> None:
        target_dir = self._resolve_record_dir(record)
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "metadata.json").write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def load_record(self, relative_dir: str) -> FileRecord:
        target_dir = self._resolve_under_root(Path(relative_dir))
        data = json.loads((target_dir / "metadata.json").read_text(encoding="utf-8"))
        return FileRecord.from_dict(data)

    def original_path(self, record: FileRecord) -> Path:
        return self._resolve_record_dir(record) / "original"

    def save_extract(self, record: FileRecord, result: ExtractResult) -> FileRecord:
        target_dir = self._resolve_record_dir(record)
        if result.markdown:
            (target_dir / "extracted.md").write_text(result.markdown, encoding="utf-8")
        if result.text and not result.markdown:
            (target_dir / "extracted.txt").write_text(result.text, encoding="utf-8")
        (target_dir / "extract.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        record.extract_status = result.status
        record.extract_provider = result.provider
        record.extract_task_id = result.provider_task_id
        record.extract_chars = result.chars
        if result.metadata:
            record.metadata["extract"] = dict(result.metadata)
        self.save_record(record)
        return record

    def load_extract_text(self, record: FileRecord) -> str:
        target_dir = self._resolve_record_dir(record)
        for name in ("extracted.md", "extracted.txt"):
            path = target_dir / name
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return ""

    def _resolve_record_dir(self, record: FileRecord) -> Path:
        return self._resolve_under_root(Path(record.relative_dir))

    def _resolve_under_root(self, relative_path: Path) -> Path:
        root = self.root_dir.resolve()
        candidate = (root / relative_path).resolve()
        if not candidate.is_relative_to(root):
            raise FileStoreError("Resolved path escapes file store root")
        return candidate


def sanitize_filename(filename: str) -> str:
    name = Path(filename or "file").name.strip() or "file"
    suffix = Path(name).suffix.lower()
    stem = name[: -len(suffix)] if suffix else name
    safe_stem = _SAFE_FILENAME_RE.sub("_", stem).strip("._")
    safe_suffix = _SAFE_FILENAME_RE.sub("", suffix)
    if not safe_stem:
        safe_stem = "file"
    safe = f"{safe_stem}{safe_suffix}"
    return safe[:180]
