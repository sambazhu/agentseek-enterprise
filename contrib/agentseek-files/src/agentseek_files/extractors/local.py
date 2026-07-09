from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from agentseek_files.models import ExtractResult, FileRecord


class LocalTextExtractor:
    provider = "local"
    supported_extensions: ClassVar[set[str]] = {".txt", ".md", ".csv", ".json"}

    def __init__(self, *, max_chars: int = 12_000) -> None:
        self.max_chars = max_chars

    def can_extract(self, record: FileRecord) -> bool:
        return Path(record.sanitized_filename).suffix.lower() in self.supported_extensions

    async def extract(self, record: FileRecord, source_path: Path) -> ExtractResult:
        if not self.can_extract(record):
            return ExtractResult(
                file_id=record.file_id,
                provider=self.provider,
                status="failed",
                error_code="unsupported_extension",
                error_message="Local extractor only supports text-like files.",
            )
        text = source_path.read_bytes().decode("utf-8", errors="replace")
        truncated = text[: self.max_chars]
        return ExtractResult(
            file_id=record.file_id,
            provider=self.provider,
            status="done",
            text=truncated,
            markdown=truncated,
            chars=len(truncated),
            metadata={"truncated": len(text) > len(truncated)},
        )
