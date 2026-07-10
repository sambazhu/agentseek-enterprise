from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentseek_files.context import build_current_files_context
from agentseek_files.extractors.local import LocalTextExtractor
from agentseek_files.extractors.mineru import MinerUExtractor
from agentseek_files.models import ExtractResult, FileRecord, FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore


@dataclass
class InboundFileResult:
    record: FileRecord
    context_block: str
    user_notice: str
    extract_text: str = ""
    pending: bool = False

    def to_context(self) -> dict[str, object]:
        return {
            "current_files_context": self.context_block,
            "records": [self.record.to_dict()],
            "pending": self.pending,
        }


class InboundFileService:
    """Save inbound channel files and produce model-facing file context."""

    def __init__(
        self,
        settings: FilesSettings | None = None,
        store: LocalFileStore | None = None,
    ) -> None:
        self.settings = settings or FilesSettings.from_env()
        self.store = store or LocalFileStore(self.settings)
        self._local_extractor = LocalTextExtractor(max_chars=self.settings.extract_max_chars)
        self._mineru_extractor = MinerUExtractor(self.settings)

    async def handle_bytes(
        self,
        *,
        scope: FileScope,
        filename: str,
        data: bytes,
        mime_type: str = "application/octet-stream",
    ) -> InboundFileResult:
        record = self.store.store_bytes(
            scope=scope,
            filename=filename,
            data=data,
            mime_type=mime_type,
        )
        result = await self._extract_initial(record)
        record = self.store.save_extract(record, result)
        text = result.markdown or result.text
        context_block = build_current_files_context(
            [record],
            {record.file_id: text} if text else {},
            max_chars_per_file=self.settings.extract_max_chars,
        )
        return InboundFileResult(
            record=record,
            context_block=context_block,
            extract_text=text,
            pending=result.status in {"pending", "running"},
            user_notice=_notice(record, result),
        )

    async def poll_pending(self, record: FileRecord) -> InboundFileResult:
        if not record.extract_task_id:
            result = ExtractResult(
                file_id=record.file_id,
                provider=record.extract_provider or self.settings.extractor,
                status="failed",
                error_code="missing_task_id",
                error_message="Pending file record has no extractor task id.",
            )
        else:
            result = await self._mineru_extractor.poll_result(record, record.extract_task_id)
        record = self.store.save_extract(record, result)
        text = result.markdown or result.text
        context_block = build_current_files_context(
            [record],
            {record.file_id: text} if text else {},
            max_chars_per_file=self.settings.extract_max_chars,
        )
        return InboundFileResult(
            record=record,
            context_block=context_block,
            extract_text=text,
            pending=result.status in {"pending", "running"},
            user_notice=_notice(record, result),
        )

    async def _extract_initial(self, record: FileRecord) -> ExtractResult:
        original_path = self.store.original_path(record)
        if self._local_extractor.can_extract(record):
            return await self._local_extractor.extract(record, original_path)

        if self.settings.extractor.strip().lower() == "mineru" and self._mineru_extractor.can_extract(record):
            return await self._mineru_extractor.extract(record, original_path)

        suffix = Path(record.sanitized_filename).suffix.lower()
        return ExtractResult(
            file_id=record.file_id,
            provider="none",
            status="not_started",
            error_code="unsupported_extractor",
            error_message=f"No extractor configured for {suffix or record.mime_type}.",
        )


def _notice(record: FileRecord, result: ExtractResult) -> str:
    filename = record.sanitized_filename
    if result.status == "done":
        return f"已收到并解析文件：{filename}。"
    if result.status in {"pending", "running"}:
        return f"已收到文件：{filename}。文件正在解析，请稍后再问我文件内容。"
    if result.status == "failed":
        return f"已收到文件：{filename}，但解析失败：{result.error_message or result.error_code or 'unknown'}。"
    return f"已收到文件：{filename}，但当前类型暂不支持自动解析。"
