from __future__ import annotations

from typing import Any

from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore


class FilesPlugin:
    """Bub plugin placeholder for AgentSeek file capabilities."""

    def __init__(self, framework: Any | None = None) -> None:
        del framework
        self.settings = FilesSettings.from_env()
        self.store = LocalFileStore(self.settings)


def main(framework: Any) -> FilesPlugin:
    return FilesPlugin(framework)
