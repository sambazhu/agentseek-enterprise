from __future__ import annotations

import logging
from typing import Any

_loguru_logger: Any | None

try:
    from loguru import logger as _imported_loguru_logger
except ImportError:  # pragma: no cover - exercised only in minimal installs without loguru.
    _loguru_logger = None
else:
    _loguru_logger = _imported_loguru_logger


class _StdlibLoggerAdapter:
    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def debug(self, message: str, *args: Any) -> None:
        self._logger.debug(_format_message(message, args))

    def info(self, message: str, *args: Any) -> None:
        self._logger.info(_format_message(message, args))

    def warning(self, message: str, *args: Any) -> None:
        self._logger.warning(_format_message(message, args))


def get_logger(name: str) -> Any:
    if _loguru_logger is not None:
        return _loguru_logger.bind(agentseek_module=name)
    return _StdlibLoggerAdapter(name)


def _format_message(message: str, args: tuple[Any, ...]) -> str:
    if not args:
        return message
    try:
        return message.format(*args)
    except Exception:  # pragma: no cover - defensive fallback for malformed log templates.
        return " ".join([message, *(str(arg) for arg in args)])
