from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bub import hookimpl
from bub.envelope import field_of
from bub.types import Envelope, State
from loguru import logger

from agentseek_files.context import build_current_files_context
from agentseek_files.inbound import InboundFileService
from agentseek_files.models import FileRecord
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore

CURRENT_FILES_CONTEXT_STATE_KEY = "current_files_context"
CURRENT_FILES_STATE_KEY = "current_files"


class FilesPlugin:
    """Bub plugin placeholder for AgentSeek file capabilities."""

    def __init__(
        self,
        framework: Any | None = None,
        *,
        settings: FilesSettings | None = None,
        store: LocalFileStore | None = None,
    ) -> None:
        del framework
        self.settings = settings or FilesSettings.from_env()
        self.store = store or LocalFileStore(self.settings)
        self.inbound = InboundFileService(self.settings, self.store)
        self._session_records: dict[str, list[dict[str, Any]]] = {}

    @hookimpl
    async def load_state(self, message: Envelope, session_id: str) -> State:
        context = field_of(message, "context", {})
        state: State = {}
        files = context.get("files") if isinstance(context, Mapping) else None
        incoming_records: list[dict[str, Any]] = []
        if isinstance(files, Mapping):
            current_files_context = files.get("current_files_context")
            if isinstance(current_files_context, str) and current_files_context.strip():
                state[CURRENT_FILES_CONTEXT_STATE_KEY] = current_files_context.strip()
            records = files.get("records")
            if isinstance(records, list):
                incoming_records = [dict(record) for record in records if isinstance(record, Mapping)]
                if incoming_records:
                    self._session_records[session_id] = incoming_records
                    state[CURRENT_FILES_STATE_KEY] = incoming_records

        records_to_refresh = incoming_records or self._session_records.get(session_id, [])
        refreshed_records, extracts = await self._refresh_records(records_to_refresh)
        if refreshed_records:
            serialized_records = [record.to_dict() for record in refreshed_records]
            self._session_records[session_id] = serialized_records
            state[CURRENT_FILES_STATE_KEY] = serialized_records
            state[CURRENT_FILES_CONTEXT_STATE_KEY] = build_current_files_context(
                refreshed_records,
                extracts,
                max_chars_per_file=self.settings.extract_max_chars,
            )
        return state

    async def _refresh_records(
        self,
        records: list[dict[str, Any]],
    ) -> tuple[list[FileRecord], dict[str, str]]:
        refreshed: list[FileRecord] = []
        extracts: dict[str, str] = {}
        for data in records:
            try:
                snapshot = FileRecord.from_dict(data)
                record = self.store.load_record(snapshot.relative_dir)
                text = self.store.load_extract_text(record)
                if record.extract_status in {"pending", "running"}:
                    result = await self.inbound.poll_pending(record)
                    record = result.record
                    text = result.extract_text
                refreshed.append(record)
                if text:
                    extracts[record.file_id] = text
            except Exception as exc:
                logger.warning(
                    "files.state_refresh skipped file_id={} error={}",
                    data.get("file_id", ""),
                    type(exc).__name__,
                )
        return refreshed, extracts


def main(framework: Any) -> FilesPlugin:
    return FilesPlugin(framework)
