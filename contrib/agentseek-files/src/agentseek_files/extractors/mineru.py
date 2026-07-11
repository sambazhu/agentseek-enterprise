from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from loguru import logger

from agentseek_files.models import ExtractResult, FileRecord
from agentseek_files.settings import FilesSettings

_MAX_MARKDOWN_BYTES = 50 * 1024 * 1024
_MIN_SUBSTANTIVE_CHARS = 100
_HTML_IMAGE_COMMENT_RE = re.compile(r"<!--\s*image\s*-->", re.IGNORECASE)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


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
        if self.settings.mineru_token.strip():
            return await self.submit_extract_file(
                record,
                source_path,
                is_ocr=self.settings.mineru_is_ocr,
                ocr_mode="forced" if self.settings.mineru_is_ocr else "auto",
            )
        return await self.submit_agent_file(
            record,
            source_path,
            is_ocr=self.settings.mineru_is_ocr,
            ocr_mode="forced" if self.settings.mineru_is_ocr else "auto",
        )

    async def submit_extract_file(
        self,
        record: FileRecord,
        source_path: Path,
        *,
        is_ocr: bool,
        ocr_mode: str,
        ocr_attempt: int = 1,
    ) -> ExtractResult:
        """Upload a local file through MinerU's token-authenticated v4 Extract API."""
        model_version = (
            self.settings.mineru_ocr_model_version if is_ocr else self.settings.mineru_model_version
        )
        body = {
            "files": [
                {
                    "name": record.sanitized_filename,
                    "data_id": record.file_id,
                    "is_ocr": is_ocr,
                }
            ],
            "model_version": model_version,
            "language": self.settings.mineru_language,
            "enable_table": self.settings.mineru_enable_table,
            "enable_formula": self.settings.mineru_enable_formula,
        }
        logger.debug("mineru.v4 submit body={}", json.dumps(_redacted_v4_body(body), sort_keys=True))
        async with self._managed_client() as client:
            response = await client.post(
                f"{self.settings.mineru_base_url}/api/v4/file-urls/batch",
                headers=self._auth_headers(),
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                return _failed(record, payload)
            data = payload.get("data") or {}
            batch_id = str(data.get("batch_id") or "")
            file_urls = data.get("file_urls")
            file_url = str(file_urls[0]) if isinstance(file_urls, list) and file_urls else ""
            if not batch_id or not file_url:
                return _failed(record, {"msg": "MinerU response missing batch_id or file_urls"})
            source_bytes = await asyncio.to_thread(source_path.read_bytes)
            upload = await client.put(file_url, content=source_bytes)
            upload.raise_for_status()
            return ExtractResult(
                file_id=record.file_id,
                provider=self.provider,
                status="pending",
                provider_task_id=batch_id,
                provider_trace_id=str(payload.get("trace_id") or ""),
                metadata={
                    "mode": "extract_batch",
                    "ocr_mode": ocr_mode,
                    "is_ocr": is_ocr,
                    "ocr_attempt": ocr_attempt,
                    "model_version": model_version,
                },
            )

    async def submit_agent_file(
        self,
        record: FileRecord,
        source_path: Path,
        *,
        is_ocr: bool,
        ocr_mode: str,
        ocr_attempt: int = 1,
    ) -> ExtractResult:
        async with self._managed_client() as client:
            response = await client.post(
                f"{self.settings.mineru_base_url}/api/v1/agent/parse/file",
                json={
                    "file_name": record.sanitized_filename,
                    "language": self.settings.mineru_language,
                    "enable_table": self.settings.mineru_enable_table,
                    "is_ocr": is_ocr,
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
                metadata={
                    "mode": "agent",
                    "ocr_mode": ocr_mode,
                    "is_ocr": is_ocr,
                    "ocr_attempt": ocr_attempt,
                },
            )

    async def poll_result(self, record: FileRecord, task_id: str) -> ExtractResult:
        extract_metadata = _extract_metadata(record)
        mode = str(extract_metadata.get("mode") or "")
        if mode == "extract_batch":
            return await self.poll_extract_result(record, task_id)
        return await self.poll_agent_result(record, task_id)

    def should_retry_with_ocr(self, record: FileRecord, result: ExtractResult) -> bool:
        return self.is_auto_non_ocr_pdf_result(record, result) and not has_substantive_text(
            result.markdown or result.text
        )

    def should_run_mixed_pdf_background_ocr(
        self,
        record: FileRecord,
        result: ExtractResult,
    ) -> bool:
        content = result.markdown or result.text
        return (
            self.is_auto_non_ocr_pdf_result(record, result)
            and has_substantive_text(content)
            and has_image_references(content)
        )

    def is_auto_non_ocr_pdf_result(self, record: FileRecord, result: ExtractResult) -> bool:
        metadata = _extract_metadata(record)
        return (
            Path(record.sanitized_filename).suffix.lower() in {".pdf", ".docx", ".pptx", ".xlsx"}
            and result.status == "done"
            and metadata.get("ocr_mode") == "auto"
            and metadata.get("is_ocr") is False
        )

    async def retry_with_ocr(
        self,
        record: FileRecord,
        source_path: Path,
    ) -> ExtractResult:
        metadata = _extract_metadata(record)
        mode = str(metadata.get("mode") or "")
        if mode == "extract_batch":
            return await self.submit_extract_file(
                record,
                source_path,
                is_ocr=True,
                ocr_mode="auto",
                ocr_attempt=2,
            )
        return await self.submit_agent_file(
            record,
            source_path,
            is_ocr=True,
            ocr_mode="auto",
            ocr_attempt=2,
        )

    async def poll_extract_result(self, record: FileRecord, batch_id: str) -> ExtractResult:
        task_metadata = _extract_metadata(record)
        async with self._managed_client() as client:
            response = await client.get(
                f"{self.settings.mineru_base_url}/api/v4/extract-results/batch/{batch_id}",
                headers=self._auth_headers(),
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                return _failed(record, payload, task_id=batch_id)
            data = payload.get("data") or {}
            extract_results = data.get("extract_result")
            if not isinstance(extract_results, list) or not extract_results:
                return _failed(
                    record,
                    {"msg": "MinerU response missing extract_result", "trace_id": payload.get("trace_id")},
                    task_id=batch_id,
                )
            item = next(
                (
                    candidate
                    for candidate in extract_results
                    if isinstance(candidate, dict)
                    and str(candidate.get("data_id") or "") == record.file_id
                ),
                extract_results[0],
            )
            if not isinstance(item, dict):
                return _failed(record, {"msg": "MinerU returned an invalid extract result"}, task_id=batch_id)
            state = str(item.get("state") or "")
            if state != "done":
                return ExtractResult(
                    file_id=record.file_id,
                    provider=self.provider,
                    status="failed" if state == "failed" else "running",
                    provider_task_id=batch_id,
                    provider_trace_id=str(payload.get("trace_id") or ""),
                    error_message=str(item.get("err_msg") or "") or None,
                    metadata={**task_metadata, "mode": "extract_batch", "state": state},
                )
            full_zip_url = str(item.get("full_zip_url") or "")
            if not full_zip_url:
                return _failed(
                    record,
                    {"msg": "MinerU completed without full_zip_url", "trace_id": payload.get("trace_id")},
                    task_id=batch_id,
                )
            zip_response = await client.get(full_zip_url)
            zip_response.raise_for_status()
            markdown = _markdown_from_zip(zip_response.content)
            return ExtractResult(
                file_id=record.file_id,
                provider=self.provider,
                status="done",
                markdown=markdown,
                text=markdown,
                chars=len(markdown),
                provider_task_id=batch_id,
                provider_trace_id=str(payload.get("trace_id") or ""),
                metadata={
                    **task_metadata,
                    "mode": "extract_batch",
                    "state": state,
                    "full_zip_url_present": True,
                },
            )

    async def poll_agent_result(self, record: FileRecord, task_id: str) -> ExtractResult:
        task_metadata = _extract_metadata(record)
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
                    metadata={**task_metadata, "mode": "agent", "state": state},
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
                metadata={
                    **task_metadata,
                    "mode": "agent",
                    "state": state,
                    "markdown_url_present": bool(markdown_url),
                },
            )

    def _auth_headers(self) -> dict[str, str]:
        token = self.settings.mineru_token.strip()
        if not token:
            raise RuntimeError("MinerU v4 Extract API requires AGENTSEEK_MINERU_TOKEN")
        return {"Authorization": f"Bearer {token}"}

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


def has_substantive_text(content: str) -> bool:
    """Return whether MinerU output contains useful text beyond image placeholders."""
    return substantive_character_count(content) > _MIN_SUBSTANTIVE_CHARS


def substantive_character_count(content: str) -> int:
    """Count useful alphanumeric characters after removing image placeholders."""
    text = _HTML_IMAGE_COMMENT_RE.sub("", str(content or ""))
    text = _MARKDOWN_IMAGE_RE.sub("", text)
    text = _HTML_IMAGE_RE.sub("", text)
    return sum(character.isalnum() for character in text)


def has_image_references(content: str) -> bool:
    """Return whether MinerU markdown still contains an unparsed image page."""
    text = str(content or "")
    return bool(_HTML_IMAGE_COMMENT_RE.search(text) or _MARKDOWN_IMAGE_RE.search(text) or _HTML_IMAGE_RE.search(text))


def image_reference_count(content: str) -> int:
    """Count unresolved image placeholders in MinerU markdown."""
    text = str(content or "")
    return (
        len(_HTML_IMAGE_COMMENT_RE.findall(text))
        + len(_MARKDOWN_IMAGE_RE.findall(text))
        + len(_HTML_IMAGE_RE.findall(text))
    )


def merge_background_ocr_markdown(first_pass: str, background_pass: str) -> tuple[str, bool, str]:
    """Select or merge a Scheme C result without discarding useful first-pass text."""
    first = str(first_pass or "").strip()
    background = str(background_pass or "").strip()
    if not background or _comparison_text(background) == _comparison_text(first):
        return first, False, "unchanged"

    first_chars = substantive_character_count(first)
    background_chars = substantive_character_count(background)
    first_images = image_reference_count(first)
    background_images = image_reference_count(background)
    improved = background_images < first_images or background_chars > first_chars + 20
    if not improved:
        return first, False, "no_ocr_improvement"

    if not first or first in background or len(background) >= max(1, len(first) // 2):
        return background, True, "replace"
    merged = f"{first}\n\n<!-- MinerU background OCR supplement -->\n{background}"
    return merged, True, "append_supplement"


def _comparison_text(content: str) -> str:
    return "".join(str(content or "").split())


def _extract_metadata(record: FileRecord) -> dict[str, Any]:
    metadata = record.metadata.get("extract")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _redacted_v4_body(body: dict[str, Any]) -> dict[str, Any]:
    redacted = {key: value for key, value in body.items() if key != "files"}
    files = body.get("files")
    redacted_files: list[dict[str, Any]] = []
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            redacted_files.append(
                {
                    "name": "<redacted>",
                    "data_id": "<redacted>",
                    "is_ocr": bool(item.get("is_ocr")),
                }
            )
    redacted["files"] = redacted_files
    return redacted


def _markdown_from_zip(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            candidates = [
                info
                for info in archive.infolist()
                if not info.is_dir() and PurePosixPath(info.filename).name == "full.md"
            ]
            if not candidates:
                raise RuntimeError("MinerU result archive does not contain full.md")
            candidate = min(candidates, key=lambda info: len(info.filename))
            if candidate.file_size > _MAX_MARKDOWN_BYTES:
                raise RuntimeError("MinerU full.md exceeds the maximum accepted size")
            return archive.read(candidate).decode("utf-8")
    except (UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise RuntimeError("MinerU returned an invalid result archive") from exc


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
