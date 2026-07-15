from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from bub import hookimpl
from bub.types import State
from loguru import logger
from republic import AsyncStreamEvents, StreamEvent, StreamState

from agentseek_langchain.ag_ui import runtime_context_from_state
from agentseek_langchain.config import get_langchain_settings
from agentseek_langchain.loader import load_spec_from_path
from agentseek_langchain.spec import InvocationContext

_MODEL_TIMEOUT_MESSAGE = "本次模型处理超时，请稍后重试。"


class LangChainRunnablePlugin:
    def __init__(self, framework: object | None = None) -> None:
        self._framework = framework
        self._spec_cache = None
        self._spec_resolved = False

    def _get_spec(self):
        if self._spec_resolved:
            return self._spec_cache

        self._spec_resolved = True
        settings = get_langchain_settings()
        spec_path = settings.SPEC.strip()
        if not spec_path:
            logger.warning("LangChain spec not configured; falling back to the default model entrypoint.")
            return None

        self._spec_cache = load_spec_from_path(spec_path)
        logger.info(f"Using LangChain spec entrypoint: {spec_path}")
        return self._spec_cache

    def _build_context(self, prompt: str | list[dict[str, Any]], session_id: str, state: State) -> InvocationContext:
        workspace_value = state.get("_runtime_workspace")
        workspace = Path(str(workspace_value)).resolve() if workspace_value else Path.cwd().resolve()
        return InvocationContext(
            prompt=prompt,
            session_id=session_id,
            state=state,
            workspace=workspace,
            agents_md=self._read_agents_md(workspace),
            runtime_context=runtime_context_from_state(state),
        )

    async def _enrich_state_from_prompt_hooks(
        self,
        prompt: str | list[dict[str, Any]],
        session_id: str,
        state: State,
    ) -> None:
        hook_runtime = getattr(self._framework, "_hook_runtime", None)
        if hook_runtime is None:
            return
        call_many = getattr(hook_runtime, "call_many", None)
        if call_many is None:
            return
        try:
            await call_many(
                "build_prompt",
                message={"content": _prompt_text(prompt), "session_id": session_id},
                session_id=session_id,
                state=state,
            )
        except Exception as exc:
            logger.debug(f"Prompt hook state enrichment skipped: {exc}")

    @staticmethod
    def _read_agents_md(workspace: Path) -> str | None:
        path = workspace / "AGENTS.md"
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return content or None

    @hookimpl(tryfirst=True)
    async def run_model(self, prompt: str | list[dict[str, Any]], session_id: str, state: State) -> str | None:
        spec = self._get_spec()
        if spec is None:
            return None
        await self._enrich_state_from_prompt_hooks(prompt, session_id, state)
        timeout = _run_timeout_seconds()
        try:
            async with asyncio.timeout(timeout):
                return await spec.invoke(self._build_context(prompt, session_id, state))
        except TimeoutError:
            logger.error("LangChain turn timed out session_id={} timeout={}s", session_id, timeout)
            return _MODEL_TIMEOUT_MESSAGE

    @hookimpl(tryfirst=True)
    async def run_model_stream(
        self,
        prompt: str | list[dict[str, Any]],
        session_id: str,
        state: State,
    ) -> AsyncStreamEvents | None:
        spec = self._get_spec()
        if spec is None:
            return None

        await self._enrich_state_from_prompt_hooks(prompt, session_id, state)
        context = self._build_context(prompt, session_id, state)
        stream_state = StreamState()

        async def iterator():
            chunks: list[str] = []
            timeout = _run_timeout_seconds()
            ok = True
            try:
                async with asyncio.timeout(timeout):
                    async for chunk in spec.stream(context):
                        chunks.append(chunk)
                        yield StreamEvent("text", {"delta": chunk})
            except TimeoutError:
                ok = False
                logger.error("LangChain stream timed out session_id={} timeout={}s", session_id, timeout)
                chunks.append(_MODEL_TIMEOUT_MESSAGE)
                yield StreamEvent("text", {"delta": _MODEL_TIMEOUT_MESSAGE})
            yield StreamEvent("final", {"text": "".join(chunks), "ok": ok})

        return AsyncStreamEvents(iterator(), state=stream_state)


def _prompt_text(prompt: str | list[dict[str, Any]]) -> str:
    if isinstance(prompt, str):
        return prompt
    parts: list[str] = []
    for item in prompt:
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _run_timeout_seconds() -> float:
    settings = get_langchain_settings()
    return max(0.01, float(getattr(settings, "RUN_TIMEOUT_SECONDS", 180.0) or 180.0))


def main(framework: object | None = None) -> LangChainRunnablePlugin:
    return LangChainRunnablePlugin(framework)
