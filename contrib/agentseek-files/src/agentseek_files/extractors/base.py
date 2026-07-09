from __future__ import annotations

from pathlib import Path
from typing import Protocol

from agentseek_files.models import ExtractResult, FileRecord


class FileExtractor(Protocol):
    provider: str

    def can_extract(self, record: FileRecord) -> bool: ...

    async def extract(self, record: FileRecord, source_path: Path) -> ExtractResult: ...
