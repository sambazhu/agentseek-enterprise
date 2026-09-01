from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from agentseek_wecom.addressing import long_connection_conversation_address
from agentseek_wecom.channel import WeComChannel
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.durable import SqliteDurableMessageStore
from agentseek_wecom.messages import make_text_stream
from agentseek_wecom.transports.long_connection import (
    LONG_CONNECTION_REQUEST_ID_KEY,
    AiBotLongConnectionTransport,
    WeComLongConnectionAuthError,
    WeComLongConnectionCommandRejected,
    WeComLongConnectionError,
    WeComProactiveNotEligible,
)
from bub.channels.message import ChannelMessage
from pydantic import SecretStr

_WAIT_TIMEOUT_MESSAGE = "condition was not met before timeout"


class FakeWebSocket:
    def __init__(self, *, subscribe_errcode: int = 0) -> None:
        self.subscribe_errcode = subscribe_errcode
        self.sent: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[str | bytes | BaseException] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        envelope = json.loads(message)
        self.sent.append(envelope)
        request_id = envelope["headers"]["req_id"]
        errcode = self.subscribe_errcode if envelope["cmd"] == "aibot_subscribe" else 0
        await self.incoming.put(
            json.dumps(
                {
                    "headers": {"req_id": request_id},
                    "errcode": errcode,
                    "errmsg": "ok" if errcode == 0 else "rejected",
                }
            )
        )

    async def recv(self) -> str | bytes:
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(ConnectionError("closed"))

    async def push(self, message: dict[str, Any]) -> None:
        await self.incoming.put(json.dumps(message, ensure_ascii=False))


class FakeConnector:
    def __init__(self, *connections: FakeWebSocket) -> None:
        self.connections = list(connections)
        self.calls = 0

    @contextlib.asynccontextmanager
    async def __call__(self, url: str):
        assert url == "wss://openws.work.weixin.qq.com"
        index = min(self.calls, len(self.connections) - 1)
        connection = self.connections[index]
        self.calls += 1
        try:
            yield connection
        finally:
            await connection.close()


def long_settings(tmp_path: Path, **overrides: Any) -> WeComSettings:
    values: dict[str, Any] = {
        "enabled": True,
        "transport_mode": "long_connection",
        "long_connection_bot_id": "bot-1",
        "long_connection_secret": "long-secret",
        "long_connection_command_timeout_seconds": 1.0,
        "long_connection_reconnect_min_seconds": 0.1,
        "long_connection_reconnect_max_seconds": 1.0,
        "long_connection_lock_path": str(tmp_path / "long.lock"),
    }
    values.update(overrides)
    return WeComSettings(**values)


async def wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(_WAIT_TIMEOUT_MESSAGE)
        await asyncio.sleep(0.01)


def sync_async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def test_long_connection_address_has_24_hour_reply_window_and_group_isolation() -> None:
    interacted_at = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    direct = long_connection_conversation_address(
        {
            "msgid": "direct-1",
            "aibotid": "bot-1",
            "from": {"userid": "user-1"},
        },
        tenant_id="tenant-1",
        interacted_at=interacted_at,
    )
    group = long_connection_conversation_address(
        {
            "msgid": "group-1",
            "aibotid": "bot-1",
            "chattype": "group",
            "chatid": "group-alpha",
            "from": {"userid": "user-1"},
        },
        tenant_id="tenant-1",
        interacted_at=interacted_at,
    )

    assert direct.transport == "aibot_long_connection"
    assert direct.session_id == "wecom:user-1"
    assert direct.reply_deadline == interacted_at + timedelta(hours=24)
    assert group.session_id == "wecom:bot-1:group:group-alpha"
    assert group.with_plaintext_userid("plain-user").session_id == group.session_id


@sync_async_test
async def test_subscribe_dispatch_reply_and_proactive_eligibility(tmp_path: Path) -> None:
    websocket = FakeWebSocket()
    transport = AiBotLongConnectionTransport(
        settings=long_settings(tmp_path),
        tenant_id="tenant-1",
        connector=FakeConnector(websocket),
    )
    received: list[dict[str, Any]] = []

    async def handler(data: dict[str, Any]) -> str:
        received.append(data)
        return make_text_stream("stream-1", "首包", False)

    transport.bind_inbound(handler)
    stop_event = asyncio.Event()
    await transport.start(stop_event)
    try:
        subscribe = websocket.sent[0]
        assert subscribe["cmd"] == "aibot_subscribe"
        assert subscribe["body"]["bot_id"] == "bot-1"
        assert transport.subscribed is True

        address = transport.address_for(
            {
                "aibotid": "bot-1",
                "from": {"userid": "user-1"},
            }
        )
        with pytest.raises(WeComProactiveNotEligible):
            await transport.send_proactive(
                address,
                message_type="markdown",
                payload={"content": "too early"},
            )

        await websocket.push(
            {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "callback-1"},
                "body": {
                    "msgid": "message-1",
                    "aibotid": "bot-1",
                    "chattype": "single",
                    "from": {"userid": "user-1"},
                    "msgtype": "text",
                    "text": {"content": "你好"},
                },
            }
        )
        await wait_until(lambda: any(item["cmd"] == "aibot_respond_msg" for item in websocket.sent))
        assert received[0][LONG_CONNECTION_REQUEST_ID_KEY] == "callback-1"
        reply = next(item for item in websocket.sent if item["cmd"] == "aibot_respond_msg")
        assert reply["headers"]["req_id"] == "callback-1"
        assert reply["body"]["stream"]["id"] == "stream-1"

        await transport.send_proactive(
            address,
            message_type="markdown",
            payload={"content": "任务完成"},
            request_id="proactive-1",
        )
        proactive = next(item for item in websocket.sent if item["cmd"] == "aibot_send_msg")
        assert proactive["body"] == {
            "chatid": "user-1",
            "chat_type": 1,
            "msgtype": "markdown",
            "markdown": {"content": "任务完成"},
        }
    finally:
        await transport.stop()


@sync_async_test
async def test_subscription_rejection_fails_closed_and_releases_lock(tmp_path: Path) -> None:
    rejected = FakeWebSocket(subscribe_errcode=40001)
    settings = long_settings(tmp_path)
    transport = AiBotLongConnectionTransport(
        settings=settings,
        tenant_id="tenant-1",
        connector=FakeConnector(rejected),
    )

    with pytest.raises(WeComLongConnectionAuthError):
        await transport.start(asyncio.Event())

    accepted = FakeWebSocket()
    replacement = AiBotLongConnectionTransport(
        settings=settings,
        tenant_id="tenant-1",
        connector=FakeConnector(accepted),
    )
    await replacement.start(asyncio.Event())
    await replacement.stop()


@sync_async_test
async def test_disconnect_reconnects_and_resubscribes(tmp_path: Path) -> None:
    first = FakeWebSocket()
    second = FakeWebSocket()
    connector = FakeConnector(first, second)
    transport = AiBotLongConnectionTransport(
        settings=long_settings(tmp_path),
        tenant_id="tenant-1",
        connector=connector,
    )
    transport.bind_inbound(lambda data: asyncio.sleep(0, result=None))
    await transport.start(asyncio.Event())
    try:
        await first.incoming.put(ConnectionError("network lost"))
        await wait_until(lambda: connector.calls >= 2 and transport.subscribed)
        assert second.sent[0]["cmd"] == "aibot_subscribe"
    finally:
        await transport.stop()


@sync_async_test
async def test_single_instance_lock_rejects_second_owner(tmp_path: Path) -> None:
    settings = long_settings(tmp_path)
    first = AiBotLongConnectionTransport(
        settings=settings,
        tenant_id="tenant-1",
        connector=FakeConnector(FakeWebSocket()),
    )
    second = AiBotLongConnectionTransport(
        settings=settings,
        tenant_id="tenant-1",
        connector=FakeConnector(FakeWebSocket()),
    )
    await first.start(asyncio.Event())
    try:
        with pytest.raises(WeComLongConnectionError, match="another process"):
            await second.start(asyncio.Event())
    finally:
        await first.stop()


@sync_async_test
async def test_channel_sends_initial_stream_before_agent_terminal_reply(tmp_path: Path) -> None:
    settings = long_settings(tmp_path, enabled=False, initial_wait_seconds=0.01)
    transport = AiBotLongConnectionTransport(settings=settings, tenant_id="tenant-1")
    deliver_stream = AsyncMock()
    cast(Any, transport).deliver_stream = deliver_stream
    channel: WeComChannel

    async def receive(message: ChannelMessage) -> None:
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="最终结果",
                is_active=True,
            )
        )

    channel = WeComChannel(
        on_receive=receive,
        settings=settings,
        transport=transport,
    )
    response = await channel._handle_plain_message(
        {
            "msgid": "message-1",
            "aibotid": "bot-1",
            "chattype": "single",
            "from": {"userid": "user-1"},
            "msgtype": "text",
            "text": {"content": "开始任务"},
            LONG_CONNECTION_REQUEST_ID_KEY: "callback-1",
        }
    )
    await wait_until(lambda: deliver_stream.await_count == 2)
    await channel.stop()

    assert response is None
    initial = deliver_stream.await_args_list[0].kwargs
    terminal = deliver_stream.await_args_list[1].kwargs
    assert initial["request_id"] == terminal["request_id"] == "callback-1"
    assert initial["stream_id"] == terminal["stream_id"]
    assert initial["content"] == "已收到，正在处理..."
    assert initial["finish"] is False
    assert terminal["content"] == "最终结果"
    assert terminal["finish"] is True


@sync_async_test
async def test_proactive_probe_sends_markdown_and_button_card_once(tmp_path: Path) -> None:
    settings = long_settings(
        tmp_path,
        enabled=False,
        long_connection_proactive_probe_trigger="M0.5长连接主动消息探针",
    )
    transport = AiBotLongConnectionTransport(settings=settings, tenant_id="tenant-1")
    deliver_stream = AsyncMock()
    send_proactive = AsyncMock()
    cast(Any, transport).deliver_stream = deliver_stream
    cast(Any, transport).send_proactive = send_proactive
    channel = WeComChannel(on_receive=None, settings=settings, transport=transport)
    payload = {
        "msgid": "probe-message-1",
        "aibotid": "bot-1",
        "chattype": "single",
        "from": {"userid": "user-1"},
        "msgtype": "text",
        "text": {"content": "M0.5长连接主动消息探针"},
        LONG_CONNECTION_REQUEST_ID_KEY: "probe-callback-1",
    }
    transport.remember_interaction(transport.address_for(payload))

    first = await channel._handle_plain_message(payload)
    payload[LONG_CONNECTION_REQUEST_ID_KEY] = "probe-callback-duplicate"
    duplicate = await channel._handle_plain_message(payload)
    await channel.stop()

    assert first is None
    assert duplicate is None
    assert deliver_stream.await_count == 2
    assert deliver_stream.await_args_list[0].kwargs["request_id"] == "probe-callback-1"
    assert deliver_stream.await_args_list[1].kwargs["request_id"] == "probe-callback-duplicate"
    assert send_proactive.await_count == 2
    assert send_proactive.await_args_list[0].kwargs == {
        "message_type": "markdown",
        "payload": {"content": "AgentSeek M0.5：长连接主动 Markdown 发送成功。"},
        "request_id": send_proactive.await_args_list[0].kwargs["request_id"],
    }
    card = send_proactive.await_args_list[1].kwargs
    assert card["message_type"] == "template_card"
    assert card["payload"]["card_type"] == "button_interaction"
    assert card["payload"]["button_list"][0]["key"] == "M05_CONFIRM"


@sync_async_test
async def test_card_event_uses_proactive_terminal_without_invalid_stream_reply(tmp_path: Path) -> None:
    settings = long_settings(tmp_path, enabled=False, initial_wait_seconds=0.01)
    transport = AiBotLongConnectionTransport(settings=settings, tenant_id="tenant-1")
    deliver_stream = AsyncMock()
    send_proactive = AsyncMock()
    cast(Any, transport).deliver_stream = deliver_stream
    cast(Any, transport).send_proactive = send_proactive
    channel: WeComChannel

    async def receive(message: ChannelMessage) -> None:
        assert "M05_CONFIRM" in message.content
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="卡片操作已记录",
                is_active=True,
            )
        )

    channel = WeComChannel(on_receive=receive, settings=settings, transport=transport)
    payload = {
        "msgid": "card-event-1",
        "aibotid": "bot-1",
        "chattype": "single",
        "from": {"userid": "user-1"},
        "msgtype": "event",
        "event": {
            "eventtype": "template_card_event",
            "template_card_event": {
                "card_type": "button_interaction",
                "event_key": "M05_CONFIRM",
                "task_id": "task-1",
            },
        },
        LONG_CONNECTION_REQUEST_ID_KEY: "card-callback-1",
    }
    transport.remember_interaction(transport.address_for(payload))

    first = await channel._handle_plain_message(payload)
    duplicate = await channel._handle_plain_message(payload)
    await wait_until(lambda: send_proactive.await_count == 1)
    await channel.stop()

    assert first is None
    assert duplicate is None
    assert deliver_stream.await_count == 0
    assert send_proactive.await_args is not None
    assert send_proactive.await_args.kwargs["message_type"] == "markdown"
    assert send_proactive.await_args.kwargs["payload"] == {"content": "卡片操作已记录"}


@sync_async_test
async def test_long_connection_terminal_outbox_recovers_after_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "durable.sqlite3"
    settings = long_settings(
        tmp_path,
        enabled=False,
        durable_mode="sqlite",
        durable_sqlite_path=str(database_path),
        durable_secret="0123456789abcdef0123456789abcdef",
        initial_wait_seconds=0.01,
    )
    first_transport = AiBotLongConnectionTransport(settings=settings, tenant_id="tenant-1")
    first_delivery = AsyncMock(side_effect=[None, ConnectionError("network lost")])
    cast(Any, first_transport).deliver_stream = first_delivery
    first_channel: WeComChannel

    async def receive(message: ChannelMessage) -> None:
        await first_channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="可恢复终态",
                is_active=True,
            )
        )

    first_channel = WeComChannel(
        on_receive=receive,
        settings=settings,
        transport=first_transport,
    )
    await first_channel._handle_plain_message(
        {
            "msgid": "message-recover-1",
            "aibotid": "bot-1",
            "chattype": "single",
            "from": {"userid": "user-1"},
            "msgtype": "text",
            "text": {"content": "慢任务"},
            LONG_CONNECTION_REQUEST_ID_KEY: "callback-recover-1",
        }
    )
    await wait_until(lambda: first_delivery.await_count == 2)
    await first_channel.stop()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT status FROM wecom_outbox").fetchone() == ("failed",)

    second_transport = AiBotLongConnectionTransport(settings=settings, tenant_id="tenant-1")
    second_delivery = AsyncMock()
    cast(Any, second_transport).deliver_stream = second_delivery
    second_channel = WeComChannel(
        on_receive=None,
        settings=settings,
        transport=second_transport,
    )
    stop_event = asyncio.Event()
    await second_channel.start(stop_event)
    await second_channel.stop()

    assert second_delivery.await_args is not None
    recovered = second_delivery.await_args.kwargs
    assert recovered == {
        "request_id": "callback-recover-1",
        "stream_id": recovered["stream_id"],
        "content": "可恢复终态",
        "finish": True,
    }
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT status FROM wecom_inbox").fetchone() == ("completed",)
        assert connection.execute("SELECT status FROM wecom_outbox").fetchone() == ("delivered",)


@sync_async_test
async def test_rejected_recovered_stream_falls_back_to_durable_proactive_markdown(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-stream.sqlite3"
    key_material = "0123456789abcdef0123456789abcdef"
    settings = long_settings(
        tmp_path,
        enabled=False,
        durable_mode="sqlite",
        durable_sqlite_path=str(database_path),
        durable_secret=key_material,
    )
    payload = {
        "msgid": "legacy-message-1",
        "aibotid": "bot-1",
        "chattype": "group",
        "chatid": "group-alpha",
        "from": {"userid": "user-1"},
        "msgtype": "text",
        "text": {"content": "恢复旧终态"},
        LONG_CONNECTION_REQUEST_ID_KEY: "expired-callback-request",
    }
    transport = AiBotLongConnectionTransport(settings=settings, tenant_id="tenant-1")
    address = transport.address_for(payload)
    store = SqliteDurableMessageStore(path=database_path, secret=SecretStr(key_material))
    now = datetime.now(UTC)
    store.remember_interaction(address, now=now)
    inbox = store.admit_inbound(
        message_id="legacy-message-1",
        address=address,
        stream_id="legacy-stream-1",
        payload=payload,
        now=now,
    ).record
    store.mark_inbox(inbox.inbox_id, "processing", now=now)
    store.enqueue_outbox(
        inbox_id=inbox.inbox_id,
        stream_id=inbox.stream_id,
        message_type="long_connection_stream",
        envelope={
            "request_id": "expired-callback-request",
            "content": "恢复后主动送达",
            "finish": True,
        },
        reply_deadline=address.reply_deadline,
        now=now,
    )
    rejected_stream = AsyncMock(side_effect=WeComLongConnectionCommandRejected("stream request rejected"))
    proactive = AsyncMock()
    cast(Any, transport).deliver_stream = rejected_stream
    cast(Any, transport).send_proactive = proactive
    channel = WeComChannel(
        on_receive=None,
        settings=settings,
        transport=transport,
        durable_store=store,
    )

    await channel.start(asyncio.Event())
    await channel.stop()

    assert rejected_stream.await_count == 1
    assert proactive.await_count == 1
    assert proactive.await_args is not None
    assert proactive.await_args.kwargs["message_type"] == "markdown"
    assert proactive.await_args.kwargs["payload"] == {"content": "恢复后主动送达"}
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT status FROM wecom_inbox").fetchone() == ("completed",)
        assert connection.execute(
            "SELECT COUNT(*) FROM wecom_outbox WHERE status = 'delivered'"
        ).fetchone() == (2,)


@sync_async_test
async def test_recovered_inbox_skips_stale_stream_and_delivers_terminal_proactively(tmp_path: Path) -> None:
    database_path = tmp_path / "recovered-inbox.sqlite3"
    key_material = "0123456789abcdef0123456789abcdef"
    settings = long_settings(
        tmp_path,
        enabled=False,
        durable_mode="sqlite",
        durable_sqlite_path=str(database_path),
        durable_secret=key_material,
    )
    payload = {
        "msgid": "recover-inbox-message-1",
        "aibotid": "bot-1",
        "chattype": "group",
        "chatid": "group-beta",
        "from": {"userid": "user-1"},
        "msgtype": "text",
        "text": {"content": "恢复未完成回合"},
        LONG_CONNECTION_REQUEST_ID_KEY: "stale-callback-request",
    }
    transport = AiBotLongConnectionTransport(settings=settings, tenant_id="tenant-1")
    address = transport.address_for(payload)
    store = SqliteDurableMessageStore(path=database_path, secret=SecretStr(key_material))
    store.remember_interaction(address, now=datetime.now(UTC))
    store.admit_inbound(
        message_id="recover-inbox-message-1",
        address=address,
        stream_id="recover-inbox-stream-1",
        payload=payload,
        now=datetime.now(UTC),
    )
    stale_stream = AsyncMock(side_effect=AssertionError("stale stream must not be used"))
    proactive = AsyncMock()
    cast(Any, transport).deliver_stream = stale_stream
    cast(Any, transport).send_proactive = proactive
    channel: WeComChannel

    async def receive(message: ChannelMessage) -> None:
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="恢复回合主动终态",
                is_active=True,
            )
        )

    channel = WeComChannel(
        on_receive=receive,
        settings=settings,
        transport=transport,
        durable_store=store,
    )

    await channel.start(asyncio.Event())
    await wait_until(lambda: proactive.await_count == 1)
    await channel.stop()

    assert stale_stream.await_count == 0
    assert proactive.await_args is not None
    assert proactive.await_args.kwargs["message_type"] == "markdown"
    assert proactive.await_args.kwargs["payload"] == {"content": "恢复回合主动终态"}
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT status FROM wecom_inbox").fetchone() == ("completed",)
        assert connection.execute("SELECT status FROM wecom_outbox").fetchone() == ("delivered",)


@sync_async_test
async def test_proactive_markdown_is_qualified_durable_and_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "proactive.sqlite3"
    settings = long_settings(
        tmp_path,
        enabled=False,
        durable_mode="sqlite",
        durable_sqlite_path=str(database_path),
        durable_secret="0123456789abcdef0123456789abcdef",
    )
    transport = AiBotLongConnectionTransport(settings=settings, tenant_id="tenant-1")
    proactive = AsyncMock()
    cast(Any, transport).send_proactive = proactive
    address = transport.address_for(
        {
            "msgid": "message-qualification",
            "aibotid": "bot-1",
            "chattype": "group",
            "chatid": "group-alpha",
            "from": {"userid": "user-1"},
        }
    )
    transport.remember_interaction(address)
    channel = WeComChannel(on_receive=None, settings=settings, transport=transport)

    first = await channel.send_proactive_markdown(
        address,
        "任务已经完成",
        idempotency_key="work-123:completed",
    )
    duplicate = await channel.send_proactive_markdown(
        address,
        "任务已经完成",
        idempotency_key="work-123:completed",
    )
    await channel.stop()

    assert first == "succeeded"
    assert duplicate == "skipped"
    assert proactive.await_count == 1
    assert proactive.await_args is not None
    assert proactive.await_args.kwargs["message_type"] == "markdown"
    assert proactive.await_args.kwargs["payload"] == {"content": "任务已经完成"}
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT status FROM wecom_outbox").fetchone() == ("delivered",)
    database_bytes = database_path.read_bytes()
    assert "任务已经完成".encode() not in database_bytes
    assert b"work-123:completed" not in database_bytes
