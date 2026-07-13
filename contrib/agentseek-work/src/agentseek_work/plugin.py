from __future__ import annotations

import asyncio
import importlib
import os
from typing import Any, Protocol, runtime_checkable

from bub import hookimpl
from bub.types import Envelope, State
from loguru import logger

WORK_ENRICHED_STATE_KEY = "_work_enriched"
DIGITAL_EMPLOYEE_STATUS_STATE_KEY = "digital_employee_status"
WORK_BINDING_PATH_ENV = "AGENTSEEK_WORK_BINDING"
WORK_ENABLED_ENV = "AGENTSEEK_WORK_ENABLED"


@runtime_checkable
class WorkStateBinding(Protocol):
    """Template-owned adapter used to enrich aggregate Bub turn state."""

    def load_message_state(self, message: Envelope, session_id: str) -> State: ...

    def enrich_state(self, message: Envelope, session_id: str, state: State) -> None: ...


class WorkPlugin:
    """Bub plugin composition point for persistent enterprise work capabilities."""

    def __init__(self, framework: Any | None = None) -> None:
        del framework
        self._binding: WorkStateBinding | None = None
        self._binding_resolved = False

    @hookimpl
    async def load_state(self, message: Envelope, session_id: str) -> State:
        """Capture a template-owned opaque turn key before prompt synthesis."""

        if not _work_enabled():
            return {}
        binding = self._get_binding()
        if binding is None:
            return {DIGITAL_EMPLOYEE_STATUS_STATE_KEY: "not_configured"}
        try:
            return await asyncio.to_thread(binding.load_message_state, message, session_id)
        except Exception as exc:  # fail closed without retaining the source message identifier.
            logger.warning("work message state load failed error_type={}", type(exc).__name__)
            return {
                DIGITAL_EMPLOYEE_STATUS_STATE_KEY: "not_configured",
                "_work_binding_error": type(exc).__name__,
            }

    @hookimpl(tryfirst=True)
    async def build_prompt(
        self,
        message: Envelope,
        session_id: str,
        state: State,
    ) -> None:
        """Enrich state after identity plugins have published aggregate turn state."""

        if state.get(WORK_ENRICHED_STATE_KEY) or not _work_enabled():
            return
        state[WORK_ENRICHED_STATE_KEY] = True
        binding = self._get_binding()
        if binding is None:
            state[DIGITAL_EMPLOYEE_STATUS_STATE_KEY] = "not_configured"
            return
        try:
            await asyncio.to_thread(binding.enrich_state, message, session_id, state)
        except Exception as exc:  # fail closed; prompt execution must remain available.
            state[DIGITAL_EMPLOYEE_STATUS_STATE_KEY] = "not_configured"
            state["_work_binding_error"] = type(exc).__name__
            logger.warning("work state enrichment failed error_type={}", type(exc).__name__)

    def _get_binding(self) -> WorkStateBinding | None:
        if self._binding_resolved:
            return self._binding
        self._binding_resolved = True
        path = os.environ.get(WORK_BINDING_PATH_ENV, "").strip()
        if not path:
            logger.warning("AGENTSEEK_WORK_ENABLED=true but AGENTSEEK_WORK_BINDING is empty")
            return None
        module_name, separator, factory_name = path.partition(":")
        if separator != ":" or not module_name.strip() or not factory_name.isidentifier():
            logger.warning("invalid AGENTSEEK_WORK_BINDING import path")
            return None
        try:
            factory = getattr(importlib.import_module(module_name), factory_name)
            binding = factory()
        except Exception as exc:
            logger.warning("work binding load failed error_type={}", type(exc).__name__)
            return None
        if not isinstance(binding, WorkStateBinding):
            logger.warning("configured work binding does not implement WorkStateBinding")
            return None
        self._binding = binding
        return binding


def _work_enabled() -> bool:
    return os.environ.get(WORK_ENABLED_ENV, "false").strip().lower() in {"1", "true", "yes", "on"}


def main(framework: Any) -> WorkPlugin:
    return WorkPlugin(framework)
