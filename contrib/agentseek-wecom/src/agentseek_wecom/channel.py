from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

import uvicorn
from bub.channels.base import Channel
from bub.channels.message import ChannelMessage
from bub.envelope import content_of
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from republic import StreamEvent

from agentseek_wecom.config import WeComSettings
from agentseek_wecom.crypto import WeComCryptoError, WeComJsonCrypto
from agentseek_wecom.media import MediaDownload, WeComMediaClient, decode_encoding_aes_key
from agentseek_wecom.messages import make_text, make_text_stream
from agentseek_wecom.response_url import WeComResponseUrlSender
from agentseek_wecom.userid_resolver import UseridResolver, make_userid_resolver

_STREAM_ID_ATTR = "_agentseek_wecom_stream_id"
_QUEUE_SESSION_ID_ATTR = "_agentseek_wecom_queue_session_id"
_FROM_USERID_ATTR = "_agentseek_wecom_from_userid"
_QUEUE_STATUS_COMMANDS = frozenset({"查看消息队列", "查看排队状态"})


class MediaClient(Protocol):
    def download(self, media_id: str, *, fallback_filename: str, fallback_mime_type: str) -> MediaDownload: ...

    def download_media(
        self,
        url: str,
        *,
        aes_key: bytes,
        fallback_filename: str,
        fallback_mime_type: str,
    ) -> MediaDownload: ...


class InboundFileServiceProtocol(Protocol):
    async def handle_bytes(
        self,
        *,
        scope: Any,
        filename: str,
        data: bytes,
        mime_type: str,
    ) -> Any: ...

    async def poll_pending(self, record: Any) -> Any: ...


class ResponseUrlSenderProtocol(Protocol):
    def send_markdown(self, response_url: str, content: str) -> None: ...


@dataclass
class StreamReply:
    stream_id: str
    session_id: str
    chat_id: str
    from_userid: str | None
    response_url: str | None = None
    initial_response_sent: bool = False
    initial_response_content: str | None = None
    response_url_consumed: bool = False
    content: str = ""
    finish: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def update(self, *, content: str | None = None, append: str | None = None, finish: bool | None = None) -> None:
        if content is not None:
            self.content = content
        if append:
            self.content += append
        if finish is not None:
            self.finish = finish
        self.updated_at = time.time()


@dataclass(slots=True)
class QueuedTurn:
    message: ChannelMessage
    enqueued_at: float
    pending_counted: bool = False
    started: bool = False
    expired: bool = False
    expiry_handle: asyncio.TimerHandle | None = None


class WeComChannel(Channel):
    name = "wecom"

    def __init__(
        self,
        on_receive: Any,
        *,
        settings: WeComSettings,
        userid_resolver: UseridResolver | None = None,
        media_client: MediaClient | None = None,
        file_service: InboundFileServiceProtocol | None = None,
        response_url_sender: ResponseUrlSenderProtocol | None = None,
    ) -> None:
        self._on_receive = on_receive
        self.settings = settings
        self._userid_resolver = userid_resolver if userid_resolver is not None else make_userid_resolver(settings)
        self._media_client = media_client
        self._file_service = file_service
        self._file_service_initialized = file_service is not None
        self._response_url_sender = response_url_sender or WeComResponseUrlSender(
            api_base_url=settings.api_base_url,
            timeout_seconds=settings.api_timeout_seconds,
        )
        self._crypto: WeComJsonCrypto | None = None
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._streams: dict[str, StreamReply] = {}
        self._stream_ids_by_msgid: dict[str, str] = {}
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._session_queues: dict[str, asyncio.Queue[QueuedTurn]] = {}
        self._session_workers: dict[str, asyncio.Task[None]] = {}
        self._active_turn_started_at: dict[str, float] = {}
        self._pending_turn_counts: dict[str, int] = {}
        self._queue_expiry_handles: set[asyncio.TimerHandle] = set()
        self.app = self._build_app()

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def bind_receiver(self, on_receive: Any) -> None:
        self._on_receive = on_receive

    async def start(self, stop_event: asyncio.Event) -> None:
        del stop_event
        if not self.enabled:
            return
        self._crypto = self._build_crypto()
        if self._server_task is not None and not self._server_task.done():
            return
        config = uvicorn.Config(
            self.app,
            host=self.settings.host,
            port=self.settings.port,
            loop="asyncio",
            log_level="info",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve(), name="agentseek-wecom.server")
        await self._wait_until_started()

    async def stop(self) -> None:
        server = self._server
        task = self._server_task
        self._server = None
        self._server_task = None
        if server is not None:
            server.should_exit = True
        if task is not None and not task.done():
            try:
                async with asyncio.timeout(max(0.1, self.settings.shutdown_timeout_seconds)):
                    await task
            except TimeoutError:
                logger.warning("wecom.server graceful shutdown timed out; cancelling server task")
                task.cancel()
                _, pending = await asyncio.wait(
                    {task},
                    timeout=max(0.1, self.settings.shutdown_timeout_seconds),
                )
                if pending:
                    logger.error("wecom.server task did not stop after cancellation")
        dispatch_tasks = list(self._dispatch_tasks)
        for dispatch_task in dispatch_tasks:
            dispatch_task.cancel()
        if dispatch_tasks:
            _, pending = await asyncio.wait(
                dispatch_tasks,
                timeout=max(0.1, self.settings.shutdown_timeout_seconds),
            )
            if pending:
                logger.warning("wecom.dispatch graceful shutdown timed out after cancellation")
        self._dispatch_tasks.clear()
        for handle in self._queue_expiry_handles:
            handle.cancel()
        self._queue_expiry_handles.clear()
        self._session_workers.clear()
        self._session_queues.clear()
        self._active_turn_started_at.clear()
        self._pending_turn_counts.clear()

    async def send(self, message: ChannelMessage) -> None:
        stream = await self._stream_for_outbound(message)
        if stream is None:
            return
        stream.update(content=content_of(message), finish=True)
        delivery_status = "succeeded"
        if stream.response_url and stream.initial_response_sent:
            delivery_status = await self._deliver_response_url_once(stream, stream.content)
        _emit_enterprise_event(
            "wecom_stream_finished",
            status=delivery_status,
            stream_id=stream.stream_id,
            session_id=stream.session_id,
            chat_id=stream.chat_id,
            from_userid=stream.from_userid,
            content_chars=len(stream.content),
            age_ms=round((time.time() - stream.created_at) * 1000),
        )

    def stream_events(self, message: ChannelMessage, stream: AsyncIterable[StreamEvent]) -> AsyncIterable[StreamEvent]:
        return self._stream_events(message, stream)

    async def _stream_events(
        self,
        message: ChannelMessage,
        stream: AsyncIterable[StreamEvent],
    ) -> AsyncGenerator[StreamEvent, None]:
        stream_id = getattr(message, _STREAM_ID_ATTR, None)
        async for event in stream:
            if isinstance(stream_id, str):
                reply = await self._get_stream(stream_id)
                if reply is not None:
                    if event.kind == "text":
                        reply.update(append=str(event.data.get("delta", "")), finish=False)
                    elif event.kind == "error":
                        reply.update(content=str(event.data.get("message", "模型处理失败")), finish=True)
                        _emit_enterprise_event(
                            "wecom_stream_finished",
                            status="error",
                            stream_id=reply.stream_id,
                            session_id=reply.session_id,
                            chat_id=reply.chat_id,
                            from_userid=reply.from_userid,
                            error_message=reply.content,
                            age_ms=round((time.time() - reply.created_at) * 1000),
                        )
            yield event

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get(self.settings.callback_path)
        async def verify_url(
            request: Request,
            msg_signature: str,
            timestamp: str,
            nonce: str,
            echostr: str,
            botid: str | None = None,
        ) -> Response:
            del request, botid
            crypto = self._require_crypto()
            try:
                plain_echo = crypto.verify_url(
                    msg_signature=msg_signature,
                    timestamp=timestamp,
                    nonce=nonce,
                    echostr=echostr,
                )
            except WeComCryptoError as exc:
                logger.warning("wecom.verify_url failed: {}", exc)
                plain_echo = "verify fail"
            return Response(content=plain_echo, media_type="text/plain")

        @app.post(self.settings.callback_path)
        async def handle_message(
            request: Request,
            botid: str | None = None,
            msg_signature: str | None = None,
            timestamp: str | None = None,
            nonce: str | None = None,
        ) -> Response:
            del botid
            if not msg_signature or not timestamp or not nonce:
                raise HTTPException(status_code=400, detail="missing WeCom callback query parameters")

            crypto = self._require_crypto()
            encrypted_body = await request.body()
            try:
                plain_text = crypto.decrypt_message(
                    post_data=encrypted_body,
                    msg_signature=msg_signature,
                    timestamp=timestamp,
                    nonce=nonce,
                )
                data = json.loads(plain_text)
            except (WeComCryptoError, json.JSONDecodeError) as exc:
                logger.warning("wecom.decrypt failed: {}", exc)
                raise HTTPException(status_code=400, detail="decrypt failed") from exc

            response_plain = await self._handle_plain_message(data)
            if response_plain is None:
                return Response(content="success", media_type="text/plain")

            encrypted = crypto.encrypt_message(response_plain, nonce=nonce, timestamp=timestamp)
            return Response(content=encrypted.to_json(), media_type="text/plain")

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return {"ok": True, "channel": self.name, "enabled": self.enabled}

        return app

    async def _handle_plain_message(self, data: dict[str, Any]) -> str | None:
        msgtype = data.get("msgtype")
        logger.debug("wecom.incoming msgtype={} msgid={}", msgtype, _extract_msgid(data))
        if msgtype == "text":
            return await self._handle_text(data)
        if msgtype in {"file", "image", "video"}:
            return await self._handle_media_message(data)
        if msgtype == "voice":
            return await self._handle_voice(data)
        if msgtype == "mixed":
            return await self._handle_mixed(data)
        if msgtype == "stream":
            return await self._handle_stream_poll(data)
        if msgtype == "event":
            return self._handle_event(data)
        logger.info("wecom.unsupported_msgtype msgtype={}", msgtype)
        return None

    async def _handle_text(self, data: dict[str, Any]) -> str:
        content = str((data.get("text") or {}).get("content") or "")
        trigger = self.settings.response_url_probe_trigger
        if trigger and content == trigger:
            return await self._handle_response_url_probe(data)
        return await self._dispatch_user_message(data, content)

    async def _handle_response_url_probe(self, data: dict[str, Any]) -> str:
        from_userid = _extract_from_userid(data)
        stream = await self._create_stream(
            session_id=f"wecom:{from_userid or 'unknown'}",
            chat_id=from_userid or "wecom:unknown",
            from_userid=from_userid,
        )
        response_url = _extract_response_url(data)
        if not response_url:
            stream.update(content="短连接延迟回复探针失败：回调未包含 response_url。", finish=True)
            return await self._stream_response(stream.stream_id)

        stream.update(content="短连接延迟回复探针已启动，请等待第二条消息。", finish=True)
        task = asyncio.create_task(
            self._run_response_url_probe(response_url),
            name=f"agentseek-wecom.response-url-probe.{stream.stream_id}",
        )
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._on_dispatch_done)
        return await self._stream_response(stream.stream_id)

    async def _run_response_url_probe(self, response_url: str) -> None:
        await asyncio.sleep(max(self.settings.response_url_probe_delay_seconds, 0.0))
        try:
            await asyncio.to_thread(
                self._response_url_sender.send_markdown,
                response_url,
                "AgentSeek v0.1.0 M0：短连接 response_url 延迟回复探针成功。",
            )
        except Exception as exc:
            logger.warning("wecom.response_url_probe failed error_type={}", type(exc).__name__)
            _emit_enterprise_event("wecom_response_url_probe", status="error", error_type=type(exc).__name__)
        else:
            _emit_enterprise_event("wecom_response_url_probe", status="succeeded")

    async def _handle_voice(self, data: dict[str, Any]) -> str:
        content = str((data.get("voice") or {}).get("content") or "")
        if not content:
            content = "用户发送了一条语音消息，但回调未包含转写内容。"
        return await self._dispatch_user_message(data, content)

    async def _handle_mixed(self, data: dict[str, Any]) -> str:
        content = _mixed_text_content(data)
        if _extract_media_items(data):
            return await self._handle_media_message(data, fallback_content=content)
        return await self._dispatch_user_message(data, content or "用户发送了一条图文混排消息。")

    async def _handle_media_message(self, data: dict[str, Any], *, fallback_content: str = "") -> str:
        media_items = _extract_media_items(data)
        content = fallback_content.strip()
        if not media_items:
            content = content or f"用户发送了 {data.get('msgtype') or 'media'} 消息，但回调未包含可下载 URL。"
            return await self._dispatch_user_message(data, content)

        from_userid = _extract_from_userid(data)
        resolved_userid = await self._resolve_userid(from_userid)
        userid = resolved_userid or from_userid
        session_id = f"wecom:{userid or 'unknown'}"
        chat_id = userid or session_id
        stream, is_duplicate = await self._get_or_create_stream_for_message(
            msgid=_extract_msgid(data),
            session_id=session_id,
            chat_id=chat_id,
            from_userid=from_userid,
            response_url=_extract_response_url(data),
        )
        if is_duplicate:
            logger.info("wecom.duplicate_msgid stream_id={}", stream.stream_id)
            _emit_enterprise_event(
                "wecom_duplicate_msgid",
                stream_id=stream.stream_id,
                session_id=session_id,
                chat_id=chat_id,
                from_userid=from_userid,
                msgtype=data.get("msgtype"),
            )
            return await self._stream_response(stream.stream_id)

        files_context: dict[str, Any] = {}
        pending_record: Any | None = None
        leading_content = content
        try:
            result = await self._download_and_store_media(
                data=data,
                media=media_items[0],
                session_id=session_id,
                chat_id=chat_id,
                userid=userid,
                from_userid=from_userid,
            )
        except Exception as exc:
            logger.warning("wecom.media_intake failed msgtype={} error={}", data.get("msgtype"), exc)
            _emit_enterprise_event(
                "wecom_media_intake",
                status="error",
                session_id=session_id,
                chat_id=chat_id,
                from_userid=from_userid,
                msgtype=data.get("msgtype"),
                media_kind=media_items[0].get("kind"),
                error_type=type(exc).__name__,
            )
            user_notice = _file_error_user_notice(exc)
            if user_notice:
                content = "\n".join(part for part in (content, user_notice) if part).strip()
            else:
                content = (
                    content
                    or f"已收到{_msgtype_label(data.get('msgtype'))}，但文件下载或解析失败：{type(exc).__name__}。"
                )
        else:
            files_context = result.to_context()
            content_parts = [content] if content else []
            content_parts.append(result.user_notice)
            if result.context_block:
                content_parts.append("请结合当前文件上下文回答用户。")
            content = "\n".join(part for part in content_parts if part).strip()
            if result.pending:
                pending_record = result.record
            _emit_enterprise_event(
                "wecom_media_intake",
                status="succeeded",
                stream_id=stream.stream_id,
                session_id=session_id,
                chat_id=chat_id,
                from_userid=from_userid,
                msgtype=data.get("msgtype"),
                file_id=result.record.file_id,
                mime_type=result.record.mime_type,
                size_bytes=result.record.size_bytes,
                extract_status=result.record.extract_status,
            )

        _emit_enterprise_event(
            "wecom_message_received",
            stream_id=stream.stream_id,
            session_id=session_id,
            chat_id=chat_id,
            from_userid=from_userid,
            userid=userid,
            resolved=bool(resolved_userid),
            msgtype=data.get("msgtype"),
            content_chars=len(content),
        )
        if pending_record is not None:
            self._schedule_pending_file_dispatch(
                stream_id=stream.stream_id,
                record=pending_record,
                data=data,
                session_id=session_id,
                chat_id=chat_id,
                from_userid=from_userid,
                resolved_userid=resolved_userid,
                userid=userid,
                leading_content=leading_content,
            )
            return await self._stream_response(stream.stream_id)
        message = ChannelMessage(
            session_id=session_id,
            channel=self.name,
            chat_id=chat_id,
            content=content,
            is_active=True,
            context=self._message_context(
                data=data,
                from_userid=from_userid,
                resolved_userid=resolved_userid,
                userid=userid,
                files_context=files_context,
            ),
        )
        setattr(message, _STREAM_ID_ATTR, stream.stream_id)
        self._schedule_receive(message)
        return await self._stream_response(stream.stream_id)

    async def _dispatch_user_message(self, data: dict[str, Any], content: str) -> str:
        from_userid = _extract_from_userid(data)
        # Queue admission and the first callback response must not wait for the
        # network-backed open-userid conversion. The session worker resolves the
        # plaintext userid before the message reaches the enterprise runtime.
        resolved_userid = None
        userid = from_userid
        session_id = f"wecom:{userid or 'unknown'}"
        chat_id = userid or session_id
        stream, is_duplicate = await self._get_or_create_stream_for_message(
            msgid=_extract_msgid(data),
            session_id=session_id,
            chat_id=chat_id,
            from_userid=from_userid,
            response_url=_extract_response_url(data),
        )
        if is_duplicate:
            logger.info("wecom.duplicate_msgid stream_id={}", stream.stream_id)
            _emit_enterprise_event(
                "wecom_duplicate_msgid",
                stream_id=stream.stream_id,
                session_id=session_id,
                chat_id=chat_id,
                from_userid=from_userid,
                msgtype=data.get("msgtype"),
            )
            return await self._stream_response(stream.stream_id)

        _emit_enterprise_event(
            "wecom_message_received",
            stream_id=stream.stream_id,
            session_id=session_id,
            chat_id=chat_id,
            from_userid=from_userid,
            userid=userid,
            resolved=bool(resolved_userid),
            msgtype=data.get("msgtype"),
            content_chars=len(content),
        )
        if content.strip() in _QUEUE_STATUS_COMMANDS:
            stream.update(content=self._queue_status_content(session_id), finish=True)
            _emit_enterprise_event(
                "wecom_turn_queue_status",
                status="succeeded",
                stream_id=stream.stream_id,
                session_id=session_id,
                active=self._session_worker_running(session_id),
                pending_count=self._pending_turn_counts.get(session_id, 0),
            )
            return await self._stream_response(stream.stream_id)
        message = ChannelMessage(
            session_id=session_id,
            channel=self.name,
            chat_id=chat_id,
            content=content,
            is_active=True,
            context=self._message_context(
                data=data,
                from_userid=from_userid,
                resolved_userid=resolved_userid,
                userid=userid,
            ),
        )
        setattr(message, _STREAM_ID_ATTR, stream.stream_id)
        setattr(message, _QUEUE_SESSION_ID_ATTR, session_id)
        setattr(message, _FROM_USERID_ATTR, from_userid)
        # Return the stream envelope quickly; slow tool/model work continues in
        # the channel manager task and is picked up by WeCom stream polls.
        self._schedule_receive(message)
        return await self._stream_response(stream.stream_id)

    async def _resolve_userid(self, from_userid: str | None) -> str | None:
        if not from_userid or self._userid_resolver is None:
            return None
        try:
            async with asyncio.timeout(max(0.05, self.settings.api_timeout_seconds)):
                return await asyncio.to_thread(self._userid_resolver.resolve, from_userid)
        except TimeoutError:
            logger.warning("wecom.userid_resolve timed out for open_userid={}", from_userid)
            return None
        except Exception as exc:
            logger.warning("wecom.userid_resolve failed for open_userid={}: {}", from_userid, exc)
            return None

    async def _handle_stream_poll(self, data: dict[str, Any]) -> str:
        stream_id = str((data.get("stream") or {}).get("id") or "")
        stream = await self._get_stream(stream_id)
        if stream is None:
            return make_text_stream(stream_id or uuid4().hex, "任务不存在或已过期", True)
        return make_text_stream(stream.stream_id, stream.content or "处理中，请稍候...", stream.finish)

    def _handle_event(self, data: dict[str, Any]) -> str | None:
        raw_event = data.get("event")
        event = raw_event if isinstance(raw_event, dict) else {}
        if event.get("eventtype") == "enter_chat":
            return make_text(self.settings.welcome_text)
        return None

    async def _create_stream(self, *, session_id: str, chat_id: str, from_userid: str | None) -> StreamReply:
        stream_id = uuid4().hex
        stream = StreamReply(
            stream_id=stream_id,
            session_id=session_id,
            chat_id=chat_id,
            from_userid=from_userid,
            content="已收到，正在处理...",
            finish=False,
        )
        async with self._lock:
            self._prune_streams_locked(time.time())
            self._streams[stream_id] = stream
        return stream

    async def _get_or_create_stream_for_message(
        self,
        *,
        msgid: str | None,
        session_id: str,
        chat_id: str,
        from_userid: str | None,
        response_url: str | None,
    ) -> tuple[StreamReply, bool]:
        stream = StreamReply(
            stream_id=uuid4().hex,
            session_id=session_id,
            chat_id=chat_id,
            from_userid=from_userid,
            response_url=response_url,
            content="已收到，正在处理...",
            finish=False,
        )
        async with self._lock:
            self._prune_streams_locked(time.time())
            if msgid:
                existing_id = self._stream_ids_by_msgid.get(msgid)
                existing = self._streams.get(existing_id or "")
                if existing is not None:
                    return existing, True
                self._stream_ids_by_msgid.pop(msgid, None)

            self._streams[stream.stream_id] = stream
            if msgid:
                self._stream_ids_by_msgid[msgid] = stream.stream_id
        _emit_enterprise_event(
            "wecom_stream_started",
            stream_id=stream.stream_id,
            session_id=session_id,
            chat_id=chat_id,
            from_userid=from_userid,
        )
        return stream, False

    async def _get_stream(self, stream_id: str) -> StreamReply | None:
        if not stream_id:
            return None
        async with self._lock:
            return self._streams.get(stream_id)

    async def _stream_response(self, stream_id: str) -> str:
        current = await self._get_stream(stream_id)
        if current is None or not current.response_url:
            await self._wait_for_first_update(stream_id)
        current = await self._get_stream(stream_id)
        if current is not None:
            if current.initial_response_content is None:
                current.initial_response_content = current.content or "已收到，正在处理..."
            current.initial_response_sent = True
            if current.response_url:
                # AI Bot clients render a plain text callback reliably, including
                # under burst traffic. Keep response_url for the one terminal reply.
                return make_text(current.initial_response_content)
        return make_text_stream(
            stream_id,
            (current.content if current else "") or "已收到，正在处理...",
            bool(current.finish if current else False),
        )

    def _schedule_receive(self, message: ChannelMessage) -> None:
        session_id = self._queue_session_id(message)
        queue = self._session_queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue()
            self._session_queues[session_id] = queue
        worker_running = self._session_worker_running(session_id)
        if worker_running:
            pending_count = self._pending_turn_counts.get(session_id, 0)
            max_pending = max(0, self.settings.session_queue_maxsize)
            if pending_count >= max_pending:
                self._finish_message_stream(
                    message,
                    content=(
                        f"当前有 1 条消息正在处理，另有 {pending_count} 条等待处理。"
                        "本条消息未进入队列，请等待前面的消息完成后再发送。"
                    ),
                    event="wecom_turn_queue_rejected",
                    status="rejected",
                )
                return
            queued = QueuedTurn(
                message=message,
                enqueued_at=time.monotonic(),
                pending_counted=True,
            )
            self._pending_turn_counts[session_id] = pending_count + 1
            queue.put_nowait(queued)
            self._schedule_queue_expiry(session_id, queued)
            self._update_message_stream(
                message,
                content=(
                    "已收到。当前有 1 条消息正在处理，"
                    f"你的消息排在等待队列第 {pending_count + 1} 位。"
                ),
            )
            _emit_enterprise_event(
                "wecom_turn_queued",
                stream_id=getattr(message, _STREAM_ID_ATTR, ""),
                session_id=session_id,
                pending_count=pending_count + 1,
                pending_limit=max_pending,
            )
            return

        queued = QueuedTurn(message=message, enqueued_at=time.monotonic())
        queue.put_nowait(queued)
        task = asyncio.create_task(
            self._run_session_queue(session_id),
            name=f"agentseek-wecom.session-worker.{session_id}",
        )
        self._session_workers[session_id] = task
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._on_dispatch_done)

    def _schedule_queue_expiry(self, session_id: str, queued: QueuedTurn) -> None:
        timeout = max(0.05, self.settings.queue_wait_timeout_seconds)
        handle = asyncio.get_running_loop().call_later(
            timeout,
            self._expire_queued_turn,
            session_id,
            queued,
        )
        queued.expiry_handle = handle
        self._queue_expiry_handles.add(handle)

    def _expire_queued_turn(self, session_id: str, queued: QueuedTurn) -> None:
        self._discard_expiry_handle(queued)
        if queued.started or queued.expired:
            return
        queued.expired = True
        self._release_pending_count(session_id, queued)
        self._finish_message_stream(
            queued.message,
            content="等待处理时间过长，本条消息已取消，请稍后重新发送。",
            event="wecom_turn_queue_wait_timeout",
            status="timeout",
        )

    def _discard_expiry_handle(self, queued: QueuedTurn) -> None:
        handle = queued.expiry_handle
        if handle is None:
            return
        handle.cancel()
        self._queue_expiry_handles.discard(handle)
        queued.expiry_handle = None

    def _release_pending_count(self, session_id: str, queued: QueuedTurn) -> None:
        if not queued.pending_counted:
            return
        queued.pending_counted = False
        remaining = max(0, self._pending_turn_counts.get(session_id, 0) - 1)
        if remaining:
            self._pending_turn_counts[session_id] = remaining
        else:
            self._pending_turn_counts.pop(session_id, None)

    def _session_worker_running(self, session_id: str) -> bool:
        worker = self._session_workers.get(session_id)
        return worker is not None and not worker.done()

    def _queue_status_content(self, session_id: str) -> str:
        active = self._session_worker_running(session_id)
        pending_count = self._pending_turn_counts.get(session_id, 0)
        if not active and pending_count == 0:
            return "当前没有正在处理或等待处理的消息。"
        lines = ["当前消息队列状态："]
        if active:
            started_at = self._active_turn_started_at.get(session_id)
            elapsed = max(0, round(time.monotonic() - started_at)) if started_at is not None else 0
            lines.append(f"- 正在处理：1 条，已运行约 {elapsed} 秒")
        else:
            lines.append("- 正在处理：0 条")
        lines.append(f"- 等待处理：{pending_count} 条")
        return "\n".join(lines)

    def _update_message_stream(self, message: ChannelMessage, *, content: str) -> None:
        stream_id = getattr(message, _STREAM_ID_ATTR, None)
        stream = self._streams.get(stream_id) if isinstance(stream_id, str) else None
        if stream is not None and not stream.finish:
            stream.update(content=content, finish=False)

    async def _run_session_queue(self, session_id: str) -> None:
        queue = self._session_queues[session_id]
        try:
            while True:
                try:
                    queued = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                self._discard_expiry_handle(queued)
                was_pending = queued.pending_counted
                self._release_pending_count(session_id, queued)
                if queued.expired:
                    queue.task_done()
                    continue
                queued.started = True
                self._active_turn_started_at[session_id] = time.monotonic()
                wait_ms = round((time.monotonic() - queued.enqueued_at) * 1000)
                if was_pending:
                    self._update_message_stream(queued.message, content="已进入处理，正在生成回复...")
                _emit_enterprise_event(
                    "wecom_turn_started",
                    stream_id=getattr(queued.message, _STREAM_ID_ATTR, ""),
                    session_id=session_id,
                    queue_wait_ms=wait_ms,
                    pending_count=self._pending_turn_counts.get(session_id, 0),
                )
                try:
                    await self._dispatch_one(queued.message)
                finally:
                    self._active_turn_started_at.pop(session_id, None)
                    queue.task_done()
        finally:
            current = asyncio.current_task()
            if self._session_workers.get(session_id) is current:
                self._session_workers.pop(session_id, None)
                self._session_queues.pop(session_id, None)
                self._pending_turn_counts.pop(session_id, None)

    async def _dispatch_one(self, message: ChannelMessage) -> None:
        started_at = time.monotonic()
        enqueue_timeout = min(max(0.05, self.settings.turn_timeout_seconds), 10.0)
        try:
            async with asyncio.timeout(enqueue_timeout):
                await self._hydrate_message_identity(message)
                await self._run_receive(message)
        except TimeoutError:
            self._finish_message_stream(
                message,
                content="消息进入处理队列超时，请稍后重试。",
                event="wecom_turn_enqueue_timeout",
                status="timeout",
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.opt(exception=exc).error("wecom.dispatch failed session_id={}", message.session_id)
            self._finish_message_stream(
                message,
                content="消息处理失败，请稍后重试。",
                event="wecom_dispatch_failed",
                status="error",
                error_type=type(exc).__name__,
            )
            return
        await self._wait_for_message_stream(message, started_at=started_at)

    async def _wait_for_message_stream(self, message: ChannelMessage, *, started_at: float) -> None:
        stream_id = getattr(message, _STREAM_ID_ATTR, None)
        if not isinstance(stream_id, str):
            return
        deadline = started_at + max(0.05, self.settings.turn_timeout_seconds)
        while time.monotonic() < deadline:
            stream = await self._get_stream(stream_id)
            if stream is None or stream.finish:
                return
            await asyncio.sleep(0.05)
        self._finish_message_stream(
            message,
            content="本次处理超时，请稍后重试。",
            event="wecom_turn_timeout",
            status="timeout",
        )

    def _finish_message_stream(
        self,
        message: ChannelMessage,
        *,
        content: str,
        event: str,
        status: str,
        error_type: str = "",
    ) -> None:
        stream_id = getattr(message, _STREAM_ID_ATTR, None)
        stream = self._streams.get(stream_id) if isinstance(stream_id, str) else None
        should_deliver = False
        if stream is not None and not stream.finish:
            stream.update(content=content, finish=True)
            should_deliver = bool(stream.response_url and stream.initial_response_sent)
        _emit_enterprise_event(
            event,
            status=status,
            stream_id=stream_id if isinstance(stream_id, str) else "",
            session_id=self._queue_session_id(message),
            pending_count=self._pending_turn_counts.get(self._queue_session_id(message), 0),
            error_type=error_type,
        )
        if should_deliver and stream is not None:
            self._schedule_response_url_delivery(stream, content)

    def _schedule_response_url_delivery(self, stream: StreamReply, content: str) -> None:
        task = asyncio.create_task(
            self._deliver_response_url_background(stream, content),
            name=f"agentseek-wecom.response-url.{stream.stream_id}",
        )
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._on_dispatch_done)

    async def _deliver_response_url_background(self, stream: StreamReply, content: str) -> None:
        await self._deliver_response_url_once(stream, content)

    async def _deliver_response_url_once(self, stream: StreamReply, content: str) -> str:
        response_url = stream.response_url
        if not response_url or stream.response_url_consumed:
            return "skipped"
        # response_url is a one-shot capability. Claim it before network I/O so
        # competing terminal paths can never send duplicate employee messages.
        stream.response_url_consumed = True
        try:
            await asyncio.to_thread(self._response_url_sender.send_markdown, response_url, content)
        except Exception as exc:
            logger.warning(
                "wecom.response_url delivery failed stream_id={} error_type={}",
                stream.stream_id,
                type(exc).__name__,
            )
            _emit_enterprise_event(
                "wecom_response_url_delivery",
                status="error",
                stream_id=stream.stream_id,
                session_id=stream.session_id,
                content_chars=len(content),
                error_type=type(exc).__name__,
            )
            return "delivery_error"
        _emit_enterprise_event(
            "wecom_response_url_delivery",
            status="succeeded",
            stream_id=stream.stream_id,
            session_id=stream.session_id,
            content_chars=len(content),
        )
        return "succeeded"

    @staticmethod
    def _queue_session_id(message: ChannelMessage) -> str:
        value = getattr(message, _QUEUE_SESSION_ID_ATTR, None)
        return value if isinstance(value, str) and value else message.session_id

    async def _hydrate_message_identity(self, message: ChannelMessage) -> None:
        from_userid = getattr(message, _FROM_USERID_ATTR, None)
        if not isinstance(from_userid, str) or not from_userid:
            return
        resolved_userid = await self._resolve_userid(from_userid)
        userid = resolved_userid or from_userid
        session_id = f"wecom:{userid}"
        message.session_id = session_id
        message.chat_id = userid
        message.context.update({
            "from_userid": from_userid,
            "userid": userid,
            "oa_account": userid,
            "chat_id": userid,
        })
        wecom = message.context.get("wecom")
        if isinstance(wecom, dict):
            wecom.update({
                "from_userid": from_userid,
                "open_userid": from_userid if resolved_userid else None,
                "resolved_userid": resolved_userid,
                "userid": userid,
            })
        stream_id = getattr(message, _STREAM_ID_ATTR, None)
        stream = self._streams.get(stream_id) if isinstance(stream_id, str) else None
        if stream is not None:
            stream.session_id = session_id
            stream.chat_id = userid

    async def _run_receive(self, message: ChannelMessage) -> None:
        if self._on_receive is None:
            logger.warning("wecom.receive handler is not bound")
            return
        await self._on_receive(message)

    def _on_dispatch_done(self, task: asyncio.Task[None]) -> None:
        self._dispatch_tasks.discard(task)
        if task.cancelled():
            return
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.opt(exception=exc).error("wecom.dispatch failed")
                _emit_enterprise_event("wecom_dispatch_failed", error_type=type(exc).__name__)

    async def _stream_for_outbound(self, message: ChannelMessage) -> StreamReply | None:
        stream_id = getattr(message, _STREAM_ID_ATTR, None)
        async with self._lock:
            if isinstance(stream_id, str) and stream_id in self._streams:
                return self._streams[stream_id]
            # The framework rebuilds outbound messages without the inbound stream id,
            # so a reply cannot be correlated to its stream by attribute. Turns are
            # serialized per session (admit_message), which means replies complete in
            # the same order their streams were created: route to the oldest still-open
            # stream for this session. Routing to the newest stream would write an
            # earlier turn's reply onto a later turn's stream and lose the later reply.
            for stream in self._streams.values():
                if stream.session_id == message.session_id and not stream.finish:
                    return stream
        return None

    async def _wait_for_first_update(self, stream_id: str) -> None:
        deadline = time.monotonic() + self.settings.initial_wait_seconds
        while time.monotonic() < deadline:
            stream = await self._get_stream(stream_id)
            if stream is None or stream.finish or stream.content != "已收到，正在处理...":
                return
            await asyncio.sleep(0.05)

    async def _prune_streams(self) -> None:
        now = time.time()
        async with self._lock:
            self._prune_streams_locked(now)

    def _prune_streams_locked(self, now: float) -> None:
        ttl = self.settings.cache_ttl_seconds
        expired = [stream_id for stream_id, stream in self._streams.items() if now - stream.created_at > ttl]
        for stream_id in expired:
            self._streams.pop(stream_id, None)
        if expired:
            expired_ids = set(expired)
            stale_msgids = [msgid for msgid, stream_id in self._stream_ids_by_msgid.items() if stream_id in expired_ids]
            for msgid in stale_msgids:
                self._stream_ids_by_msgid.pop(msgid, None)

    def _require_crypto(self) -> WeComJsonCrypto:
        if self._crypto is not None:
            return self._crypto
        self._crypto = self._build_crypto()
        return self._crypto

    def _build_crypto(self) -> WeComJsonCrypto:
        if not self.settings.token or not self.settings.encoding_aes_key:
            raise RuntimeError("WeCom token and EncodingAESKey are required")
        return WeComJsonCrypto(
            token=self.settings.token,
            encoding_aes_key=self.settings.encoding_aes_key,
            receive_id=self.settings.receive_id,
        )

    async def _wait_until_started(self) -> None:
        for _ in range(100):
            server = self._server
            if server is not None and server.started:
                return
            await asyncio.sleep(0.05)

    def _message_context(
        self,
        *,
        data: dict[str, Any],
        from_userid: str | None,
        resolved_userid: str | None,
        userid: str | None,
        files_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = {
            "from_userid": from_userid,
            "userid": userid,
            "oa_account": userid,
            "msgtype": data.get("msgtype"),
            "wecom": {
                "from_userid": from_userid,
                "open_userid": from_userid if resolved_userid else None,
                "resolved_userid": resolved_userid,
                "userid": userid,
                "msgtype": data.get("msgtype"),
                "raw": _safe_wecom_payload(data),
            },
        }
        if files_context:
            context["files"] = files_context
        return context

    async def _download_and_store_media(
        self,
        *,
        data: dict[str, Any],
        media: dict[str, str],
        session_id: str,
        chat_id: str,
        userid: str | None,
        from_userid: str | None,
    ) -> Any:
        media_client = self._get_media_client()
        if media_client is None:
            raise RuntimeError("WeCom media download requires AGENTSEEK_WECOM_CORP_ID and APP_SECRET")
        file_service = self._get_file_service()
        if file_service is None:
            raise RuntimeError("agentseek-files is not installed or AGENTSEEK_FILES_ENABLED is false")
        if media.get("url"):
            download = await asyncio.to_thread(
                media_client.download_media,
                media["url"],
                aes_key=decode_encoding_aes_key(self.settings.encoding_aes_key),
                fallback_filename=media["filename"],
                fallback_mime_type=media["mime_type"],
            )
        else:
            download = await asyncio.to_thread(
                media_client.download,
                media["media_id"],
                fallback_filename=media["filename"],
                fallback_mime_type=media["mime_type"],
            )
        scope = _file_scope(
            tenant_id=os.environ.get("AGENTSEEK_ENTERPRISE_TENANT_ID", "default"),
            employee_id=userid or from_userid or "unknown",
            session_id=session_id,
            channel=self.name,
            chat_id=chat_id,
            message_id=_extract_msgid(data),
        )
        return await file_service.handle_bytes(
            scope=scope,
            filename=download.filename,
            data=download.data,
            mime_type=download.mime_type,
        )

    def _get_media_client(self) -> MediaClient | None:
        if self._media_client is not None:
            return self._media_client
        self._media_client = WeComMediaClient.from_settings(self.settings)
        return self._media_client

    def _get_file_service(self) -> InboundFileServiceProtocol | None:
        if self._file_service_initialized:
            return self._file_service
        self._file_service_initialized = True
        try:
            from agentseek_files.inbound import InboundFileService
            from agentseek_files.settings import FilesSettings
        except ImportError:
            logger.warning("wecom.file_intake disabled: agentseek-files is not installed")
            self._file_service = None
            return None
        settings = FilesSettings.from_env()
        if not settings.enabled:
            logger.info("wecom.file_intake disabled: AGENTSEEK_FILES_ENABLED is false")
            self._file_service = None
            return None
        self._file_service = InboundFileService(settings)
        return self._file_service

    def _schedule_pending_file_dispatch(
        self,
        *,
        stream_id: str,
        record: Any,
        data: dict[str, Any],
        session_id: str,
        chat_id: str,
        from_userid: str | None,
        resolved_userid: str | None,
        userid: str | None,
        leading_content: str,
    ) -> None:
        task = asyncio.create_task(
            self._poll_pending_file_and_dispatch(
                stream_id=stream_id,
                record=record,
                data=data,
                session_id=session_id,
                chat_id=chat_id,
                from_userid=from_userid,
                resolved_userid=resolved_userid,
                userid=userid,
                leading_content=leading_content,
            ),
            name=f"agentseek-wecom.file-poll.{stream_id}",
        )
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._on_dispatch_done)

    async def _poll_pending_file_and_dispatch(
        self,
        *,
        stream_id: str,
        record: Any,
        data: dict[str, Any],
        session_id: str,
        chat_id: str,
        from_userid: str | None,
        resolved_userid: str | None,
        userid: str | None,
        leading_content: str,
    ) -> None:
        file_service = self._get_file_service()
        if file_service is None:
            return
        settings = getattr(file_service, "settings", None)
        timeout_s = float(getattr(settings, "mineru_poll_timeout_s", 300.0) or 300.0)
        interval_s = max(0.5, float(getattr(settings, "mineru_poll_interval_s", 2.0) or 2.0))
        deadline = time.monotonic() + max(timeout_s, interval_s)
        current_record = record
        while True:
            try:
                result = await file_service.poll_pending(current_record)
            except Exception as exc:
                logger.warning("wecom.file_poll failed file_id={} error={}", getattr(record, "file_id", ""), exc)
                _emit_enterprise_event(
                    "wecom_file_extract_finished",
                    status="error",
                    stream_id=stream_id,
                    file_id=getattr(record, "file_id", ""),
                    error_type=type(exc).__name__,
                )
                stream = await self._get_stream(stream_id)
                if stream is not None:
                    stream.update(content="文件解析失败，请稍后重试。", finish=True)
                return
            current_record = result.record
            if result.record.extract_status not in {"pending", "running"} or time.monotonic() >= deadline:
                break
            await asyncio.sleep(interval_s)

        _emit_enterprise_event(
            "wecom_file_extract_finished",
            status=result.record.extract_status,
            stream_id=stream_id,
            file_id=result.record.file_id,
            extract_chars=result.record.extract_chars,
        )
        content_parts = [leading_content] if leading_content else []
        content_parts.append(result.user_notice)
        if result.context_block:
            content_parts.append("请结合当前文件上下文回答用户。")
        message = ChannelMessage(
            session_id=session_id,
            channel=self.name,
            chat_id=chat_id,
            content="\n".join(part for part in content_parts if part).strip(),
            is_active=True,
            context=self._message_context(
                data=data,
                from_userid=from_userid,
                resolved_userid=resolved_userid,
                userid=userid,
                files_context=result.to_context(),
            ),
        )
        setattr(message, _STREAM_ID_ATTR, stream_id)
        await self._run_receive(message)


def _extract_from_userid(data: dict[str, Any]) -> str | None:
    raw_from = data.get("from")
    if isinstance(raw_from, dict):
        value = raw_from.get("userid")
        if value:
            return str(value)
    for key in ("from_userid", "userid", "FromUserName"):
        value = data.get(key)
        if value:
            return str(value)
    return None


def _extract_response_url(data: dict[str, Any]) -> str | None:
    value = data.get("responseurl") or data.get("response_url")
    return str(value).strip() if value else None


def _file_error_user_notice(exc: Exception) -> str | None:
    notice = getattr(exc, "user_notice", None)
    if not isinstance(notice, str):
        return None
    notice = notice.strip()
    return notice if notice and len(notice) <= 200 else None


def _extract_msgid(data: dict[str, Any]) -> str | None:
    value = data.get("msgid")
    if value:
        return str(value)
    return None


def _extract_media_id(data: dict[str, Any]) -> str | None:
    media = _extract_legacy_media_info(data)
    return media["media_id"] if media is not None else None


def _extract_media_items(data: dict[str, Any]) -> list[dict[str, str]]:
    msgtype = str(data.get("msgtype") or "")
    if msgtype == "mixed":
        return _mixed_media_items(data)
    item = _extract_ai_bot_media(data)
    if item is not None:
        return [item]
    legacy = _extract_legacy_media_info(data)
    return [legacy] if legacy is not None else []


def _extract_ai_bot_media(data: dict[str, Any]) -> dict[str, str] | None:
    msgtype = str(data.get("msgtype") or "")
    payload = data.get(msgtype)
    if not isinstance(payload, dict):
        return None
    url = _first_text(payload, "url")
    if not url:
        return None
    # AI Bot signed URLs identify opaque encrypted objects, not user-facing filenames.
    # Leave the fallback empty so the downloader can name the file after inspecting
    # the decrypted bytes and response Content-Type.
    filename = _first_text(payload, "filename", "file_name", "name") or ""
    mime_type = _first_text(payload, "mime_type", "mimetype", "content_type") or _default_media_mime_type(msgtype)
    return {"url": url, "filename": filename, "mime_type": mime_type, "kind": msgtype}


def _extract_legacy_media_info(data: dict[str, Any]) -> dict[str, str] | None:
    msgtype = str(data.get("msgtype") or "")
    payload = data.get(msgtype)
    if not isinstance(payload, dict):
        for key in ("file", "image", "voice", "video"):
            candidate = data.get(key)
            if isinstance(candidate, dict):
                payload = candidate
                break
        else:
            return None

    media_id = _first_text(payload, "media_id", "mediaid", "file_id", "fileid")
    if not media_id:
        return None
    filename = _first_text(payload, "filename", "file_name", "name") or _default_media_filename(
        msgtype,
        str(data.get("msgid") or media_id),
    )
    mime_type = _first_text(payload, "mime_type", "mimetype", "content_type") or _default_media_mime_type(msgtype)
    return {"media_id": media_id, "filename": filename, "mime_type": mime_type, "kind": msgtype}


def _mixed_text_content(data: dict[str, Any]) -> str:
    items = _mixed_items(data)
    parts: list[str] = []
    for item in items:
        if str(item.get("msgtype") or "") != "text":
            continue
        text = item.get("text")
        if isinstance(text, dict) and text.get("content") is not None:
            parts.append(str(text["content"]))
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _mixed_media_items(data: dict[str, Any]) -> list[dict[str, str]]:
    media_items: list[dict[str, str]] = []
    for item in _mixed_items(data):
        msgtype = str(item.get("msgtype") or "")
        if msgtype not in {"image", "file", "video"}:
            continue
        media = _extract_ai_bot_media(item) or _extract_legacy_media_info(item)
        if media is not None:
            media_items.append(media)
    return media_items


def _mixed_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    mixed = data.get("mixed")
    if not isinstance(mixed, dict):
        return []
    items = mixed.get("msg_item")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _first_text(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    return None


def _default_media_filename(msgtype: str, identifier: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in identifier)[:32] or "upload"
    extension = {
        "image": ".jpg",
        "voice": ".amr",
        "video": ".mp4",
    }.get(msgtype, ".bin")
    return f"{msgtype or 'file'}_{clean}{extension}"


def _default_media_mime_type(msgtype: str) -> str:
    return {
        "image": "image/jpeg",
        "voice": "audio/amr",
        "video": "video/mp4",
    }.get(msgtype, "application/octet-stream")


def _msgtype_label(msgtype: object) -> str:
    return {
        "file": "文件",
        "image": "图片",
        "voice": "语音",
        "video": "视频",
    }.get(str(msgtype or ""), "媒体")


def _file_scope(
    *,
    tenant_id: str,
    employee_id: str,
    session_id: str,
    channel: str,
    chat_id: str | None,
    message_id: str | None,
) -> Any:
    try:
        from agentseek_enterprise.runtime import EnterpriseRuntimeSettings
        from agentseek_files.models import FileScope
    except ImportError:
        from agentseek_files.models import FileScope
        from agentseek_files.scope import hmac_key

        secret = os.environ.get("AGENTSEEK_ENTERPRISE_NAMESPACE_SECRET", "")
        return FileScope(
            tenant_key=hmac_key(f"tenant:{tenant_id}", secret=secret or "agentseek-files"),
            employee_key=hmac_key(f"employee:{employee_id}", secret=secret or "agentseek-files"),
            session_key=hmac_key(f"session:{session_id}", secret=secret or "agentseek-files"),
            channel=channel,
            chat_id=hmac_key(f"chat:{chat_id}", secret=secret or "agentseek-files") if chat_id else None,
            message_id=hmac_key(f"message:{message_id}", secret=secret or "agentseek-files") if message_id else None,
        )

    settings = EnterpriseRuntimeSettings.from_env()
    return FileScope(
        tenant_key=settings.scoped_key("tenant", tenant_id),
        employee_key=settings.scoped_key("employee", employee_id),
        session_key=settings.scoped_key("session", session_id),
        channel=channel,
        chat_id=settings.scoped_key("chat", chat_id) if chat_id else None,
        message_id=settings.scoped_key("message", message_id) if message_id else None,
    )


def _safe_wecom_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Return the prompt-safe subset of a WeCom callback payload."""

    safe: dict[str, Any] = {}
    for key in ("msgid", "aibotid", "chattype", "msgtype"):
        value = data.get(key)
        if value:
            safe[key] = value

    raw_from = data.get("from")
    if isinstance(raw_from, dict) and raw_from.get("userid"):
        safe["from"] = {"userid": str(raw_from["userid"])}

    msgtype = str(data.get("msgtype") or "")
    if msgtype == "text":
        text = data.get("text")
        if isinstance(text, dict) and text.get("content") is not None:
            safe["text"] = {"content": str(text["content"])}
    elif msgtype in {"file", "image", "voice", "video"}:
        payload = data.get(msgtype)
        if isinstance(payload, dict):
            safe_media: dict[str, Any] = {
                "has_url": bool(_extract_ai_bot_media(data)),
                "has_media_id": bool(_extract_media_id(data)),
            }
            for key in ("filename", "file_name", "size", "filesize", "mime_type", "content_type"):
                if key in payload:
                    safe_media[key] = payload[key]
            if msgtype == "voice" and payload.get("content") is not None:
                safe_media["content"] = str(payload["content"])
            safe[msgtype] = safe_media
    elif msgtype == "mixed":
        items: list[dict[str, Any]] = []
        for item in _mixed_items(data):
            item_type = str(item.get("msgtype") or "")
            if item_type == "text":
                text = item.get("text")
                if isinstance(text, dict) and text.get("content") is not None:
                    items.append({"msgtype": "text", "text": {"content": str(text["content"])}})
            elif item_type in {"image", "file", "video"}:
                items.append({"msgtype": item_type, item_type: {"has_url": bool(_extract_ai_bot_media(item))}})
        safe["mixed"] = {"msg_item": items}
    elif msgtype == "event":
        event = data.get("event")
        if isinstance(event, dict):
            safe_event = {key: event[key] for key in ("eventtype",) if key in event}
            if safe_event:
                safe["event"] = safe_event

    return safe


def _emit_enterprise_event(event: str, **fields: Any) -> None:
    try:
        from agentseek_enterprise.observability import emit_enterprise_event
    except ImportError:  # pragma: no cover - agentseek-wecom can be installed without enterprise extras.
        return
    emit_enterprise_event(event, **fields)
