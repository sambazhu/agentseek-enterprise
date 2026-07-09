from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bub import hookimpl
from bub.envelope import field_of
from bub.types import Envelope, State

from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore

CURRENT_FILES_CONTEXT_STATE_KEY = "current_files_context"
CURRENT_FILES_STATE_KEY = "current_files"


class FilesPlugin:
    """Bub plugin placeholder for AgentSeek file capabilities."""

    def __init__(self, framework: Any | None = None) -> None:
        del framework
        self.settings = FilesSettings.from_env()
        self.store = LocalFileStore(self.settings)

    @hookimpl
    def load_state(self, message: Envelope, session_id: str) -> State:
        del session_id
        context = field_of(message, "context", {})
        if not isinstance(context, Mapping):
            return {}
        files = context.get("files")
        if not isinstance(files, Mapping):
            return {}
        current_files_context = files.get("current_files_context")
        state: State = {}
        if isinstance(current_files_context, str) and current_files_context.strip():
            state[CURRENT_FILES_CONTEXT_STATE_KEY] = current_files_context.strip()
        records = files.get("records")
        if isinstance(records, list):
            state[CURRENT_FILES_STATE_KEY] = records
        return state


def main(framework: Any) -> FilesPlugin:
    return FilesPlugin(framework)
