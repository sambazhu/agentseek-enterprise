from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from collections.abc import AsyncGenerator, AsyncIterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote, urlparse
from uuid import uuid4

from bub.channels.base import Channel
from bub.channels.message import ChannelMessage
from bub.envelope import content_of
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from republic import StreamEvent

from agentseek_wecom.addressing import ConversationAddress
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.durable import (
    DurableMessageStore,
    InboxRecord,
    InboxStatus,
    OutboxRecord,
    OutboxStatus,
    SqliteDurableMessageStore,
)
from agentseek_wecom.media import MediaDownload, WeComMediaClient, decode_encoding_aes_key
from agentseek_wecom.messages import make_text, make_text_stream
from agentseek_wecom.outbound import (
    ArtifactDownloadGone,
    ArtifactDownloadNotFound,
    has_template_card_control_instruction,
    has_template_card_intent_marker,
    resolve_artifact_download,
    take_template_card_intent,
    validate_artifact_download_base_url,
)
from agentseek_wecom.response_url import WeComResponseUrlSender
from agentseek_wecom.transport import WeComTransport
from agentseek_wecom.transports.callback import AiBotCallbackTransport
from agentseek_wecom.userid_resolver import UseridResolver, make_userid_resolver

_STREAM_ID_ATTR = "_agentseek_wecom_stream_id"
_QUEUE_SESSION_ID_ATTR = "_agentseek_wecom_queue_session_id"
_FROM_USERID_ATTR = "_agentseek_wecom_from_userid"
_CONVERSATION_ADDRESS_ATTR = "_agentseek_wecom_conversation_address"
_DURABLE_INBOX_ID_ATTR = "_agentseek_wecom_durable_inbox_id"
_DURABLE_RECOVERY_ATTR = "_agentseek_wecom_durable_recovery"
_DURABLE_REPLY_DEADLINE_ATTR = "_agentseek_wecom_durable_reply_deadline"
_DURABLE_STREAM_ID_ATTR = "_agentseek_wecom_durable_stream_id"
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

    def send_template_card(
        self,
        response_url: str,
        template_card: Mapping[str, Any],
    ) -> None: ...


@dataclass
class StreamReply:
    stream_id: str
    session_id: str
    chat_id: str
    from_userid: str | None
    inbox_id: str | None = None
    reply_deadline: datetime | None = None
    response_url: str | None = None
    initial_response_sent: bool = False
    initial_response_content: str | None = None
    initial_response_finish: bool | None = None
    deferred_response_url: bool = False
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
        transport: WeComTransport | None = None,
        durable_store: DurableMessageStore | None = None,
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
        self._lock = asyncio.Lock()
        self._durable_init_lock = asyncio.Lock()
        self._durable_store = durable_store
        self._durable_store_initialized = durable_store is not None or settings.durable_mode == "memory"
        self._durable_owner = f"{os.getpid()}-{uuid4().hex}"
        self._durable_recovery_task: asyncio.Task[None] | None = None
        self._streams: dict[str, StreamReply] = {}
        self._stream_ids_by_msgid: dict[str, str] = {}
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._session_queues: dict[str, asyncio.Queue[QueuedTurn]] = {}
        self._session_workers: dict[str, asyncio.Task[None]] = {}
        self._active_turn_started_at: dict[str, float] = {}
        self._pending_turn_counts: dict[str, int] = {}
        self._queue_expiry_handles: set[asyncio.TimerHandle] = set()
        self._transport = transport or AiBotCallbackTransport(
            settings=settings,
            tenant_id=os.environ.get("AGENTSEEK_ENTERPRISE_TENANT_ID", "default"),
        )
        self._transport.bind_inbound(self._handle_plain_message)
        app = self._transport.app
        self.app = app
        if app is not None:
            self._register_artifact_routes(app)
        elif settings.artifact_delivery_mode == "signed_link":
            raise RuntimeError("signed-link artifact delivery requires a WeCom transport with an ASGI application")

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @property
    def transport(self) -> WeComTransport:
        return self._transport

    def bind_receiver(self, on_receive: Any) -> None:
        self._on_receive = on_receive

    async def start(self, stop_event: asyncio.Event) -> None:
        store = await self._ensure_durable_store()
        await self._recover_durable_messages()
        if store is not None:
            self._durable_recovery_task = asyncio.create_task(
                self._run_durable_recovery_loop(stop_event),
                name="agentseek-wecom.durable-recovery",
            )
        try:
            await self._transport.start(stop_event)
        except BaseException:
            await self._stop_durable_recovery_loop()
            raise

    async def stop(self) -> None:
        await self._stop_durable_recovery_loop()
        await self._transport.stop()
        await self._drain_dispatch_tasks()
        self._dispatch_tasks.clear()
        for handle in self._queue_expiry_handles:
            handle.cancel()
        self._queue_expiry_handles.clear()
        self._session_workers.clear()
        self._session_queues.clear()
        self._active_turn_started_at.clear()
        self._pending_turn_counts.clear()
        if self._durable_store is not None:
            await asyncio.to_thread(
                self._durable_store.release_owner,
                self._durable_owner,
                now=datetime.now(UTC),
            )

    async def _drain_dispatch_tasks(self) -> None:
        timeout = max(0.1, self.settings.shutdown_timeout_seconds)
        deadline = asyncio.get_running_loop().time() + timeout
        current = asyncio.current_task()
        while True:
            dispatch_tasks = [task for task in self._dispatch_tasks if task is not current and not task.done()]
            if not dispatch_tasks:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            _, pending = await asyncio.wait(dispatch_tasks, timeout=remaining)
            if pending:
                break

        pending = [task for task in self._dispatch_tasks if task is not current and not task.done()]
        for dispatch_task in pending:
            dispatch_task.cancel()
        if pending:
            _, still_pending = await asyncio.wait(pending, timeout=min(1.0, timeout))
            logger.warning(
                "wecom.dispatch graceful shutdown timed out; cancelled={} still_pending={}",
                len(pending),
                len(still_pending),
            )

    async def send(self, message: ChannelMessage) -> None:
        stream = await self._stream_for_outbound(message)
        if stream is None:
            return
        stream.update(content=content_of(message), finish=True)
        delivery_status = "succeeded"
        if stream.deferred_response_url:
            delivery_status = await self._deliver_response_url_once(stream, stream.content)
        elif stream.inbox_id:
            await self._mark_inbox(stream.inbox_id, "completed")
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
                        if reply.inbox_id:
                            await self._mark_inbox(reply.inbox_id, "failed", error_type="stream_error")
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

    def _register_artifact_routes(self, app: FastAPI) -> None:
        if self.settings.artifact_delivery_mode == "signed_link":
            base_url = validate_artifact_download_base_url(self.settings.artifact_public_base_url)
            download_path = urlparse(base_url).path.rstrip("/")

            @app.get(f"{download_path}/{{delivery_id}}")
            async def artifact_download_page(delivery_id: str) -> Response:
                if not re.fullmatch(r"delivery_[a-f0-9]{64}", delivery_id):
                    raise HTTPException(status_code=404, detail="download grant not found")
                nonce = uuid4().hex
                body = _artifact_redemption_page(nonce=nonce)
                return Response(
                    content=body,
                    media_type="text/html",
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Security-Policy": (
                            "default-src 'none'; connect-src 'self'; "
                            f"script-src 'nonce-{nonce}'; style-src 'unsafe-inline'"
                        ),
                        "Referrer-Policy": "no-referrer",
                        "X-Content-Type-Options": "nosniff",
                    },
                )

            @app.post(f"{download_path}/{{delivery_id}}/redeem")
            async def redeem_artifact_download(delivery_id: str, request: Request) -> Response:
                token_bytes = await request.body()
                if not token_bytes or len(token_bytes) > 512:
                    raise HTTPException(status_code=404, detail="download grant not found")
                try:
                    grant_token = token_bytes.decode("ascii")
                    download = await asyncio.to_thread(
                        resolve_artifact_download,
                        delivery_id,
                        grant_token,
                    )
                except (UnicodeDecodeError, ArtifactDownloadNotFound) as exc:
                    raise HTTPException(status_code=404, detail="download grant not found") from exc
                except ArtifactDownloadGone as exc:
                    raise HTTPException(status_code=410, detail="download grant is no longer active") from exc
                filename = download.filename.replace('"', "").replace("\r", "").replace("\n", "")
                disposition = (
                    f'attachment; filename="report.docx"; '
                    f"filename*=UTF-8''{quote(filename, safe='')}"
                )
                return Response(
                    content=download.data,
                    media_type=download.media_type,
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Disposition": disposition,
                        "X-Content-Type-Options": "nosniff",
                    },
                )

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
        card_trigger = self.settings.response_url_template_card_probe_trigger
        if card_trigger and content == card_trigger:
            return await self._handle_response_url_template_card_probe(data)
        return await self._dispatch_user_message(data, _append_quote_context(data, content))

    async def _handle_response_url_probe(self, data: dict[str, Any]) -> str:
        from_userid = _extract_from_userid(data)
        conversation = self._conversation_address(data)
        stream = await self._create_stream(
            session_id=conversation.session_id,
            chat_id=conversation.chat_id,
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

    async def _handle_response_url_template_card_probe(self, data: dict[str, Any]) -> str:
        from_userid = _extract_from_userid(data)
        conversation = self._conversation_address(data)
        stream = await self._create_stream(
            session_id=conversation.session_id,
            chat_id=conversation.chat_id,
            from_userid=from_userid,
        )
        response_url = _extract_response_url(data)
        if not response_url:
            stream.update(content="模板卡片探针失败：回调未包含 response_url。", finish=True)
            return await self._stream_response(stream.stream_id)

        stream.update(content="模板卡片探针已启动，请等待第二条消息。", finish=True)
        task = asyncio.create_task(
            self._run_response_url_template_card_probe(response_url),
            name=f"agentseek-wecom.response-url-template-card-probe.{stream.stream_id}",
        )
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._on_dispatch_done)
        return await self._stream_response(stream.stream_id)

    async def _run_response_url_template_card_probe(self, response_url: str) -> None:
        await asyncio.sleep(max(self.settings.response_url_probe_delay_seconds, 0.0))
        card = {
            "card_type": "text_notice",
            "main_title": {
                "title": "AgentSeek M4-00 出站协议探针",
                "desc": "AI Bot response_url 模板卡片发送成功",
            },
            "sub_title_text": "当前回调模式将使用模板卡片承载受控 Artifact 下载链接。",
            "card_action": {
                "type": 1,
                "url": "https://developer.work.weixin.qq.com/document/path/101138",
            },
        }
        try:
            await asyncio.to_thread(
                self._response_url_sender.send_template_card,
                response_url,
                card,
            )
        except Exception as exc:
            logger.warning("wecom.response_url_template_card_probe failed error_type={}", type(exc).__name__)
            _emit_enterprise_event(
                "wecom_response_url_template_card_probe",
                status="error",
                error_type=type(exc).__name__,
            )
        else:
            _emit_enterprise_event("wecom_response_url_template_card_probe", status="succeeded")

    async def _handle_voice(self, data: dict[str, Any]) -> str:
        content = str((data.get("voice") or {}).get("content") or "")
        if not content:
            content = "用户发送了一条语音消息，但回调未包含转写内容。"
        return await self._dispatch_user_message(data, _append_quote_context(data, content))

    async def _handle_mixed(self, data: dict[str, Any]) -> str:
        content = _mixed_text_content(data)
        if _extract_media_items(data):
            return await self._handle_media_message(data, fallback_content=content)
        content = _append_quote_context(data, content)
        return await self._dispatch_user_message(data, content or "用户发送了一条图文混排消息。")

    async def _handle_media_message(self, data: dict[str, Any], *, fallback_content: str = "") -> str:
        media_items = _extract_media_items(data)
        content = _append_quote_context(data, fallback_content.strip())
        if not media_items:
            content = content or f"用户发送了 {data.get('msgtype') or 'media'} 消息，但回调未包含可下载 URL。"
            return await self._dispatch_user_message(data, content)

        from_userid = _extract_from_userid(data)
        resolved_userid = await self._resolve_userid(from_userid)
        userid = resolved_userid or from_userid
        conversation = self._conversation_address(data, plaintext_userid=resolved_userid)
        session_id = conversation.session_id
        chat_id = conversation.chat_id
        stream, is_duplicate = await self._get_or_create_stream_for_message(
            msgid=_extract_msgid(data),
            data=data,
            address=conversation,
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
                address=conversation,
            )
            if stream.response_url:
                return await self._commit_stream_response(stream, force_deferred=True)
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
                chat_id=chat_id,
                files_context=files_context,
                address=conversation,
            ),
        )
        setattr(message, _STREAM_ID_ATTR, stream.stream_id)
        setattr(message, _CONVERSATION_ADDRESS_ATTR, conversation)
        self._schedule_receive(message)
        if stream.response_url:
            return await self._commit_stream_response(
                stream,
                force_deferred=_is_durable_recovery(data),
            )
        return await self._stream_response(stream.stream_id)

    async def _dispatch_user_message(self, data: dict[str, Any], content: str) -> str:
        from_userid = _extract_from_userid(data)
        # Queue admission and the first callback response must not wait for the
        # network-backed open-userid conversion. The session worker resolves the
        # plaintext userid before the message reaches the enterprise runtime.
        resolved_userid = None
        userid = from_userid
        conversation = self._conversation_address(data)
        session_id = conversation.session_id
        chat_id = conversation.chat_id
        stream, is_duplicate = await self._get_or_create_stream_for_message(
            msgid=_extract_msgid(data),
            data=data,
            address=conversation,
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
            return await self._commit_stream_response(stream)
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
                chat_id=chat_id,
                address=conversation,
            ),
        )
        setattr(message, _STREAM_ID_ATTR, stream.stream_id)
        setattr(message, _QUEUE_SESSION_ID_ATTR, session_id)
        setattr(message, _FROM_USERID_ATTR, from_userid)
        setattr(message, _CONVERSATION_ADDRESS_ATTR, conversation)
        # Fast turns finish in this callback. Slow turns receive one terminal
        # acknowledgement here and deliver their eventual result through the
        # callback's one-shot response_url.
        self._schedule_receive(message)
        if stream.response_url:
            return await self._commit_stream_response(
                stream,
                force_deferred=_is_durable_recovery(data),
            )
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
        data: dict[str, Any],
        address: ConversationAddress,
        session_id: str,
        chat_id: str,
        from_userid: str | None,
        response_url: str | None,
    ) -> tuple[StreamReply, bool]:
        recovery_stream_id = data.get(_DURABLE_STREAM_ID_ATTR)
        stream = StreamReply(
            stream_id=str(recovery_stream_id) if recovery_stream_id else uuid4().hex,
            session_id=session_id,
            chat_id=chat_id,
            from_userid=from_userid,
            inbox_id=str(data.get(_DURABLE_INBOX_ID_ATTR) or "") or None,
            reply_deadline=_durable_reply_deadline(data) or address.reply_deadline,
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

            if msgid and not _is_durable_recovery(data):
                store = await self._ensure_durable_store()
                if store is not None:
                    admission = await asyncio.to_thread(
                        store.admit_inbound,
                        message_id=msgid,
                        address=address,
                        stream_id=stream.stream_id,
                        payload=data,
                        now=datetime.now(UTC),
                    )
                    stream.inbox_id = admission.record.inbox_id
                    stream.reply_deadline = admission.record.reply_deadline
                    if not admission.admitted:
                        stream.stream_id = admission.record.stream_id
                        stream.content = (
                            "该消息已接收，正在恢复处理，请勿重复发送。"
                            if admission.record.status in {"pending", "processing", "failed"}
                            else "该消息已经处理，请勿重复发送。"
                        )
                        stream.finish = True
                        self._streams[stream.stream_id] = stream
                        self._stream_ids_by_msgid[msgid] = stream.stream_id
                        return stream, True
                    claimed = await asyncio.to_thread(
                        store.claim_inbox,
                        admission.record.inbox_id,
                        now=datetime.now(UTC),
                        owner=self._durable_owner,
                        lease_duration=self._durable_lease_duration(),
                    )
                    if claimed is None:
                        stream.content = "该消息已由另一实例接收，正在处理，请勿重复发送。"
                        stream.finish = True
                        self._streams[stream.stream_id] = stream
                        self._stream_ids_by_msgid[msgid] = stream.stream_id
                        return stream, True

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
        if current is None:
            return make_text_stream(stream_id, "任务不存在或已过期", True)
        if current.initial_response_sent:
            return make_text_stream(
                stream_id,
                current.initial_response_content or "已收到，正在处理...",
                bool(current.initial_response_finish),
            )

        # Give genuinely fast turns a short chance to finish so they need only
        # one callback reply. The decision below is synchronous: once the
        # callback claims the final response or deferred response_url path,
        # outbound completion cannot race into both channels.
        if not current.finish and not current.response_url:
            await self._wait_for_first_update(stream_id)
        current = await self._get_stream(stream_id)
        if current is None:
            return make_text_stream(stream_id, "任务不存在或已过期", True)
        if current.initial_response_sent:
            return make_text_stream(
                stream_id,
                current.initial_response_content or "已收到，正在处理...",
                bool(current.initial_response_finish),
            )

        return await self._commit_stream_response(current)

    async def _commit_stream_response(self, current: StreamReply, *, force_deferred: bool = False) -> str:
        if current.initial_response_sent:
            return make_text_stream(
                current.stream_id,
                current.initial_response_content or "已收到，正在处理...",
                bool(current.initial_response_finish),
            )

        initial_content = current.content or "已收到，正在处理..."
        if force_deferred and current.response_url:
            current.deferred_response_url = True
            initial_finish = True
        elif current.finish:
            # The callback owns this completed result. Clear response_url so a
            # later retry or terminal hook cannot deliver the same text twice.
            current.response_url = None
            initial_finish = True
        elif current.response_url:
            # AI Bot clients do not reliably render finish=false updates. End
            # the callback stream with a visible acknowledgement, then use this
            # message's response_url exactly once for the eventual terminal text.
            current.deferred_response_url = True
            initial_finish = True
        else:
            # Compatibility path for callbacks without response_url.
            initial_finish = False

        current.initial_response_content = initial_content
        current.initial_response_finish = initial_finish
        current.initial_response_sent = True

        # Completion can land after the wait loop but immediately before this
        # delivery decision. If the deferred path won, schedule its already
        # available terminal result now.
        if current.deferred_response_url and current.finish:
            self._schedule_response_url_delivery(current, current.content)
        elif current.finish and current.inbox_id:
            await self._mark_inbox(current.inbox_id, "completed")
        return make_text_stream(current.stream_id, initial_content, initial_finish)

    async def _ensure_durable_store(self) -> DurableMessageStore | None:
        if self._durable_store_initialized:
            return self._durable_store
        async with self._durable_init_lock:
            if self._durable_store_initialized:
                return self._durable_store
            self._durable_store = await asyncio.to_thread(
                SqliteDurableMessageStore,
                path=self.settings.durable_sqlite_path,
                secret=self.settings.durable_secret,
            )
            self._durable_store_initialized = True
            logger.info("wecom.durable_store initialized mode=sqlite")
        return self._durable_store

    async def _recover_durable_messages(self) -> None:
        store = await self._ensure_durable_store()
        if store is None:
            return
        now = datetime.now(UTC)
        lease = self._durable_lease_duration()
        limit = self.settings.durable_recovery_limit
        outbox_records = await asyncio.to_thread(
            store.claim_recoverable_outbox,
            now=now,
            owner=self._durable_owner,
            lease_duration=lease,
            limit=limit,
        )
        for record in outbox_records:
            await self._recover_outbox(record)
        inbox_records = await asyncio.to_thread(
            store.claim_recoverable_inbox,
            now=datetime.now(UTC),
            owner=self._durable_owner,
            lease_duration=lease,
            limit=limit,
        )
        for record in inbox_records:
            await self._recover_inbox(record)
        if outbox_records or inbox_records:
            _emit_enterprise_event(
                "wecom_durable_recovery",
                status="scheduled",
                outbox_count=len(outbox_records),
                inbox_count=len(inbox_records),
            )

    async def _run_durable_recovery_loop(self, stop_event: asyncio.Event) -> None:
        interval = self.settings.durable_recovery_interval_seconds
        while not stop_event.is_set():
            try:
                async with asyncio.timeout(interval):
                    await stop_event.wait()
            except TimeoutError:
                try:
                    await self._recover_durable_messages()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("wecom.durable periodic recovery failed error_type={}", type(exc).__name__)

    async def _stop_durable_recovery_loop(self) -> None:
        task = self._durable_recovery_task
        self._durable_recovery_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _recover_inbox(self, record: InboxRecord) -> None:
        response_url = _extract_response_url(record.payload)
        if not response_url:
            await self._mark_inbox(record.inbox_id, "blocked", error_type="reply_capability_missing")
            return
        data = dict(record.payload)
        data[_DURABLE_RECOVERY_ATTR] = True
        data[_DURABLE_INBOX_ID_ATTR] = record.inbox_id
        data[_DURABLE_STREAM_ID_ATTR] = record.stream_id
        if record.reply_deadline is not None:
            data[_DURABLE_REPLY_DEADLINE_ATTR] = record.reply_deadline.isoformat()
        try:
            await self._handle_plain_message(data)
        except Exception as exc:
            await self._mark_inbox(record.inbox_id, "failed", error_type=type(exc).__name__)
            logger.warning("wecom.durable inbox recovery failed error_type={}", type(exc).__name__)

    async def _recover_outbox(self, record: OutboxRecord) -> None:
        if record.message_type != "markdown":
            await self._mark_outbox(record.outbox_id, "blocked", error_type="manual_reconciliation_required")
            if record.inbox_id:
                await self._mark_inbox(record.inbox_id, "blocked", error_type="manual_reconciliation_required")
            return
        response_url = record.envelope.get("response_url")
        content = record.envelope.get("content")
        if not isinstance(response_url, str) or not response_url or not isinstance(content, str):
            await self._mark_outbox(record.outbox_id, "blocked", error_type="invalid_envelope")
            if record.inbox_id:
                await self._mark_inbox(record.inbox_id, "blocked", error_type="invalid_envelope")
            return
        try:
            await asyncio.to_thread(self._response_url_sender.send_markdown, response_url, content)
        except Exception as exc:
            await self._mark_outbox(record.outbox_id, "failed", error_type=type(exc).__name__)
            logger.warning("wecom.durable outbox recovery failed error_type={}", type(exc).__name__)
            return
        await self._mark_outbox(record.outbox_id, "delivered")
        if record.inbox_id:
            await self._mark_inbox(record.inbox_id, "completed")

    async def _mark_inbox(
        self,
        inbox_id: str,
        status: InboxStatus,
        *,
        error_type: str = "",
    ) -> None:
        store = await self._ensure_durable_store()
        if store is None:
            return
        await asyncio.to_thread(
            store.mark_inbox,
            inbox_id,
            status,
            now=datetime.now(UTC),
            error_type=error_type,
        )

    async def _mark_outbox(
        self,
        outbox_id: str,
        status: OutboxStatus,
        *,
        error_type: str = "",
    ) -> None:
        store = await self._ensure_durable_store()
        if store is None:
            return
        await asyncio.to_thread(
            store.mark_outbox,
            outbox_id,
            status,
            now=datetime.now(UTC),
            error_type=error_type,
        )

    def _schedule_inbox_status(
        self,
        inbox_id: str,
        status: InboxStatus,
        *,
        error_type: str = "",
    ) -> None:
        task = asyncio.create_task(
            self._mark_inbox(inbox_id, status, error_type=error_type),
            name=f"agentseek-wecom.inbox-status.{inbox_id[-12:]}",
        )
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._on_dispatch_done)

    def _durable_lease_duration(self) -> timedelta:
        processing_window = (
            self.settings.queue_wait_timeout_seconds
            + self.settings.turn_timeout_seconds
            + self.settings.shutdown_timeout_seconds
            + 30.0
        )
        return timedelta(seconds=max(self.settings.durable_lease_seconds, processing_window))

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
            should_deliver = stream.deferred_response_url
            if stream.inbox_id:
                self._schedule_inbox_status(stream.inbox_id, "failed", error_type=error_type or status)
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
        intent = take_template_card_intent(content)
        if intent is None and has_template_card_intent_marker(content):
            content = "交付意图已失效，请重新发送精确交付命令。"
        elif intent is None and has_template_card_control_instruction(content):
            content = "内部交付指令已被安全拦截，未发送任何文件。请重新发送精确交付命令。"
        durable_outbox: OutboxRecord | None = None
        store = await self._ensure_durable_store()
        if store is not None:
            message_type = "markdown" if intent is None else "template_card"
            envelope: dict[str, Any] = {"response_url": response_url}
            if intent is None:
                envelope["content"] = content
            else:
                envelope["template_card"] = dict(intent.template_card)
            durable_outbox = await asyncio.to_thread(
                store.enqueue_outbox,
                inbox_id=stream.inbox_id,
                stream_id=stream.stream_id,
                message_type=message_type,
                envelope=envelope,
                reply_deadline=stream.reply_deadline,
                now=datetime.now(UTC),
            )
            durable_outbox = await asyncio.to_thread(
                store.claim_outbox,
                durable_outbox.outbox_id,
                now=datetime.now(UTC),
                owner=self._durable_owner,
                lease_duration=self._durable_lease_duration(),
            )
            if durable_outbox is None:
                return "skipped"
        try:
            if intent is None:
                await asyncio.to_thread(self._response_url_sender.send_markdown, response_url, content)
            else:
                await asyncio.to_thread(
                    self._response_url_sender.send_template_card,
                    response_url,
                    intent.template_card,
                )
                await asyncio.to_thread(intent.on_succeeded)
        except Exception as exc:
            if intent is not None:
                await asyncio.to_thread(intent.on_failed, type(exc).__name__)
            logger.warning(
                "wecom.response_url delivery failed stream_id={} error_type={}",
                stream.stream_id,
                type(exc).__name__,
            )
            _emit_enterprise_event(
                "wecom_template_card_delivery" if intent is not None else "wecom_response_url_delivery",
                status="error",
                stream_id=stream.stream_id,
                session_id=stream.session_id,
                content_chars=len(content),
                error_type=type(exc).__name__,
            )
            if durable_outbox is not None:
                await self._mark_outbox(durable_outbox.outbox_id, "failed", error_type=type(exc).__name__)
            return "delivery_error"
        if durable_outbox is not None:
            await self._mark_outbox(durable_outbox.outbox_id, "delivered")
        if stream.inbox_id:
            await self._mark_inbox(stream.inbox_id, "completed")
        _emit_enterprise_event(
            "wecom_template_card_delivery" if intent is not None else "wecom_response_url_delivery",
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
        address = getattr(message, _CONVERSATION_ADDRESS_ATTR, None)
        if isinstance(address, ConversationAddress):
            address = address.with_plaintext_userid(resolved_userid)
            session_id = address.session_id
            chat_id = address.chat_id
            setattr(message, _CONVERSATION_ADDRESS_ATTR, address)
        else:
            wecom_context = message.context.get("wecom")
            raw = wecom_context.get("raw") if isinstance(wecom_context, dict) else None
            is_group = isinstance(raw, dict) and raw.get("chattype") == "group"
            if is_group:
                session_id = self._queue_session_id(message)
                chat_id = message.chat_id or session_id
            else:
                session_id = f"wecom:{userid}"
                chat_id = userid
        message.session_id = session_id
        message.chat_id = chat_id
        message.context.update({
            "from_userid": from_userid,
            "userid": userid,
            "oa_account": userid,
            "chat_id": chat_id,
        })
        wecom = message.context.get("wecom")
        if isinstance(wecom, dict):
            wecom.update({
                "from_userid": from_userid,
                "open_userid": from_userid if resolved_userid else None,
                "resolved_userid": resolved_userid,
                "userid": userid,
                "chat_id": chat_id,
            })
            address_context = wecom.get("address")
            if isinstance(address_context, dict):
                if isinstance(address, ConversationAddress):
                    address_context.clear()
                    address_context.update(address.to_safe_context())
                else:
                    address_context.update({
                        "chat_id": chat_id,
                        "plaintext_userid": resolved_userid,
                    })
        stream_id = getattr(message, _STREAM_ID_ATTR, None)
        stream = self._streams.get(stream_id) if isinstance(stream_id, str) else None
        if stream is not None:
            stream.session_id = session_id
            stream.chat_id = chat_id

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

    def _message_context(
        self,
        *,
        data: dict[str, Any],
        from_userid: str | None,
        resolved_userid: str | None,
        userid: str | None,
        chat_id: str,
        files_context: dict[str, Any] | None = None,
        address: ConversationAddress | None = None,
    ) -> dict[str, Any]:
        address = address or self._conversation_address(data, plaintext_userid=resolved_userid)
        address_context = address.to_safe_context()
        address_context["chat_id"] = chat_id
        context = {
            "from_userid": from_userid,
            "userid": userid,
            "oa_account": userid,
            "chat_id": chat_id,
            "msgtype": data.get("msgtype"),
            "wecom": {
                "from_userid": from_userid,
                "open_userid": from_userid if resolved_userid else None,
                "resolved_userid": resolved_userid,
                "userid": userid,
                "chat_id": chat_id,
                "chat_type": address.chat_type,
                "msgtype": data.get("msgtype"),
                "address": address_context,
                "raw": _safe_wecom_payload(data),
            },
        }
        if files_context:
            context["files"] = files_context
        return context

    def _conversation_address(
        self,
        data: dict[str, Any],
        *,
        plaintext_userid: str | None = None,
    ) -> ConversationAddress:
        if str(data.get("chattype") or "single") == "group" and not str(data.get("chatid") or "").strip():
            logger.warning("wecom.group_message missing chatid msgid={}", _extract_msgid(data))
        return self._transport.address_for(data, plaintext_userid=plaintext_userid)

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
        address: ConversationAddress,
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
                address=address,
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
        address: ConversationAddress,
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
                    if stream.inbox_id:
                        self._schedule_inbox_status(stream.inbox_id, "failed", error_type=type(exc).__name__)
                    if stream.deferred_response_url:
                        self._schedule_response_url_delivery(stream, stream.content)
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
                chat_id=chat_id,
                files_context=result.to_context(),
                address=address,
            ),
        )
        setattr(message, _STREAM_ID_ATTR, stream_id)
        setattr(message, _CONVERSATION_ADDRESS_ATTR, address)
        await self._run_receive(message)


def _artifact_redemption_page(*, nonce: str) -> str:
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>下载报告</title></head><body><p id='status'>正在校验一次性下载授权…</p>"
        f"<script nonce='{nonce}'>"
        "const token=window.location.hash.slice(1);history.replaceState(null,'',window.location.pathname);"
        "const status=document.getElementById('status');"
        "if(!token){status.textContent='下载授权无效或已被消费。';}else{"
        "fetch(window.location.pathname+'/redeem',"
        "{method:'POST',headers:{'Content-Type':'text/plain'},body:token,credentials:'omit'})"
        ".then(async response=>{if(!response.ok)throw new Error('gone');"
        "const blob=await response.blob();const url=URL.createObjectURL(blob);"
        "const link=document.createElement('a');link.href=url;link.download='report.docx';"
        "document.body.appendChild(link);link.click();URL.revokeObjectURL(url);link.remove();"
        "status.textContent='下载已开始；该链接不能再次使用。';})"
        ".catch(()=>{status.textContent='下载授权已过期、已使用或无效。';});}"
        "</script></body></html>"
    )


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


def _is_durable_recovery(data: dict[str, Any]) -> bool:
    return data.get(_DURABLE_RECOVERY_ATTR) is True


def _durable_reply_deadline(data: dict[str, Any]) -> datetime | None:
    value = data.get(_DURABLE_REPLY_DEADLINE_ATTR)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


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


def _append_quote_context(data: dict[str, Any], content: str) -> str:
    quote = data.get("quote")
    if not isinstance(quote, dict):
        return content

    quote_type = str(quote.get("msgtype") or "unknown")
    quoted_content = ""
    if quote_type == "text":
        text = quote.get("text")
        if isinstance(text, dict):
            quoted_content = str(text.get("content") or "").strip()
    elif quote_type == "mixed":
        quoted_content = _mixed_text_content(quote)
    elif quote_type == "voice":
        voice = quote.get("voice")
        if isinstance(voice, dict):
            quoted_content = str(voice.get("content") or "").strip()

    label = {
        "text": "文本",
        "mixed": "图文混排",
        "image": "图片",
        "voice": "语音",
        "file": "文件",
        "video": "视频",
    }.get(quote_type, "未知类型")
    quote_block = f"引用消息（{label}）"
    if quoted_content:
        quote_block = f"{quote_block}：\n{quoted_content}"
    return "\n\n".join(part for part in (content.strip(), quote_block) if part)


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
    for key in ("msgid", "aibotid", "chatid", "chattype", "msgtype"):
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
        safe["mixed"] = {"msg_item": _safe_mixed_items(data)}
    elif msgtype == "event":
        event = data.get("event")
        if isinstance(event, dict):
            safe_event = {key: event[key] for key in ("eventtype",) if key in event}
            if safe_event:
                safe["event"] = safe_event

    quote = data.get("quote")
    if isinstance(quote, dict):
        safe_quote = _safe_quote_payload(quote)
        if safe_quote:
            safe["quote"] = safe_quote

    return safe


def _safe_quote_payload(quote: dict[str, Any]) -> dict[str, Any]:
    """Keep quoted semantics while excluding signed URLs and response capabilities."""

    quote_type = str(quote.get("msgtype") or "")
    if not quote_type:
        return {}
    safe: dict[str, Any] = {"msgtype": quote_type}
    if quote_type == "text":
        text = quote.get("text")
        if isinstance(text, dict) and text.get("content") is not None:
            safe["text"] = {"content": str(text["content"])}
    elif quote_type == "mixed":
        safe["mixed"] = {"msg_item": _safe_mixed_items(quote)}
    elif quote_type in {"file", "image", "voice", "video"}:
        payload = quote.get(quote_type)
        if isinstance(payload, dict):
            safe_media: dict[str, Any] = {
                "has_url": bool(_extract_ai_bot_media(quote)),
                "has_media_id": bool(_extract_media_id(quote)),
            }
            for key in ("filename", "file_name", "size", "filesize", "mime_type", "content_type"):
                if key in payload:
                    safe_media[key] = payload[key]
            if quote_type == "voice" and payload.get("content") is not None:
                safe_media["content"] = str(payload["content"])
            safe[quote_type] = safe_media
    return safe


def _safe_mixed_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in _mixed_items(data):
        item_type = str(item.get("msgtype") or "")
        if item_type == "text":
            text = item.get("text")
            if isinstance(text, dict) and text.get("content") is not None:
                items.append({"msgtype": "text", "text": {"content": str(text["content"])}})
        elif item_type in {"image", "file", "video"}:
            items.append({"msgtype": item_type, item_type: {"has_url": bool(_extract_ai_bot_media(item))}})
    return items


def _emit_enterprise_event(event: str, **fields: Any) -> None:
    try:
        from agentseek_enterprise.observability import emit_enterprise_event
    except ImportError:  # pragma: no cover - agentseek-wecom can be installed without enterprise extras.
        return
    emit_enterprise_event(event, **fields)
