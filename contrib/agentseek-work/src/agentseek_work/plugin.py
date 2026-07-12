from __future__ import annotations

from typing import Any


class WorkPlugin:
    """Bub plugin composition point for persistent enterprise work capabilities."""

    def __init__(self, framework: Any | None = None) -> None:
        self.framework = framework


def main(framework: Any) -> WorkPlugin:
    return WorkPlugin(framework)
