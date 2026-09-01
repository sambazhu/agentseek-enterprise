from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import secrets
import signal
import stat
import threading
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from loguru import logger
from websockets.asyncio.client import connect

from agentseek_wecom.addressing import ConversationAddress, long_connection_conversation_address
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.transport import InboundMessageHandler

LONG_CONNECTION_REQUEST_ID_KEY = "_agentseek_wecom_long_connection_req_id"


class WeComLongConnectionError(RuntimeError):
    """Base error for AI Bot long-connection lifecycle and commands."""


class WeComLongConnectionAuthError(WeComLongConnectionError):
    """Raised when WeCom rejects the BotID/Secret subscription."""


class WeComLongConnectionNotReady(WeComLongConnectionError):
    """Raised when a command is attempted without an authenticated connection."""


class WeComLongConnectionCommandRejected(WeComLongConnectionError):
    """Raised when WeCom explicitly rejects an authenticated command."""


class WeComProactiveNotEligible(WeComLongConnectionError):
    """Raised when the target conversation has no observed interaction qualification."""


class _WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


Connector = Callable[[str], contextlib.AbstractAsyncContextManager[_WebSocketConnection]]


class _SignalNeutralUvicornServer(uvicorn.Server):
    """Let the outer channel manager own process termination signals."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


def _default_connector(url: str) -> contextlib.AbstractAsyncContextManager[_WebSocketConnection]:
    return connect(url, ping_interval=None, max_size=16 * 1024 * 1024)


class AiBotLongConnectionTransport:
    """Authenticated WebSocket transport for one WeCom AI Bot."""

    def __init__(
        self,
        *,
        settings: WeComSettings,
        tenant_id: str,
        connector: Connector | None = None,
    ) -> None:
        self.settings = settings
        self.tenant_id = tenant_id.strip() or "default"
        self._connector = connector or _default_connector
        self._inbound_handler: InboundMessageHandler | None = None
        self._connection: _WebSocketConnection | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._callback_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._command_lock = asyncio.Lock()
        self._subscribed = asyncio.Event()
        self._closing = asyncio.Event()
        self._fatal_error: BaseException | None = None
        self._external_stop_event: asyncio.Event | None = None
        self._lock_descriptor: int | None = None
        self._interacted_conversations: set[tuple[str, str, str]] = set()
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._signal_loop: asyncio.AbstractEventLoop | None = None
        self._previous_sigterm_handler: Any = None
        self.app: FastAPI | None = self._build_app()

    @property
    def kind(self) -> Literal["aibot_long_connection"]:
        return "aibot_long_connection"

    @property
    def subscribed(self) -> bool:
        return self._subscribed.is_set()

    def bind_inbound(self, handler: InboundMessageHandler) -> None:
        self._inbound_handler = handler

    def address_for(
        self,
        data: dict[str, Any],
        *,
        plaintext_userid: str | None = None,
    ) -> ConversationAddress:
        return long_connection_conversation_address(
            data,
            tenant_id=self.tenant_id,
            plaintext_userid=plaintext_userid,
        )

    async def start(self, stop_event: asyncio.Event) -> None:
        if not self.settings.enabled:
            return
        if self._run_task is not None and not self._run_task.done():
            return
        self._acquire_instance_lock()
        self._external_stop_event = stop_event
        self._closing.clear()
        self._subscribed.clear()
        self._fatal_error = None
        try:
            self._install_sigterm_bridge(stop_event)
            await self._start_health_server()
            self._run_task = asyncio.create_task(
                self._run_forever(),
                name="agentseek-wecom.long-connection",
            )
            await asyncio.wait_for(
                self._wait_until_ready(),
                timeout=self.settings.long_connection_command_timeout_seconds
                + self.settings.long_connection_reconnect_min_seconds,
            )
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._closing.set()
        connection = self._connection
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()
        task = self._run_task
        self._run_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        callback_tasks = [task for task in self._callback_tasks if not task.done()]
        for callback_task in callback_tasks:
            callback_task.cancel()
        if callback_tasks:
            await asyncio.gather(*callback_tasks, return_exceptions=True)
        self._callback_tasks.clear()
        self._fail_pending(WeComLongConnectionNotReady("long connection stopped"))
        self._connection = None
        self._subscribed.clear()
        await self._stop_health_server()
        self._remove_sigterm_bridge()
        self._release_instance_lock()

    async def deliver_stream(
        self,
        *,
        request_id: str,
        stream_id: str,
        content: str,
        finish: bool,
    ) -> None:
        if not request_id or not stream_id:
            raise ValueError("long-connection stream delivery requires request_id and stream_id")
        await self._request(
            "aibot_respond_msg",
            request_id=request_id,
            body={
                "msgtype": "stream",
                "stream": {
                    "id": stream_id,
                    "finish": finish,
                    "content": content,
                },
            },
        )

    def remember_interaction(self, address: ConversationAddress) -> None:
        if address.transport != "aibot_long_connection":
            raise ValueError("only long-connection addresses can qualify proactive delivery")
        self._interacted_conversations.add(self._eligibility_key(address))

    def is_proactive_eligible(self, address: ConversationAddress) -> bool:
        return (
            address.transport == "aibot_long_connection"
            and self._eligibility_key(address) in self._interacted_conversations
        )

    async def send_proactive(
        self,
        address: ConversationAddress,
        *,
        message_type: Literal["markdown", "template_card"],
        payload: dict[str, Any],
        request_id: str | None = None,
    ) -> None:
        if not self.is_proactive_eligible(address):
            raise WeComProactiveNotEligible("conversation has no observed user interaction")
        if message_type not in {"markdown", "template_card"}:
            raise ValueError("AI Bot proactive delivery supports markdown or template_card")
        await self._request(
            "aibot_send_msg",
            request_id=request_id or uuid4().hex,
            body={
                "chatid": address.chat_id,
                "chat_type": 2 if address.chat_type == "group" else 1,
                "msgtype": message_type,
                message_type: payload,
            },
        )

    async def _run_forever(self) -> None:
        delay = self.settings.long_connection_reconnect_min_seconds
        while not self._should_stop():
            try:
                await self._run_connection()
                delay = self.settings.long_connection_reconnect_min_seconds
            except asyncio.CancelledError:
                raise
            except WeComLongConnectionAuthError as exc:
                self._fatal_error = exc
                self._subscribed.clear()
                return
            except Exception as exc:
                self._subscribed.clear()
                self._fail_pending(WeComLongConnectionNotReady("long connection interrupted"))
                if self._should_stop():
                    return
                logger.warning(
                    "wecom.long_connection disconnected error_type={} reconnect_seconds={}",
                    type(exc).__name__,
                    round(delay, 2),
                )
                jittered = delay * secrets.SystemRandom().uniform(0.8, 1.2)
                if await self._wait_or_stop(jittered):
                    return
                delay = min(delay * 2, self.settings.long_connection_reconnect_max_seconds)

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return {
                "ok": self.subscribed,
                "channel": "wecom",
                "enabled": self.settings.enabled,
                "transport": "long_connection",
                "subscribed": self.subscribed,
            }

        return app

    async def _start_health_server(self) -> None:
        if self._server_task is not None and not self._server_task.done():
            return
        app = self.app
        if app is None:
            raise WeComLongConnectionError("long connection health application is unavailable")
        config = uvicorn.Config(
            app,
            host=self.settings.host,
            port=self.settings.port,
            loop="asyncio",
            log_level="info",
        )
        self._server = _SignalNeutralUvicornServer(config)
        self._server_task = asyncio.create_task(
            self._server.serve(),
            name="agentseek-wecom.long-health-server",
        )
        for _ in range(100):
            server = self._server
            if server is not None and server.started:
                return
            task = self._server_task
            if task is not None and task.done():
                task.result()
                raise WeComLongConnectionError("long connection health server stopped during startup")
            await asyncio.sleep(0.05)
        raise WeComLongConnectionError("long connection health server did not start")

    async def _stop_health_server(self) -> None:
        server = self._server
        task = self._server_task
        self._server = None
        self._server_task = None
        if server is not None:
            server.should_exit = True
        if task is None or task.done():
            return
        try:
            async with asyncio.timeout(max(0.1, self.settings.shutdown_timeout_seconds)):
                await task
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _install_sigterm_bridge(self, stop_event: asyncio.Event) -> None:
        if threading.current_thread() is not threading.main_thread() or self._signal_loop is not None:
            return
        loop = asyncio.get_running_loop()
        previous_handler = signal.getsignal(signal.SIGTERM)
        try:
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            logger.warning("wecom.long_connection could not install SIGTERM bridge")
            return
        self._signal_loop = loop
        self._previous_sigterm_handler = previous_handler

    def _remove_sigterm_bridge(self) -> None:
        loop = self._signal_loop
        previous_handler = self._previous_sigterm_handler
        self._signal_loop = None
        self._previous_sigterm_handler = None
        if loop is None or loop.is_closed():
            return
        loop.remove_signal_handler(signal.SIGTERM)
        if threading.current_thread() is threading.main_thread() and previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)

    async def _run_connection(self) -> None:
        async with self._connector(self.settings.long_connection_url) as connection:
            self._connection = connection
            reader = asyncio.create_task(self._read_loop(connection), name="agentseek-wecom.long-reader")
            heartbeat: asyncio.Task[None] | None = None
            stop_wait: asyncio.Task[bool] | None = None
            try:
                response = await self._request(
                    "aibot_subscribe",
                    request_id=uuid4().hex,
                    body={
                        "bot_id": self.settings.long_connection_bot_id,
                        "secret": self.settings.long_connection_secret.get_secret_value(),
                    },
                    require_subscribed=False,
                )
                if int(response.get("errcode", -1)) != 0:
                    raise WeComLongConnectionAuthError("WeCom rejected long-connection subscription")
                self._subscribed.set()
                logger.info("wecom.long_connection subscribed")
                heartbeat = asyncio.create_task(
                    self._heartbeat_loop(),
                    name="agentseek-wecom.long-heartbeat",
                )
                stop_wait = asyncio.create_task(self._wait_for_stop(), name="agentseek-wecom.long-stop")
                done, _ = await asyncio.wait(
                    {reader, heartbeat, stop_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_wait in done and stop_wait.result():
                    return
                for completed in done:
                    if completed is not stop_wait:
                        completed.result()
                raise WeComLongConnectionNotReady("long connection ended")
            finally:
                self._subscribed.clear()
                self._connection = None
                for task in (reader, heartbeat, stop_wait):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (reader, heartbeat, stop_wait) if task is not None),
                    return_exceptions=True,
                )

    async def _read_loop(self, connection: _WebSocketConnection) -> None:
        while not self._should_stop():
            raw_message = await connection.recv()
            if isinstance(raw_message, bytes):
                raw_message = raw_message.decode("utf-8")
            try:
                message = json.loads(raw_message)
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("wecom.long_connection ignored malformed frame")
                continue
            if not isinstance(message, dict):
                logger.warning("wecom.long_connection ignored non-object frame")
                continue
            request_id = _request_id(message)
            if "errcode" in message and request_id:
                pending = self._pending.pop(request_id, None)
                if pending is not None and not pending.done():
                    pending.set_result(message)
                continue
            command = message.get("cmd")
            if command in {"aibot_msg_callback", "aibot_event_callback"}:
                task = asyncio.create_task(
                    self._dispatch_callback(message),
                    name=f"agentseek-wecom.long-callback.{request_id[-12:] if request_id else 'unknown'}",
                )
                self._callback_tasks.add(task)
                task.add_done_callback(self._on_callback_done)
                continue
            logger.info("wecom.long_connection ignored command={}", command)

    async def _dispatch_callback(self, message: dict[str, Any]) -> None:
        command = str(message.get("cmd") or "")
        request_id = _request_id(message)
        raw_body = message.get("body")
        if not request_id or not isinstance(raw_body, dict):
            logger.warning("wecom.long_connection callback missing request metadata command={}", command)
            return
        data = dict(raw_body)
        data[LONG_CONNECTION_REQUEST_ID_KEY] = request_id
        raw_event = data.get("event")
        event: dict[str, Any] = raw_event if isinstance(raw_event, dict) else {}
        if command == "aibot_event_callback" and event.get("eventtype") == "disconnected_event":
            logger.warning("wecom.long_connection received disconnected_event")
            return
        if command == "aibot_msg_callback":
            self.remember_interaction(self.address_for(data))
        handler = self._inbound_handler
        if handler is None:
            raise WeComLongConnectionError("long-connection inbound handler is not bound")
        response_plain = await handler(data)
        if response_plain is None:
            return
        try:
            response_body = json.loads(response_plain)
        except json.JSONDecodeError as exc:
            raise WeComLongConnectionError("inbound handler returned invalid JSON") from exc
        if not isinstance(response_body, dict):
            raise WeComLongConnectionError("inbound handler response must be an object")
        response_command = "aibot_respond_msg"
        if command == "aibot_event_callback" and event.get("eventtype") == "enter_chat":
            response_command = "aibot_respond_welcome_msg"
        elif command == "aibot_event_callback" and event.get("eventtype") == "template_card_event":
            response_command = "aibot_respond_update_msg"
        await self._request(response_command, request_id=request_id, body=response_body)

    async def _heartbeat_loop(self) -> None:
        while not self._should_stop():
            if await self._wait_or_stop(self.settings.long_connection_heartbeat_seconds):
                return
            await self._request("ping", request_id=uuid4().hex)

    async def _request(
        self,
        command: str,
        *,
        request_id: str,
        body: dict[str, Any] | None = None,
        require_subscribed: bool = True,
    ) -> dict[str, Any]:
        async with self._command_lock:
            connection = self._connection
            if connection is None or (require_subscribed and not self._subscribed.is_set()):
                raise WeComLongConnectionNotReady("long connection is not subscribed")
            if request_id in self._pending:
                raise WeComLongConnectionError("duplicate in-flight request id")
            loop = asyncio.get_running_loop()
            pending: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[request_id] = pending
            envelope: dict[str, Any] = {
                "cmd": command,
                "headers": {"req_id": request_id},
            }
            if body is not None:
                envelope["body"] = body
            try:
                await connection.send(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
                response = await asyncio.wait_for(
                    pending,
                    timeout=self.settings.long_connection_command_timeout_seconds,
                )
            finally:
                self._pending.pop(request_id, None)
            errcode = response.get("errcode")
            if not isinstance(errcode, int):
                raise WeComLongConnectionError("WeCom command response has no integer errcode")
            if errcode != 0 and command != "aibot_subscribe":
                raise WeComLongConnectionCommandRejected(
                    f"WeCom command {command} failed with errcode {errcode}"
                )
            return response

    async def _wait_until_ready(self) -> None:
        while True:
            if self._subscribed.is_set():
                return
            if self._fatal_error is not None:
                raise self._fatal_error
            task = self._run_task
            if task is not None and task.done():
                task.result()
                raise WeComLongConnectionNotReady("long connection stopped before subscription")
            await asyncio.sleep(0.02)

    async def _wait_for_stop(self) -> bool:
        while not self._should_stop():
            await asyncio.sleep(0.1)
        return True

    async def _wait_or_stop(self, delay: float) -> bool:
        try:
            await asyncio.wait_for(self._wait_for_stop(), timeout=max(0.0, delay))
        except TimeoutError:
            return False
        return True

    def _should_stop(self) -> bool:
        return self._closing.is_set() or bool(self._external_stop_event and self._external_stop_event.is_set())

    def _on_callback_done(self, task: asyncio.Task[None]) -> None:
        self._callback_tasks.discard(task)
        if task.cancelled():
            return
        with contextlib.suppress(asyncio.CancelledError):
            error = task.exception()
            if error is not None:
                logger.opt(exception=error).error(
                    "wecom.long_connection callback failed error_type={}",
                    type(error).__name__,
                )

    def _fail_pending(self, error: BaseException) -> None:
        for pending in tuple(self._pending.values()):
            if not pending.done():
                pending.set_exception(error)
        self._pending.clear()

    def _eligibility_key(self, address: ConversationAddress) -> tuple[str, str, str]:
        return (address.bot_or_agent_id, address.chat_type, address.chat_id)

    def _acquire_instance_lock(self) -> None:
        path = Path(os.path.abspath(Path(self.settings.long_connection_lock_path).expanduser()))
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise WeComLongConnectionError("long-connection lock path must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            _validate_lock_descriptor(descriptor)
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WeComLongConnectionError("another process owns this AI Bot long connection") from exc
        except BaseException:
            os.close(descriptor)
            raise
        self._lock_descriptor = descriptor

    def _release_instance_lock(self) -> None:
        descriptor = self._lock_descriptor
        self._lock_descriptor = None
        if descriptor is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _request_id(message: dict[str, Any]) -> str:
    headers = message.get("headers")
    if not isinstance(headers, dict):
        return ""
    return str(headers.get("req_id") or "").strip()


def _validate_lock_descriptor(descriptor: int) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise WeComLongConnectionError("long-connection lock path must be a regular file")
