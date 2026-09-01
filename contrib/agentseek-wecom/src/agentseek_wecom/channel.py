from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import time
from collections.abc import AsyncGenerator, AsyncIterable, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote, urlparse
from uuid import uuid4

from bub.channels.base import Channel
from bub.channels.message import ChannelMessage
from bub.envelope import content_of
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from republic import StreamEvent

from agentseek_wecom.addressing import ConversationAddress, WeComChatType
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.crypto import WeComCryptoError
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
    TemplateCardAction,
    has_template_card_action_handler,
    has_template_card_control_instruction,
    has_template_card_intent_marker,
    resolve_artifact_download,
    run_template_card_action,
    take_template_card_intent,
    validate_artifact_download_base_url,
)
from agentseek_wecom.response_url import WeComResponseUrlSender
from agentseek_wecom.transport import WeComTransport
from agentseek_wecom.transports.callback import AiBotCallbackTransport
from agentseek_wecom.transports.long_connection import (
    LONG_CONNECTION_REQUEST_ID_KEY,
    AiBotLongConnectionTransport,
    WeComLongConnectionCommandRejected,
)
from agentseek_wecom.userid_resolver import UseridResolver, make_userid_resolver

_STREAM_ID_ATTR = "_agentseek_wecom_stream_id"
_QUEUE_SESSION_ID_ATTR = "_agentseek_wecom_queue_session_id"
_FROM_USERID_ATTR = "_agentseek_wecom_from_userid"
_CONVERSATION_ADDRESS_ATTR = "_agentseek_wecom_conversation_address"
_DURABLE_INBOX_ID_ATTR = "_agentseek_wecom_durable_inbox_id"
_DURABLE_RECOVERY_ATTR = "_agentseek_wecom_durable_recovery"
_DURABLE_REPLY_DEADLINE_ATTR = "_agentseek_wecom_durable_reply_deadline"
_DURABLE_STREAM_ID_ATTR = "_agentseek_wecom_durable_stream_id"
_INTERNAL_CONTEXT_KEY = "_agentseek_wecom_internal"
_QUEUE_STATUS_COMMANDS = frozenset({"查看消息队列", "查看排队状态"})


class _WeComInboundMessage(ChannelMessage):
    """Keep routing metadata available to plugins but out of the model prompt."""

    @property
    def context_str(self) -> str:
        return _prompt_safe_context_str(self.context)


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
    long_connection_request_id: str | None = None
    long_connection_proactive_address: ConversationAddress | None = None
    conversation_address: ConversationAddress | None = None
    initial_response_sent: bool = False
    initial_response_content: str | None = None
    initial_response_finish: bool | None = None
    deferred_response_url: bool = False
    response_url_consumed: bool = False
    content: str = ""
    finish: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_stream_delivery_at: float = 0.0
    last_stream_delivery_content: str = ""

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
    prepare: Callable[[ChannelMessage], Awaitable[bool]] | None = None
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
        tenant_id = os.environ.get("AGENTSEEK_ENTERPRISE_TENANT_ID", "default")
        if transport is not None:
            self._transport = transport
        elif settings.transport_mode == "long_connection":
            self._transport = AiBotLongConnectionTransport(settings=settings, tenant_id=tenant_id)
        else:
            self._transport = AiBotCallbackTransport(settings=settings, tenant_id=tenant_id)
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
        transport_started = False
        try:
            if self._is_long_connection():
                await self._transport.start(stop_event)
                transport_started = True
            store = await self._ensure_durable_store()
            await self._recover_durable_messages()
            if store is not None:
                self._durable_recovery_task = asyncio.create_task(
                    self._run_durable_recovery_loop(stop_event),
                    name="agentseek-wecom.durable-recovery",
                )
            if not self._is_long_connection():
                await self._transport.start(stop_event)
                transport_started = True
        except BaseException:
            await self._stop_durable_recovery_loop()
            if transport_started:
                await self._transport.stop()
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
        if self._is_long_connection() and stream.long_connection_proactive_address is not None:
            delivery_status = await self._deliver_long_connection_proactive_terminal(stream, stream.content)
        elif self._is_long_connection():
            delivery_status = await self._deliver_long_connection_stream_once(stream, stream.content)
        elif stream.deferred_response_url:
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

    async def send_proactive_markdown(
        self,
        address: ConversationAddress,
        content: str,
        *,
        idempotency_key: str,
    ) -> str:
        if not content.strip():
            raise ValueError("proactive markdown content must not be empty")
        return await self._send_long_connection_proactive(
            address,
            message_type="markdown",
            payload={"content": content},
            idempotency_key=idempotency_key,
        )

    async def send_proactive_template_card(
        self,
        address: ConversationAddress,
        template_card: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> str:
        if not template_card:
            raise ValueError("proactive template_card must not be empty")
        return await self._send_long_connection_proactive(
            address,
            message_type="template_card",
            payload=dict(template_card),
            idempotency_key=idempotency_key,
        )

    async def _send_long_connection_proactive(
        self,
        address: ConversationAddress,
        *,
        message_type: Literal["markdown", "template_card"],
        payload: dict[str, Any],
        idempotency_key: str,
        inbox_id: str | None = None,
    ) -> str:
        transport = self._long_connection_transport()
        store = await self._ensure_durable_store()
        if not transport.is_proactive_eligible(address) and store is not None:
            persisted_eligible = await asyncio.to_thread(store.has_interaction, address)
            if persisted_eligible:
                transport.remember_interaction(address)
        if not transport.is_proactive_eligible(address):
            raise RuntimeError("conversation has no observed user interaction for proactive delivery")
        normalized_key = idempotency_key.strip()
        if not normalized_key or len(normalized_key) > 256:
            raise ValueError("proactive idempotency_key must contain 1 to 256 characters")
        stable_scope = "\x1f".join(
            (address.tenant_id, address.bot_or_agent_id, address.chat_type, address.chat_id, normalized_key)
        )
        stream_id = f"proactive_{hashlib.sha256(stable_scope.encode()).hexdigest()}"
        request_id = uuid4().hex
        deadline = datetime.now(UTC) + timedelta(hours=24)
        durable_outbox: OutboxRecord | None = None
        if store is not None:
            durable_outbox = await asyncio.to_thread(
                store.enqueue_outbox,
                inbox_id=inbox_id,
                stream_id=stream_id,
                message_type=f"long_connection_proactive_{message_type}",
                envelope={
                    "request_id": request_id,
                    "address": _address_envelope(address),
                    "payload": payload,
                },
                reply_deadline=deadline,
                now=datetime.now(UTC),
            )
            request_id = str(durable_outbox.envelope.get("request_id") or request_id)
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
            await transport.send_proactive(
                address,
                message_type=message_type,
                payload=payload,
                request_id=request_id,
            )
        except Exception as exc:
            if durable_outbox is not None:
                await self._mark_outbox(durable_outbox.outbox_id, "failed", error_type=type(exc).__name__)
            raise
        if durable_outbox is not None:
            await self._mark_outbox(durable_outbox.outbox_id, "delivered")
        _emit_enterprise_event(
            "wecom_long_connection_proactive_delivery",
            status="succeeded",
            stream_id=stream_id,
            chat_type=address.chat_type,
            content_chars=len(str(payload.get("content") or "")),
        )
        return "succeeded"

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
                        if self._is_long_connection():
                            await self._maybe_deliver_long_connection_stream(reply)
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
            return await self._handle_event(data)
        logger.info("wecom.unsupported_msgtype msgtype={}", msgtype)
        return None

    async def _handle_text(self, data: dict[str, Any]) -> str | None:
        content = str((data.get("text") or {}).get("content") or "")
        proactive_trigger = self.settings.long_connection_proactive_probe_trigger
        if self._is_long_connection() and proactive_trigger and content == proactive_trigger:
            return await self._handle_long_connection_proactive_probe(data)
        trigger = self.settings.response_url_probe_trigger
        if trigger and content == trigger:
            return await self._handle_response_url_probe(data)
        card_trigger = self.settings.response_url_template_card_probe_trigger
        if card_trigger and content == card_trigger:
            return await self._handle_response_url_template_card_probe(data)
        interaction_trigger = self.settings.template_card_event_probe_trigger
        if interaction_trigger and content == interaction_trigger:
            return await self._handle_template_card_event_probe(data)
        return await self._dispatch_user_message(data, _append_quote_context(data, content))

    async def _handle_long_connection_proactive_probe(self, data: dict[str, Any]) -> None:
        from_userid = _extract_from_userid(data)
        conversation = self._conversation_address(data)
        stream, is_duplicate = await self._get_or_create_stream_for_message(
            msgid=_extract_msgid(data),
            data=data,
            address=conversation,
            session_id=conversation.session_id,
            chat_id=conversation.chat_id,
            from_userid=from_userid,
            response_url=None,
        )
        if is_duplicate:
            await self._commit_inbound_stream_response(stream)
            return
        stream.update(content="长连接主动消息探针已启动，请检查随后两条消息。", finish=True)
        await self._commit_inbound_stream_response(stream)
        probe_scope = _extract_msgid(data) or stream.stream_id
        card = {
            "card_type": "button_interaction",
            "main_title": {
                "title": "AgentSeek M0.5 长连接探针",
                "desc": "主动模板卡片发送成功",
            },
            "sub_title_text": "点击按钮后应进入同一会话的卡片事件回合。",
            "button_list": [{"text": "确认交互", "style": 1, "key": "M05_CONFIRM"}],
            "task_id": f"agentseek_m05_{stream.stream_id}",
        }
        try:
            await self.send_proactive_markdown(
                conversation,
                "AgentSeek M0.5：长连接主动 Markdown 发送成功。",
                idempotency_key=f"m05-probe:{probe_scope}:markdown",
            )
            await self.send_proactive_template_card(
                conversation,
                card,
                idempotency_key=f"m05-probe:{probe_scope}:template-card",
            )
        except Exception as exc:
            logger.warning("wecom.long_connection proactive probe failed error_type={}", type(exc).__name__)
            _emit_enterprise_event(
                "wecom_long_connection_proactive_probe",
                status="error",
                error_type=type(exc).__name__,
            )
            return
        _emit_enterprise_event("wecom_long_connection_proactive_probe", status="succeeded")

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

    async def _handle_template_card_event_probe(self, data: dict[str, Any]) -> str:
        from_userid = _extract_from_userid(data)
        conversation = self._conversation_address(data)
        stream = await self._create_stream(
            session_id=conversation.session_id,
            chat_id=conversation.chat_id,
            from_userid=from_userid,
        )
        response_url = _extract_response_url(data)
        if not response_url:
            stream.update(content="卡片交互探针失败：回调未包含 response_url。", finish=True)
            return await self._stream_response(stream.stream_id)
        stream.update(content="卡片交互探针已启动，请点击随后卡片中的确认按钮。", finish=True)
        task = asyncio.create_task(
            self._run_template_card_event_probe(response_url),
            name=f"agentseek-wecom.template-card-event-probe.{stream.stream_id}",
        )
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._on_dispatch_done)
        return await self._stream_response(stream.stream_id)

    async def _run_template_card_event_probe(self, response_url: str) -> None:
        card = {
            "card_type": "button_interaction",
            "main_title": {
                "title": "AgentSeek M0.4 卡片交互探针",
                "desc": "点击按钮后应收到卡片事件确认",
            },
            "sub_title_text": "该探针只验证 Callback 模板卡片事件，不执行外部业务操作。",
            "button_list": [{"text": "确认交互", "style": 1, "key": "M04_CONFIRM"}],
            "task_id": f"agentseek_m04_{uuid4().hex}",
        }
        try:
            await asyncio.to_thread(self._response_url_sender.send_template_card, response_url, card)
        except Exception as exc:
            logger.warning("wecom.template_card_event_probe failed error_type={}", type(exc).__name__)
            _emit_enterprise_event(
                "wecom_template_card_event_probe",
                status="error",
                error_type=type(exc).__name__,
            )
        else:
            _emit_enterprise_event("wecom_template_card_event_probe", status="succeeded")

    async def _handle_voice(self, data: dict[str, Any]) -> str | None:
        content = str((data.get("voice") or {}).get("content") or "")
        if not content:
            content = "用户发送了一条语音消息，但回调未包含转写内容。"
        return await self._dispatch_user_message(data, _append_quote_context(data, content))

    async def _handle_mixed(self, data: dict[str, Any]) -> str | None:
        content = _mixed_text_content(data)
        if _extract_media_items(data):
            return await self._handle_media_message(data, fallback_content=content)
        content = _append_quote_context(data, content)
        return await self._dispatch_user_message(data, content or "用户发送了一条图文混排消息。")

    async def _handle_media_message(self, data: dict[str, Any], *, fallback_content: str = "") -> str | None:
        media_items = _extract_media_items(data)
        content = _append_quote_context(data, fallback_content.strip())
        if not media_items:
            content = content or f"用户发送了 {data.get('msgtype') or 'media'} 消息，但回调未包含可下载 URL。"
            return await self._dispatch_user_message(data, content)

        from_userid = _extract_from_userid(data)
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
            return await self._commit_inbound_stream_response(stream)

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
        message = _WeComInboundMessage(
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
        first_response = await self._commit_inbound_stream_response(
            stream,
            force_deferred=bool(stream.response_url),
        )
        self._schedule_receive(
            message,
            prepare=lambda pending: self._prepare_media_turn(
                pending,
                data=data,
                media=media_items[0],
                leading_content=content,
            ),
        )
        return first_response

    async def _dispatch_user_message(
        self,
        data: dict[str, Any],
        content: str,
        *,
        long_connection_proactive: bool = False,
    ) -> str | None:
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
            if long_connection_proactive and self._is_long_connection():
                return None
            return await self._commit_inbound_stream_response(stream)

        if long_connection_proactive and self._is_long_connection():
            stream.long_connection_proactive_address = conversation

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
            return await self._commit_inbound_stream_response(stream)
        message = _WeComInboundMessage(
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
        if self._is_long_connection():
            if stream.long_connection_proactive_address is not None:
                self._schedule_receive(message)
                return None
            first_response = await self._commit_inbound_stream_response(stream)
            self._schedule_receive(message)
            return first_response
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

    async def _handle_event(self, data: dict[str, Any]) -> str | None:
        raw_event = data.get("event")
        event = raw_event if isinstance(raw_event, dict) else {}
        if event.get("eventtype") == "enter_chat":
            return make_text(self.settings.welcome_text)
        if event.get("eventtype") == "template_card_event":
            content = _template_card_event_content(event.get("template_card_event"))
            if content is None:
                logger.warning("wecom.template_card_event invalid msgid={}", _extract_msgid(data))
                if self._is_long_connection():
                    return None
                return make_text("卡片操作数据无效，请重新打开卡片后再试。")
            return await self._dispatch_user_message(
                data,
                content,
                long_connection_proactive=True,
            )
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
            long_connection_request_id=str(data.get(LONG_CONNECTION_REQUEST_ID_KEY) or "") or None,
            conversation_address=address,
            content="已收到，正在处理...",
            finish=False,
        )
        async with self._lock:
            self._prune_streams_locked(time.time())
            if msgid:
                existing_id = self._stream_ids_by_msgid.get(msgid)
                existing = self._streams.get(existing_id or "")
                if existing is not None:
                    incoming_request_id = str(data.get(LONG_CONNECTION_REQUEST_ID_KEY) or "").strip()
                    if incoming_request_id:
                        existing.long_connection_request_id = incoming_request_id
                    return existing, True
                self._stream_ids_by_msgid.pop(msgid, None)

            if msgid and not _is_durable_recovery(data):
                store = await self._ensure_durable_store()
                if store is not None:
                    await asyncio.to_thread(
                        store.remember_interaction,
                        address,
                        now=datetime.now(UTC),
                    )
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

    async def _commit_inbound_stream_response(
        self,
        current: StreamReply,
        *,
        force_deferred: bool | None = None,
    ) -> str | None:
        if not self._is_long_connection():
            if force_deferred is None:
                return await self._stream_response(current.stream_id)
            return await self._commit_stream_response(current, force_deferred=force_deferred)

        request_id = current.long_connection_request_id
        if not request_id:
            if current.inbox_id:
                await self._mark_inbox(current.inbox_id, "blocked", error_type="long_connection_req_id_missing")
            raise RuntimeError("long-connection callback did not include req_id")
        transport = self._long_connection_transport()
        await transport.deliver_stream(
            request_id=request_id,
            stream_id=current.stream_id,
            content=current.content or "已收到，正在处理...",
            finish=current.finish,
        )
        current.initial_response_content = current.content or "已收到，正在处理..."
        current.initial_response_finish = current.finish
        current.initial_response_sent = True
        current.last_stream_delivery_at = time.monotonic()
        current.last_stream_delivery_content = current.content
        if current.finish and current.inbox_id:
            await self._mark_inbox(current.inbox_id, "completed")
        return None

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
        long_connection_request_id = _extract_long_connection_request_id(record.payload)
        if self._is_long_connection() and not long_connection_request_id:
            await self._mark_inbox(record.inbox_id, "blocked", error_type="reply_capability_missing")
            return
        if not self._is_long_connection() and not response_url:
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
        if record.message_type in {
            "long_connection_proactive_markdown",
            "long_connection_proactive_template_card",
        }:
            await self._recover_long_connection_proactive_outbox(record)
            return
        if record.message_type == "long_connection_stream":
            await self._recover_long_connection_stream_outbox(record)
            return
        if record.message_type == "template_card":
            await self._recover_template_card_outbox(record)
            return
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

    async def _recover_long_connection_stream_outbox(self, record: OutboxRecord) -> None:
        if not self._is_long_connection():
            await self._mark_outbox(record.outbox_id, "blocked", error_type="transport_unavailable")
            if record.inbox_id:
                await self._mark_inbox(record.inbox_id, "blocked", error_type="transport_unavailable")
            return
        request_id = record.envelope.get("request_id")
        content = record.envelope.get("content")
        finish = record.envelope.get("finish")
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(content, str)
            or finish is not True
        ):
            await self._mark_outbox(record.outbox_id, "blocked", error_type="invalid_envelope")
            if record.inbox_id:
                await self._mark_inbox(record.inbox_id, "blocked", error_type="invalid_envelope")
            return
        try:
            await self._long_connection_transport().deliver_stream(
                request_id=request_id,
                stream_id=record.stream_id,
                content=content,
                finish=True,
            )
        except WeComLongConnectionCommandRejected as exc:
            await self._recover_long_connection_stream_proactively(record, content, exc)
            return
        except Exception as exc:
            await self._mark_outbox(record.outbox_id, "failed", error_type=type(exc).__name__)
            logger.warning("wecom.long_connection outbox recovery failed error_type={}", type(exc).__name__)
            return
        await self._mark_outbox(record.outbox_id, "delivered")
        if record.inbox_id:
            await self._mark_inbox(record.inbox_id, "completed")

    async def _recover_long_connection_stream_proactively(
        self,
        record: OutboxRecord,
        content: str,
        stream_error: WeComLongConnectionCommandRejected,
    ) -> None:
        """Fall back after WeCom rejects a stream tied to a previous connection."""

        address = _address_from_envelope(record.envelope.get("address"))
        if address is None and record.inbox_id:
            store = await self._ensure_durable_store()
            inbox = await asyncio.to_thread(store.get_inbox, record.inbox_id) if store is not None else None
            if inbox is not None:
                address = self._conversation_address(inbox.payload)
        if address is None:
            await self._mark_outbox(record.outbox_id, "blocked", error_type="conversation_address_missing")
            if record.inbox_id:
                await self._mark_inbox(record.inbox_id, "blocked", error_type="conversation_address_missing")
            return
        try:
            status = await self._send_long_connection_proactive(
                address,
                message_type="markdown",
                payload={"content": content},
                idempotency_key=f"recovered-stream:{record.outbox_id}",
                inbox_id=record.inbox_id,
            )
        except Exception as exc:
            await self._mark_outbox(record.outbox_id, "failed", error_type=type(exc).__name__)
            logger.warning(
                "wecom.long_connection proactive stream recovery failed stream_error_type={} error_type={}",
                type(stream_error).__name__,
                type(exc).__name__,
            )
            return
        if status in {"succeeded", "skipped"}:
            await self._mark_outbox(record.outbox_id, "delivered")
            if record.inbox_id:
                await self._mark_inbox(record.inbox_id, "completed")
            _emit_enterprise_event(
                "wecom_long_connection_stream_recovery",
                status="proactive_fallback",
                stream_id=record.stream_id,
                chat_type=address.chat_type,
                content_chars=len(content),
                stream_error_type=type(stream_error).__name__,
            )

    async def _recover_long_connection_proactive_outbox(self, record: OutboxRecord) -> None:
        if not self._is_long_connection():
            await self._mark_outbox(record.outbox_id, "blocked", error_type="transport_unavailable")
            if record.inbox_id:
                await self._mark_inbox(record.inbox_id, "blocked", error_type="transport_unavailable")
            return
        if record.status == "sending" and record.attempts > 1:
            await self._mark_outbox(record.outbox_id, "blocked", error_type="delivery_outcome_ambiguous")
            if record.inbox_id:
                await self._mark_inbox(record.inbox_id, "blocked", error_type="delivery_outcome_ambiguous")
            return
        address = _address_from_envelope(record.envelope.get("address"))
        payload = record.envelope.get("payload")
        request_id = record.envelope.get("request_id")
        message_type = record.message_type.removeprefix("long_connection_proactive_")
        if (
            address is None
            or not isinstance(payload, dict)
            or not isinstance(request_id, str)
            or not request_id
            or message_type not in {"markdown", "template_card"}
        ):
            await self._mark_outbox(record.outbox_id, "blocked", error_type="invalid_envelope")
            if record.inbox_id:
                await self._mark_inbox(record.inbox_id, "blocked", error_type="invalid_envelope")
            return
        transport = self._long_connection_transport()
        # A proactive outbox can only be created after this address was
        # observed on an authenticated inbound callback. Restore that sealed
        # qualification for this recovery attempt.
        transport.remember_interaction(address)
        try:
            await transport.send_proactive(
                address,
                message_type=cast(Literal["markdown", "template_card"], message_type),
                payload=payload,
                request_id=request_id,
            )
        except Exception as exc:
            await self._mark_outbox(record.outbox_id, "failed", error_type=type(exc).__name__)
            logger.warning("wecom.long_connection proactive recovery failed error_type={}", type(exc).__name__)
            return
        await self._mark_outbox(record.outbox_id, "delivered")
        if record.inbox_id:
            await self._mark_inbox(record.inbox_id, "completed")

    async def _recover_template_card_outbox(self, record: OutboxRecord) -> None:
        response_url = record.envelope.get("response_url")
        template_card = record.envelope.get("template_card")
        action = _template_card_action_from_envelope(record.envelope.get("success_action"))
        if (
            not isinstance(response_url, str)
            or not response_url
            or not isinstance(template_card, dict)
            or action is None
            or not has_template_card_action_handler(action.kind)
        ):
            await self._mark_outbox(record.outbox_id, "blocked", error_type="manual_reconciliation_required")
            if record.inbox_id:
                await self._mark_inbox(record.inbox_id, "blocked", error_type="manual_reconciliation_required")
            return
        if record.status == "sending" and record.attempts > 1:
            await self._mark_outbox(record.outbox_id, "blocked", error_type="delivery_outcome_ambiguous")
            if record.inbox_id:
                await self._mark_inbox(record.inbox_id, "blocked", error_type="delivery_outcome_ambiguous")
            return
        if record.status != "sent":
            try:
                await asyncio.to_thread(
                    self._response_url_sender.send_template_card,
                    response_url,
                    template_card,
                )
            except Exception as exc:
                await self._mark_outbox(record.outbox_id, "failed", error_type=type(exc).__name__)
                logger.warning("wecom.durable card recovery failed error_type={}", type(exc).__name__)
                return
            await self._mark_outbox(record.outbox_id, "sent")
        try:
            await asyncio.to_thread(run_template_card_action, action)
        except Exception as exc:
            await self._mark_outbox(record.outbox_id, "sent", error_type=type(exc).__name__)
            logger.warning("wecom.durable card finalization failed error_type={}", type(exc).__name__)
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

    def _schedule_receive(
        self,
        message: ChannelMessage,
        *,
        prepare: Callable[[ChannelMessage], Awaitable[bool]] | None = None,
    ) -> None:
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
                prepare=prepare,
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

        queued = QueuedTurn(message=message, enqueued_at=time.monotonic(), prepare=prepare)
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
                    await self._dispatch_one(queued.message, prepare=queued.prepare)
                finally:
                    self._active_turn_started_at.pop(session_id, None)
                    queue.task_done()
        finally:
            current = asyncio.current_task()
            if self._session_workers.get(session_id) is current:
                self._session_workers.pop(session_id, None)
                self._session_queues.pop(session_id, None)
                self._pending_turn_counts.pop(session_id, None)

    async def _dispatch_one(
        self,
        message: ChannelMessage,
        *,
        prepare: Callable[[ChannelMessage], Awaitable[bool]] | None = None,
    ) -> None:
        enqueue_timeout = min(max(0.05, self.settings.turn_timeout_seconds), 10.0)
        try:
            async with asyncio.timeout(enqueue_timeout):
                await self._hydrate_message_identity(message)
            if prepare is not None and not await prepare(message):
                return
            started_at = time.monotonic()
            async with asyncio.timeout(enqueue_timeout):
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

    async def _prepare_media_turn(
        self,
        message: ChannelMessage,
        *,
        data: dict[str, Any],
        media: dict[str, str],
        leading_content: str,
    ) -> bool:
        """Perform media I/O inside the reserved per-session queue position."""

        from_userid = getattr(message, _FROM_USERID_ATTR, None)
        userid = message.context.get("userid")
        wecom_context = message.context.get("wecom")
        resolved_userid = wecom_context.get("resolved_userid") if isinstance(wecom_context, dict) else None
        address = getattr(message, _CONVERSATION_ADDRESS_ATTR, None)
        if not isinstance(address, ConversationAddress):
            address = self._conversation_address(
                data,
                plaintext_userid=resolved_userid if isinstance(resolved_userid, str) else None,
            )
        try:
            result = await self._download_and_store_media(
                data=data,
                media=media,
                session_id=message.session_id,
                chat_id=message.chat_id,
                userid=userid if isinstance(userid, str) else None,
                from_userid=from_userid if isinstance(from_userid, str) else None,
            )
            if result.pending:
                result = await self._poll_pending_file(
                    stream_id=getattr(message, _STREAM_ID_ATTR, ""),
                    record=result.record,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("wecom.media_intake failed msgtype={} error_type={}", data.get("msgtype"), type(exc).__name__)
            _emit_enterprise_event(
                "wecom_media_intake",
                status="error",
                stream_id=getattr(message, _STREAM_ID_ATTR, ""),
                session_id=message.session_id,
                chat_id=message.chat_id,
                from_userid=from_userid,
                msgtype=data.get("msgtype"),
                media_kind=media.get("kind"),
                error_type=type(exc).__name__,
            )
            user_notice = _file_error_user_notice(exc)
            message.content = "\n".join(
                part
                for part in (
                    leading_content,
                    user_notice
                    or f"已收到{_msgtype_label(data.get('msgtype'))}，但文件下载或解析失败：{type(exc).__name__}。",
                )
                if part
            ).strip()
            return True

        content_parts = [leading_content] if leading_content else []
        content_parts.append(result.user_notice)
        if result.context_block:
            content_parts.append("请结合当前文件上下文回答用户。")
        message.content = "\n".join(part for part in content_parts if part).strip()
        message.context["files"] = result.to_context()
        _emit_enterprise_event(
            "wecom_media_intake",
            status="succeeded",
            stream_id=getattr(message, _STREAM_ID_ATTR, ""),
            session_id=message.session_id,
            chat_id=message.chat_id,
            from_userid=from_userid,
            msgtype=data.get("msgtype"),
            file_id=result.record.file_id,
            mime_type=result.record.mime_type,
            size_bytes=result.record.size_bytes,
            extract_status=result.record.extract_status,
        )
        return True

    async def _poll_pending_file(self, *, stream_id: str, record: Any) -> Any:
        file_service = self._get_file_service()
        if file_service is None:
            raise RuntimeError("agentseek-files became unavailable while a file was pending")
        settings = getattr(file_service, "settings", None)
        timeout_s = float(getattr(settings, "mineru_poll_timeout_s", 300.0) or 300.0)
        interval_s = max(0.5, float(getattr(settings, "mineru_poll_interval_s", 2.0) or 2.0))
        deadline = time.monotonic() + max(timeout_s, interval_s)
        current_record = record
        while True:
            result = await file_service.poll_pending(current_record)
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
        return result

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
        should_deliver_long_connection = False
        if stream is not None and not stream.finish:
            stream.update(content=content, finish=True)
            should_deliver = stream.deferred_response_url
            should_deliver_long_connection = self._is_long_connection()
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
        elif should_deliver_long_connection and stream is not None:
            if stream.long_connection_proactive_address is not None:
                self._schedule_long_connection_proactive_delivery(stream, content)
            else:
                self._schedule_long_connection_delivery(stream, content)

    async def _maybe_deliver_long_connection_stream(self, stream: StreamReply) -> None:
        if (
            stream.finish
            or not stream.initial_response_sent
            or not stream.long_connection_request_id
            or stream.content == stream.last_stream_delivery_content
        ):
            return
        now = time.monotonic()
        if now - stream.last_stream_delivery_at < self.settings.long_connection_stream_interval_seconds:
            return
        try:
            await self._long_connection_transport().deliver_stream(
                request_id=stream.long_connection_request_id,
                stream_id=stream.stream_id,
                content=stream.content,
                finish=False,
            )
        except Exception as exc:
            logger.warning(
                "wecom.long_connection stream refresh failed stream_id={} error_type={}",
                stream.stream_id,
                type(exc).__name__,
            )
            return
        stream.last_stream_delivery_at = now
        stream.last_stream_delivery_content = stream.content

    def _schedule_long_connection_delivery(self, stream: StreamReply, content: str) -> None:
        task = asyncio.create_task(
            self._deliver_long_connection_stream_background(stream, content),
            name=f"agentseek-wecom.long-final.{stream.stream_id}",
        )
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._on_dispatch_done)

    async def _deliver_long_connection_stream_background(self, stream: StreamReply, content: str) -> None:
        await self._deliver_long_connection_stream_once(stream, content)

    def _schedule_long_connection_proactive_delivery(self, stream: StreamReply, content: str) -> None:
        task = asyncio.create_task(
            self._deliver_long_connection_proactive_background(stream, content),
            name=f"agentseek-wecom.long-proactive-final.{stream.stream_id}",
        )
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._on_dispatch_done)

    async def _deliver_long_connection_proactive_background(self, stream: StreamReply, content: str) -> None:
        await self._deliver_long_connection_proactive_terminal(stream, content)

    async def _deliver_long_connection_proactive_terminal(self, stream: StreamReply, content: str) -> str:
        address = stream.long_connection_proactive_address
        if address is None:
            if stream.inbox_id:
                await self._mark_inbox(stream.inbox_id, "blocked", error_type="conversation_address_missing")
            return "blocked"
        try:
            status = await self._send_long_connection_proactive(
                address,
                message_type="markdown",
                payload={"content": content},
                idempotency_key=f"card-event:{stream.inbox_id or stream.stream_id}",
                inbox_id=stream.inbox_id,
            )
        except Exception as exc:
            if stream.inbox_id:
                await self._mark_inbox(stream.inbox_id, "failed", error_type=type(exc).__name__)
            logger.warning(
                "wecom.long_connection card event delivery failed stream_id={} error_type={}",
                stream.stream_id,
                type(exc).__name__,
            )
            return "delivery_error"
        if stream.inbox_id and status in {"succeeded", "skipped"}:
            await self._mark_inbox(stream.inbox_id, "completed")
        return status

    async def _deliver_long_connection_stream_once(self, stream: StreamReply, content: str) -> str:
        request_id = stream.long_connection_request_id
        if not request_id:
            if stream.inbox_id:
                await self._mark_inbox(stream.inbox_id, "blocked", error_type="long_connection_req_id_missing")
            return "blocked"
        now = datetime.now(UTC)
        if stream.reply_deadline is not None and stream.reply_deadline <= now:
            if stream.inbox_id:
                await self._mark_inbox(stream.inbox_id, "blocked", error_type="reply_deadline_expired")
            return "expired"
        durable_outbox: OutboxRecord | None = None
        store = await self._ensure_durable_store()
        if store is not None:
            durable_outbox = await asyncio.to_thread(
                store.enqueue_outbox,
                inbox_id=stream.inbox_id,
                stream_id=stream.stream_id,
                message_type="long_connection_stream",
                envelope={
                    "request_id": request_id,
                    "content": content,
                    "finish": True,
                    "address": (
                        _address_envelope(stream.conversation_address)
                        if stream.conversation_address is not None
                        else None
                    ),
                },
                reply_deadline=stream.reply_deadline,
                now=now,
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
            await self._long_connection_transport().deliver_stream(
                request_id=request_id,
                stream_id=stream.stream_id,
                content=content,
                finish=True,
            )
        except Exception as exc:
            if durable_outbox is not None:
                await self._mark_outbox(durable_outbox.outbox_id, "failed", error_type=type(exc).__name__)
            logger.warning(
                "wecom.long_connection final delivery failed stream_id={} error_type={}",
                stream.stream_id,
                type(exc).__name__,
            )
            return "delivery_error"
        if durable_outbox is not None:
            await self._mark_outbox(durable_outbox.outbox_id, "delivered")
        if stream.inbox_id:
            await self._mark_inbox(stream.inbox_id, "completed")
        stream.last_stream_delivery_at = time.monotonic()
        stream.last_stream_delivery_content = content
        return "succeeded"

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
                if intent.success_action is not None:
                    envelope["success_action"] = {
                        "kind": intent.success_action.kind,
                        "payload": dict(intent.success_action.payload),
                    }
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
        except Exception as exc:
            if intent is not None and intent.on_failed is not None:
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
        if intent is not None:
            if durable_outbox is not None:
                await self._mark_outbox(durable_outbox.outbox_id, "sent")
            try:
                if intent.success_action is not None:
                    await asyncio.to_thread(run_template_card_action, intent.success_action)
                elif intent.on_succeeded is not None:
                    await asyncio.to_thread(intent.on_succeeded)
            except Exception as exc:
                if intent.on_failed is not None:
                    await asyncio.to_thread(intent.on_failed, type(exc).__name__)
                if durable_outbox is not None:
                    await self._mark_outbox(durable_outbox.outbox_id, "sent", error_type=type(exc).__name__)
                logger.warning(
                    "wecom.template_card finalization failed stream_id={} error_type={}",
                    stream.stream_id,
                    type(exc).__name__,
                )
                return "finalization_error"
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
            if self._is_long_connection():
                self._long_connection_transport().remember_interaction(address)
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
        message_id = _extract_msgid(data)
        if message_id:
            context[_INTERNAL_CONTEXT_KEY] = {"message_id": message_id}
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

    def _is_long_connection(self) -> bool:
        return self._transport.kind == "aibot_long_connection"

    def _long_connection_transport(self) -> AiBotLongConnectionTransport:
        if not isinstance(self._transport, AiBotLongConnectionTransport):
            raise TypeError("active stream delivery requires AiBotLongConnectionTransport")
        return self._transport

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
                aes_key=_media_decryption_key(media, callback_encoding_aes_key=self.settings.encoding_aes_key),
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


def _extract_long_connection_request_id(data: dict[str, Any]) -> str | None:
    value = data.get(LONG_CONNECTION_REQUEST_ID_KEY)
    return str(value).strip() if value else None


def _address_envelope(address: ConversationAddress) -> dict[str, Any]:
    return {
        "tenant_id": address.tenant_id,
        "bot_or_agent_id": address.bot_or_agent_id,
        "transport": address.transport,
        "chat_type": address.chat_type,
        "chat_id": address.chat_id,
        "sender_userid": address.sender_userid,
        "plaintext_userid": address.plaintext_userid,
        "last_interacted_at": address.last_interacted_at.isoformat(),
        "reply_deadline": address.reply_deadline.isoformat() if address.reply_deadline else None,
    }


def _address_from_envelope(value: Any) -> ConversationAddress | None:
    if not isinstance(value, dict):
        return None
    chat_type = value.get("chat_type")
    if value.get("transport") != "aibot_long_connection" or chat_type not in {"single", "group"}:
        return None
    try:
        last_interacted_at = datetime.fromisoformat(str(value["last_interacted_at"]))
        raw_deadline = value.get("reply_deadline")
        reply_deadline = datetime.fromisoformat(str(raw_deadline)) if raw_deadline else None
    except (KeyError, ValueError):
        return None
    if last_interacted_at.tzinfo is None or (reply_deadline is not None and reply_deadline.tzinfo is None):
        return None
    tenant_id = str(value.get("tenant_id") or "").strip()
    bot_id = str(value.get("bot_or_agent_id") or "").strip()
    chat_id = str(value.get("chat_id") or "").strip()
    if not tenant_id or not bot_id or not chat_id:
        return None
    sender_userid = value.get("sender_userid")
    plaintext_userid = value.get("plaintext_userid")
    return ConversationAddress(
        tenant_id=tenant_id,
        bot_or_agent_id=bot_id,
        transport="aibot_long_connection",
        chat_type=cast(WeComChatType, chat_type),
        chat_id=chat_id,
        sender_userid=str(sender_userid) if sender_userid else None,
        plaintext_userid=str(plaintext_userid) if plaintext_userid else None,
        last_interacted_at=last_interacted_at.astimezone(UTC),
        reply_deadline=reply_deadline.astimezone(UTC) if reply_deadline else None,
    )


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


def _template_card_event_content(value: Any) -> str | None:
    event = _safe_template_card_event(value)
    if event is None:
        return None
    lines = [
        "用户提交了企业微信模板卡片交互。",
        f"卡片类型：{event['card_type']}",
        f"操作标识：{event['event_key']}",
        f"任务标识：{event['task_id']}",
    ]
    selected_items = event.get("selected_items")
    if isinstance(selected_items, list):
        lines.append("选择结果：")
        for item in selected_items:
            option_ids = "、".join(item["option_ids"]) or "（未选择）"
            lines.append(f"- {item['question_key']}：{option_ids}")
    return "\n".join(lines)


def _safe_template_card_event(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    card_type = _bounded_event_value(value.get("card_type"), 64)
    event_key = _bounded_event_value(value.get("event_key"), 128)
    task_id = _bounded_event_value(value.get("task_id"), 128)
    if not card_type or not event_key or not task_id:
        return None
    if card_type not in {"button_interaction", "vote_interaction", "multiple_interaction", "text_notice", "news_notice"}:
        return None
    safe: dict[str, Any] = {
        "card_type": card_type,
        "event_key": event_key,
        "task_id": task_id,
    }
    selected_items = value.get("selected_items")
    raw_items = selected_items.get("selected_item") if isinstance(selected_items, dict) else None
    if isinstance(raw_items, list):
        normalized_items: list[dict[str, Any]] = []
        for raw_item in raw_items[:32]:
            if not isinstance(raw_item, dict):
                continue
            question_key = _bounded_event_value(raw_item.get("question_key"), 128)
            option_ids = raw_item.get("option_ids")
            raw_options = option_ids.get("option_id") if isinstance(option_ids, dict) else None
            if not question_key or not isinstance(raw_options, list):
                continue
            normalized_options = [
                option
                for raw_option in raw_options[:64]
                if (option := _bounded_event_value(raw_option, 128))
            ]
            normalized_items.append({"question_key": question_key, "option_ids": normalized_options})
        if normalized_items:
            safe["selected_items"] = normalized_items
    return safe


def _bounded_event_value(value: Any, limit: int) -> str:
    if not isinstance(value, (str, int)):
        return ""
    normalized = " ".join(str(value).split())
    return normalized[:limit]


def _template_card_action_from_envelope(value: Any) -> TemplateCardAction | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    payload = value.get("payload")
    if not isinstance(kind, str) or not kind.strip() or not isinstance(payload, dict):
        return None
    return TemplateCardAction(kind=kind.strip(), payload=payload)


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
    media = {"url": url, "filename": filename, "mime_type": mime_type, "kind": msgtype}
    aes_key = _first_text(payload, "aeskey")
    if aes_key:
        media["aes_key"] = aes_key
    return media


def _media_decryption_key(media: dict[str, str], *, callback_encoding_aes_key: str) -> bytes:
    value = media.get("aes_key") or callback_encoding_aes_key
    if not value:
        raise WeComCryptoError("AI Bot media callback did not include a decryption key")
    return decode_encoding_aes_key(value)


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
    for key in ("chattype", "msgtype"):
        value = data.get(key)
        if value:
            safe[key] = value

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
            card_event = _safe_template_card_event(event.get("template_card_event"))
            if card_event is not None:
                safe_event["template_card_event"] = card_event
            if safe_event:
                safe["event"] = safe_event

    quote = data.get("quote")
    if isinstance(quote, dict):
        safe_quote = _safe_quote_payload(quote)
        if safe_quote:
            safe["quote"] = safe_quote

    return safe


def _prompt_safe_context_str(context: Mapping[str, Any]) -> str:
    """Project semantic WeCom context without transport or identity identifiers."""

    prompt_context: dict[str, Any] = {}
    channel = context.get("channel")
    if channel:
        prompt_context["channel"] = channel

    wecom = context.get("wecom")
    if isinstance(wecom, Mapping):
        prompt_wecom: dict[str, Any] = {}
        for key in ("chat_type", "msgtype"):
            value = wecom.get(key)
            if value:
                prompt_wecom[key] = value
        raw = wecom.get("raw")
        if isinstance(raw, Mapping) and raw:
            prompt_wecom["raw"] = dict(raw)
        if prompt_wecom:
            prompt_context["wecom"] = prompt_wecom

    return "|".join(f"{key}={value}" for key, value in prompt_context.items())


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
