from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from bub import hookimpl
from bub.types import State
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.outputs import LLMResult
from loguru import logger
from republic import AsyncStreamEvents, StreamEvent, StreamState

from agentseek_langchain.ag_ui import runtime_context_from_state
from agentseek_langchain.config import get_langchain_settings
from agentseek_langchain.loader import load_spec_from_path
from agentseek_langchain.spec import InvocationContext

_MODEL_TIMEOUT_MESSAGE = "本次模型处理超时，请稍后重试。"


@dataclass(slots=True)
class _ModelCallStats:
    started_at: float
    input_chars: int
    system_chars: int
    message_count: int
    tool_count: int
    model_name: str


class _ModelCallObservability(AsyncCallbackHandler):
    """Record model latency and sizes without retaining prompt content."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._calls: dict[UUID, _ModelCallStats] = {}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        flattened = [message for batch in messages for message in batch]
        self._calls[run_id] = _ModelCallStats(
            started_at=time.monotonic(),
            input_chars=sum(_content_chars(message.content) for message in flattened),
            system_chars=sum(
                _content_chars(message.content) for message in flattened if isinstance(message, SystemMessage)
            ),
            message_count=len(flattened),
            tool_count=_tool_count(kwargs),
            model_name=_model_name(serialized, kwargs),
        )

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self._calls.setdefault(
            run_id,
            _ModelCallStats(
                started_at=time.monotonic(),
                input_chars=sum(len(prompt) for prompt in prompts),
                system_chars=0,
                message_count=len(prompts),
                tool_count=_tool_count(kwargs),
                model_name=_model_name(serialized, kwargs),
            ),
        )

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        stats = self._calls.pop(run_id, None)
        if stats is None:
            return
        usage = _token_usage(response)
        _emit_enterprise_event(
            "langchain_model_call",
            status="succeeded",
            session_id=self._session_id,
            elapsed_ms=round((time.monotonic() - stats.started_at) * 1000),
            input_chars=stats.input_chars,
            system_chars=stats.system_chars,
            message_count=stats.message_count,
            tool_count=stats.tool_count,
            model_name=stats.model_name,
            output_chars=_llm_output_chars(response),
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            total_tokens=usage[2],
        )

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        stats = self._calls.pop(run_id, None)
        if stats is None:
            return
        _emit_enterprise_event(
            "langchain_model_call",
            status="error",
            session_id=self._session_id,
            elapsed_ms=round((time.monotonic() - stats.started_at) * 1000),
            input_chars=stats.input_chars,
            system_chars=stats.system_chars,
            message_count=stats.message_count,
            tool_count=stats.tool_count,
            model_name=stats.model_name,
            error_type=type(error).__name__,
        )


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
            callbacks=(_ModelCallObservability(session_id),),
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
        run_started = time.monotonic()
        _emit_run_stage(
            session_id,
            stage="run_model",
            status="started",
            prompt_chars=len(_prompt_text(prompt)),
            state_key_count=len(state),
        )
        stage_started = time.monotonic()
        spec = self._get_spec()
        _emit_run_stage(
            session_id,
            stage="spec_resolve",
            status="succeeded" if spec is not None else "skipped",
            started_at=stage_started,
        )
        if spec is None:
            _emit_run_stage(session_id, stage="run_model", status="skipped", started_at=run_started)
            return None

        stage_started = time.monotonic()
        await self._enrich_state_from_prompt_hooks(prompt, session_id, state)
        _emit_run_stage(
            session_id,
            stage="prompt_enrichment",
            status="succeeded",
            started_at=stage_started,
        )
        stage_started = time.monotonic()
        context = self._build_context(prompt, session_id, state)
        _emit_run_stage(
            session_id,
            stage="context_build",
            status="succeeded",
            started_at=stage_started,
        )
        timeout = _run_timeout_seconds()
        invoke_started = time.monotonic()
        _emit_run_stage(
            session_id,
            stage="model_invoke",
            status="started",
            timeout_seconds=timeout,
        )
        try:
            async with asyncio.timeout(timeout):
                result = await spec.invoke(context)
        except TimeoutError:
            _emit_run_stage(
                session_id,
                stage="model_invoke",
                status="timeout",
                started_at=invoke_started,
                timeout_seconds=timeout,
            )
            _emit_run_stage(session_id, stage="run_model", status="timeout", started_at=run_started)
            logger.error("LangChain turn timed out session_id={} timeout={}s", session_id, timeout)
            return _MODEL_TIMEOUT_MESSAGE
        except asyncio.CancelledError:
            _emit_run_stage(session_id, stage="model_invoke", status="cancelled", started_at=invoke_started)
            _emit_run_stage(session_id, stage="run_model", status="cancelled", started_at=run_started)
            raise
        except Exception as exc:
            _emit_run_stage(
                session_id,
                stage="model_invoke",
                status="error",
                started_at=invoke_started,
                error_type=type(exc).__name__,
            )
            _emit_run_stage(
                session_id,
                stage="run_model",
                status="error",
                started_at=run_started,
                error_type=type(exc).__name__,
            )
            raise
        _emit_run_stage(
            session_id,
            stage="model_invoke",
            status="succeeded",
            started_at=invoke_started,
            output_chars=len(result),
        )
        _emit_run_stage(session_id, stage="run_model", status="succeeded", started_at=run_started)
        return result

    @hookimpl(tryfirst=True)
    async def run_model_stream(
        self,
        prompt: str | list[dict[str, Any]],
        session_id: str,
        state: State,
    ) -> AsyncStreamEvents | None:
        run_started = time.monotonic()
        _emit_run_stage(
            session_id,
            stage="run_model_stream",
            status="started",
            prompt_chars=len(_prompt_text(prompt)),
            state_key_count=len(state),
        )
        stage_started = time.monotonic()
        spec = self._get_spec()
        _emit_run_stage(
            session_id,
            stage="spec_resolve",
            status="succeeded" if spec is not None else "skipped",
            started_at=stage_started,
            streaming=True,
        )
        if spec is None:
            _emit_run_stage(
                session_id,
                stage="run_model_stream",
                status="skipped",
                started_at=run_started,
            )
            return None

        stage_started = time.monotonic()
        await self._enrich_state_from_prompt_hooks(prompt, session_id, state)
        _emit_run_stage(
            session_id,
            stage="prompt_enrichment",
            status="succeeded",
            started_at=stage_started,
            streaming=True,
        )
        stage_started = time.monotonic()
        context = self._build_context(prompt, session_id, state)
        _emit_run_stage(
            session_id,
            stage="context_build",
            status="succeeded",
            started_at=stage_started,
            streaming=True,
        )
        stream_state = StreamState()

        async def iterator():
            chunks: list[str] = []
            timeout = _run_timeout_seconds()
            ok = True
            invoke_started = time.monotonic()
            _emit_run_stage(
                session_id,
                stage="model_invoke",
                status="started",
                timeout_seconds=timeout,
                streaming=True,
            )
            try:
                async with asyncio.timeout(timeout):
                    async for chunk in spec.stream(context):
                        chunks.append(chunk)
                        yield StreamEvent("text", {"delta": chunk})
            except TimeoutError:
                ok = False
                _emit_run_stage(
                    session_id,
                    stage="model_invoke",
                    status="timeout",
                    started_at=invoke_started,
                    timeout_seconds=timeout,
                    streaming=True,
                )
                logger.error("LangChain stream timed out session_id={} timeout={}s", session_id, timeout)
                chunks.append(_MODEL_TIMEOUT_MESSAGE)
                yield StreamEvent("text", {"delta": _MODEL_TIMEOUT_MESSAGE})
            except asyncio.CancelledError:
                _emit_run_stage(
                    session_id,
                    stage="model_invoke",
                    status="cancelled",
                    started_at=invoke_started,
                    streaming=True,
                )
                _emit_run_stage(
                    session_id,
                    stage="run_model_stream",
                    status="cancelled",
                    started_at=run_started,
                )
                raise
            except Exception as exc:
                _emit_run_stage(
                    session_id,
                    stage="model_invoke",
                    status="error",
                    started_at=invoke_started,
                    error_type=type(exc).__name__,
                    streaming=True,
                )
                _emit_run_stage(
                    session_id,
                    stage="run_model_stream",
                    status="error",
                    started_at=run_started,
                    error_type=type(exc).__name__,
                )
                raise
            else:
                _emit_run_stage(
                    session_id,
                    stage="model_invoke",
                    status="succeeded",
                    started_at=invoke_started,
                    output_chars=sum(len(chunk) for chunk in chunks),
                    streaming=True,
                )
            _emit_run_stage(
                session_id,
                stage="run_model_stream",
                status="succeeded" if ok else "timeout",
                started_at=run_started,
            )
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


def _emit_run_stage(
    session_id: str,
    *,
    stage: str,
    status: str,
    started_at: float | None = None,
    **fields: Any,
) -> None:
    if started_at is not None:
        fields["elapsed_ms"] = round((time.monotonic() - started_at) * 1000)
    _emit_enterprise_event(
        "langchain_run_stage",
        status=status,
        session_id=session_id,
        stage=stage,
        **fields,
    )


def _run_timeout_seconds() -> float:
    settings = get_langchain_settings()
    return max(0.01, float(getattr(settings, "RUN_TIMEOUT_SECONDS", 180.0) or 180.0))


def _content_chars(content: object) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(_content_chars(item) for item in content)
    if isinstance(content, dict):
        return sum(_content_chars(value) for value in content.values())
    return 0


def _tool_count(kwargs: dict[str, Any]) -> int:
    invocation = kwargs.get("invocation_params")
    tools = invocation.get("tools") if isinstance(invocation, dict) else None
    return len(tools) if isinstance(tools, list) else 0


def _model_name(serialized: dict[str, Any], kwargs: dict[str, Any]) -> str:
    invocation = kwargs.get("invocation_params")
    if isinstance(invocation, dict):
        for key in ("model", "model_name"):
            value = invocation.get(key)
            if isinstance(value, str) and value:
                return value[:120]
    name = serialized.get("name")
    if isinstance(name, str) and name:
        return name[:120]
    serialized_id = serialized.get("id")
    if isinstance(serialized_id, list) and serialized_id:
        return str(serialized_id[-1])[:120]
    return "unknown"


def _token_usage(response: LLMResult) -> tuple[int, int, int]:
    prompt_tokens = completion_tokens = total_tokens = 0
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if isinstance(usage, dict):
                prompt_tokens += int(usage.get("input_tokens") or 0)
                completion_tokens += int(usage.get("output_tokens") or 0)
                total_tokens += int(usage.get("total_tokens") or 0)
    if prompt_tokens or completion_tokens or total_tokens:
        return prompt_tokens, completion_tokens, total_tokens or prompt_tokens + completion_tokens
    llm_output = response.llm_output
    token_usage = llm_output.get("token_usage") if isinstance(llm_output, dict) else None
    if not isinstance(token_usage, dict):
        return 0, 0, 0
    prompt_tokens = int(token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0)
    completion_tokens = int(token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0)
    total_tokens = int(token_usage.get("total_tokens") or prompt_tokens + completion_tokens)
    return prompt_tokens, completion_tokens, total_tokens


def _llm_output_chars(response: LLMResult) -> int:
    total = 0
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            content = getattr(message, "content", None)
            total += _content_chars(content if content is not None else getattr(generation, "text", ""))
    return total


def _emit_enterprise_event(event: str, **fields: Any) -> None:
    try:
        from agentseek_enterprise.observability import emit_enterprise_event
    except ImportError:
        return
    emit_enterprise_event(event, **fields)


def main(framework: object | None = None) -> LangChainRunnablePlugin:
    return LangChainRunnablePlugin(framework)
