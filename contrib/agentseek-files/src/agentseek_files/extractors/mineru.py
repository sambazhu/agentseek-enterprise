from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from agentseek_files.models import ExtractResult, FileRecord
from agentseek_files.settings import FilesSettings


class MinerUExtractor:
    """Remote MinerU extractor.

    The extractor submits local files to MinerU and returns task metadata. Long
    polling and notification are handled by a caller/poller so WeCom response
    streams are not held open for large documents.
    """

    provider = "mineru"

    def __init__(self, settings: FilesSettings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or FilesSettings.from_env()
        self._client = client

    def can_extract(self, record: FileRecord) -> bool:
        return Path(record.sanitized_filename).suffix.lower() in {
            ".pdf",
            ".doc",
            ".docx",
            ".ppt",
            ".pptx",
            ".xls",
            ".xlsx",
            ".png",
            ".jpg",
            ".jpeg",
            ".jp2",
            ".webp",
            ".gif",
            ".bmp",
        }

    async def extract(self, record: FileRecord, source_path: Path) -> ExtractResult:
        if not self.can_extract(record):
            return ExtractResult(
                file_id=record.file_id,
                provider=self.provider,
                status="failed",
                error_code="unsupported_extension",
                error_message="MinerU extractor does not support this file extension.",
            )
        return await self.submit_agent_file(record, source_path)

    async def submit_agent_file(self, record: FileRecord, source_path: Path) -> ExtractResult:
        async with self._managed_client() as client:
            response = await client.post(
                f"{self.settings.mineru_base_url}/api/v1/agent/parse/file",
                json={
                    "file_name": record.sanitized_filename,
                    "language": self.settings.mineru_language,
                    "enable_table": self.settings.mineru_enable_table,
                    "is_ocr": self.settings.mineru_is_ocr,
                    "enable_formula": self.settings.mineru_enable_formula,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                return _failed(record, payload)
            data = payload.get("data") or {}
            task_id = str(data.get("task_id") or "")
            file_url = str(data.get("file_url") or "")
            if not task_id or not file_url:
                return _failed(record, {"msg": "MinerU response missing task_id or file_url"})
            # httpx.AsyncClient rejects a synchronous file object as request
            # content. Read it off the event loop and send async-compatible bytes.
            source_bytes = await asyncio.to_thread(source_path.read_bytes)
            upload = await client.put(file_url, content=source_bytes)
            upload.raise_for_status()
            return ExtractResult(
                file_id=record.file_id,
                provider=self.provider,
                status="pending",
                provider_task_id=task_id,
                provider_trace_id=str(payload.get("trace_id") or ""),
                metadata={"mode": "agent"},
            )

    async def poll_agent_result(self, record: FileRecord, task_id: str) -> ExtractResult:
        async with self._managed_client() as client:
            response = await client.get(f"{self.settings.mineru_base_url}/api/v1/agent/parse/{task_id}")
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                return _failed(record, payload, task_id=task_id)
            data = payload.get("data") or {}
            state = str(data.get("state") or "")
            if state != "done":
                return ExtractResult(
                    file_id=record.file_id,
                    provider=self.provider,
                    status="failed" if state == "failed" else "running",
                    provider_task_id=task_id,
                    provider_trace_id=str(payload.get("trace_id") or ""),
                    error_message=str(data.get("err_msg") or "") or None,
                    metadata={"state": state},
                )
            markdown_url = str(data.get("md_url") or data.get("markdown_url") or data.get("full_md_url") or "")
            markdown = ""
            if markdown_url:
                md_response = await client.get(markdown_url)
                md_response.raise_for_status()
                markdown = md_response.text
            return ExtractResult(
                file_id=record.file_id,
                provider=self.provider,
                status="done",
                markdown=markdown,
                text=markdown,
                chars=len(markdown),
                provider_task_id=task_id,
                provider_trace_id=str(payload.get("trace_id") or ""),
                metadata={"state": state, "markdown_url_present": bool(markdown_url)},
            )

    def _managed_client(self) -> _ClientContext:
        if self._client is not None:
            return _ClientContext(self._client, close=False)
        return _ClientContext(httpx.AsyncClient(timeout=30), close=True)


class _ClientContext:
    def __init__(self, client: httpx.AsyncClient, *, close: bool) -> None:
        self.client = client
        self.close = close

    async def __aenter__(self) -> httpx.AsyncClient:
        return self.client

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        if self.close:
            await self.client.aclose()


def _failed(record: FileRecord, payload: dict[str, Any], *, task_id: str | None = None) -> ExtractResult:
    return ExtractResult(
        file_id=record.file_id,
        provider=MinerUExtractor.provider,
        status="failed",
        provider_task_id=task_id,
        provider_trace_id=str(payload.get("trace_id") or ""),
        error_code=str(payload.get("code") or ""),
        error_message=str(payload.get("msg") or "MinerU extraction failed"),
    )
