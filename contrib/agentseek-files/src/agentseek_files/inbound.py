from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path

from loguru import logger

from agentseek_files.context import build_current_files_context
from agentseek_files.extractors.local import LocalTextExtractor
from agentseek_files.extractors.mineru import (
    MinerUExtractor,
    image_reference_count,
    merge_background_ocr_markdown,
)
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
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._background_file_ids: set[str] = set()

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
        auto_non_ocr_pdf = False
        mixed_pdf = False
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
            auto_non_ocr_pdf = self._mineru_extractor.is_auto_non_ocr_pdf_result(record, result)
            mixed_pdf = self._mineru_extractor.should_run_mixed_pdf_background_ocr(record, result)
            retry_with_ocr = self._mineru_extractor.should_retry_with_ocr(record, result)
            if retry_with_ocr:
                try:
                    result = await self._mineru_extractor.retry_with_ocr(
                        record,
                        self.store.original_path(record),
                    )
                except Exception as exc:
                    logger.warning(
                        "files.ocr_retry failed file_id={} error={}",
                        record.file_id,
                        type(exc).__name__,
                    )
                    result = ExtractResult(
                        file_id=record.file_id,
                        provider="mineru",
                        status="failed",
                        provider_task_id=record.extract_task_id,
                        error_code="ocr_retry_failed",
                        error_message="MinerU OCR retry failed.",
                    )
        record = self.store.save_extract(record, result)
        if record.extract_provider == "mineru" and auto_non_ocr_pdf:
            if mixed_pdf and self.settings.mixed_pdf_bg_ocr:
                self._schedule_mixed_pdf_background_ocr(record)
            else:
                record.mixed_pdf_bg_ocr = False
                record.bg_ocr_status = "skipped"
                self.store.save_record(record)
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

    def _schedule_mixed_pdf_background_ocr(self, record: FileRecord) -> None:
        status = record.bg_ocr_status
        if record.file_id in self._background_file_ids or status in {"pending", "running", "done"}:
            return
        record.mixed_pdf_bg_ocr = True
        record.bg_ocr_status = "pending"
        self.store.save_record(record)
        self._background_file_ids.add(record.file_id)
        task = asyncio.create_task(
            self._background_ocr_retry(record.relative_dir),
            name=f"agentseek-files-mixed-pdf-ocr-{record.file_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(lambda completed, file_id=record.file_id: self._background_done(file_id, completed))

    def _background_done(self, file_id: str, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        self._background_file_ids.discard(file_id)
        if not task.cancelled():
            task.exception()

    async def _background_ocr_retry(self, relative_dir: str) -> None:
        try:
            record = self.store.load_record(relative_dir)
            record.bg_ocr_status = "running"
            self.store.save_record(record)
            pending = await self._mineru_extractor.retry_with_ocr(
                record,
                self.store.original_path(record),
            )
            if pending.status not in {"pending", "running"} or not pending.provider_task_id:
                record = self.store.load_record(relative_dir)
                record.bg_ocr_status = "failed"
                self.store.save_record(record)
                logger.warning(
                    "files.mixed_pdf_bg_ocr failed file_id={} error=missing_task_id",
                    record.file_id,
                )
                return

            record = self.store.load_record(relative_dir)
            record.bg_ocr_status = "running"
            record.bg_ocr_task_id = pending.provider_task_id
            self.store.save_record(record)

            task_record = FileRecord.from_dict(record.to_dict())
            task_record.extract_task_id = pending.provider_task_id
            task_record.metadata["extract"] = dict(pending.metadata)
            result = pending
            while result.status in {"pending", "running"}:
                result = await self._mineru_extractor.poll_result(
                    task_record,
                    pending.provider_task_id,
                )
                if result.status in {"pending", "running"}:
                    await asyncio.sleep(self.settings.mineru_poll_interval_s)

            record = self.store.load_record(relative_dir)
            if result.status == "done":
                first_pass = self.store.load_extract_text(record)
                background_pass = result.markdown or result.text
                merged, changed, strategy = merge_background_ocr_markdown(first_pass, background_pass)
                record.mixed_pdf_bg_ocr = True
                record.bg_ocr_status = "done"
                record.bg_ocr_task_id = pending.provider_task_id
                record.metadata["background_ocr"] = {
                    "result_changed": changed,
                    "merge_strategy": strategy,
                    "result_chars": len(background_pass),
                    "image_refs_before": image_reference_count(first_pass),
                    "image_refs_after": image_reference_count(background_pass),
                }
                if changed:
                    result = replace(result, markdown=merged, text=merged, chars=len(merged))
                    self.store.save_extract(record, result)
                else:
                    self.store.save_record(record)
                logger.info(
                    "files.mixed_pdf_bg_ocr done file_id={} changed={} strategy={}",
                    record.file_id,
                    changed,
                    strategy,
                )
                return
            record.bg_ocr_status = "failed"
            self.store.save_record(record)
            logger.warning("files.mixed_pdf_bg_ocr failed file_id={}", record.file_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                record = self.store.load_record(relative_dir)
                record.bg_ocr_status = "failed"
                self.store.save_record(record)
                file_id = record.file_id
            except Exception:
                file_id = "unknown"
            logger.warning(
                "files.mixed_pdf_bg_ocr failed file_id={} error={}",
                file_id,
                type(exc).__name__,
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
