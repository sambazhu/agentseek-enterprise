from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass, field
from typing import Any
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
from agentseek_wecom.messages import make_text, make_text_stream
from agentseek_wecom.userid_resolver import UseridResolver, make_userid_resolver

_STREAM_ID_ATTR = "_agentseek_wecom_stream_id"


@dataclass
class StreamReply:
    stream_id: str
    session_id: str
    chat_id: str
    from_userid: str | None
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


class WeComChannel(Channel):
    name = "wecom"

    def __init__(
        self,
        on_receive: Any,
        *,
        settings: WeComSettings,
        userid_resolver: UseridResolver | None = None,
    ) -> None:
        self._on_receive = on_receive
        self.settings = settings
        self._userid_resolver = userid_resolver if userid_resolver is not None else make_userid_resolver(settings)
        self._crypto: WeComJsonCrypto | None = None
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._streams: dict[str, StreamReply] = {}
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
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def send(self, message: ChannelMessage) -> None:
        stream = await self._stream_for_outbound(message)
        if stream is None:
            return
        stream.update(content=content_of(message), finish=True)

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
        if msgtype == "text":
            return await self._handle_text(data)
        if msgtype == "voice":
            return await self._handle_voice(data)
        if msgtype == "stream":
            return await self._handle_stream_poll(data)
        if msgtype == "event":
            return self._handle_event(data)
        logger.info("wecom.unsupported_msgtype msgtype={}", msgtype)
        return None

    async def _handle_text(self, data: dict[str, Any]) -> str:
        content = str((data.get("text") or {}).get("content") or "")
        return await self._dispatch_user_message(data, content)

    async def _handle_voice(self, data: dict[str, Any]) -> str:
        content = str((data.get("voice") or {}).get("content") or "")
        return await self._dispatch_user_message(data, content)

    async def _dispatch_user_message(self, data: dict[str, Any], content: str) -> str:
        from_userid = _extract_from_userid(data)
        resolved_userid = await self._resolve_userid(from_userid)
        userid = resolved_userid or from_userid
        session_id = f"wecom:{userid or 'unknown'}"
        chat_id = userid or session_id
        stream = await self._create_stream(
            session_id=session_id,
            chat_id=chat_id,
            from_userid=from_userid,
        )
        message = ChannelMessage(
            session_id=session_id,
            channel=self.name,
            chat_id=chat_id,
            content=content,
            is_active=True,
            context={
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
            },
        )
        setattr(message, _STREAM_ID_ATTR, stream.stream_id)
        await self._on_receive(message)
        await self._wait_for_first_update(stream.stream_id)
        current = await self._get_stream(stream.stream_id)
        return make_text_stream(
            stream.stream_id,
            (current.content if current else "") or "已收到，正在处理...",
            bool(current.finish if current else False),
        )

    async def _resolve_userid(self, from_userid: str | None) -> str | None:
        if not from_userid or self._userid_resolver is None:
            return None
        try:
            return await asyncio.to_thread(self._userid_resolver.resolve, from_userid)
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
        await self._prune_streams()
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
            self._streams[stream_id] = stream
        return stream

    async def _get_stream(self, stream_id: str) -> StreamReply | None:
        if not stream_id:
            return None
        async with self._lock:
            return self._streams.get(stream_id)

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
        ttl = self.settings.cache_ttl_seconds
        async with self._lock:
            expired = [stream_id for stream_id, stream in self._streams.items() if now - stream.created_at > ttl]
            for stream_id in expired:
                self._streams.pop(stream_id, None)

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
    elif msgtype == "voice":
        voice = data.get("voice")
        if isinstance(voice, dict) and voice.get("content") is not None:
            safe["voice"] = {"content": str(voice["content"])}
    elif msgtype == "event":
        event = data.get("event")
        if isinstance(event, dict):
            safe_event = {key: event[key] for key in ("eventtype",) if key in event}
            if safe_event:
                safe["event"] = safe_event

    return safe
