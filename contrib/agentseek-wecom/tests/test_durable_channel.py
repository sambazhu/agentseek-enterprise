from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from agentseek_wecom.addressing import callback_conversation_address
from agentseek_wecom.channel import WeComChannel
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.durable import InboxStatus, SqliteDurableMessageStore
from agentseek_wecom.transport import InboundMessageHandler
from bub.channels.message import ChannelMessage
from pydantic import SecretStr

TEST_KEY_MATERIAL = "durable-channel-test-key-material-with-32-characters"


class WaitConditionTimeout(AssertionError):
    pass


class HeadlessCallbackTransport:
    app = None

    def __init__(self) -> None:
        self.handler: InboundMessageHandler | None = None

    @property
    def kind(self) -> Literal["aibot_callback"]:
        return "aibot_callback"

    def bind_inbound(self, handler: InboundMessageHandler) -> None:
        self.handler = handler

    def address_for(self, data: dict[str, Any], *, plaintext_userid: str | None = None):
        return callback_conversation_address(
            data,
            tenant_id="tenant-1",
            plaintext_userid=plaintext_userid,
        )

    async def start(self, stop_event: asyncio.Event) -> None:
        del stop_event

    async def stop(self) -> None:
        return None


class RecordingSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.card_calls: list[tuple[str, dict[str, Any]]] = []

    def send_markdown(self, response_url: str, content: str) -> None:
        self.calls.append((response_url, content))

    def send_template_card(self, response_url: str, template_card: Mapping[str, Any]) -> None:
        self.card_calls.append((response_url, dict(template_card)))


class DelayedCompletionStore(SqliteDurableMessageStore):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.completion_started = threading.Event()
        self.allow_completion = threading.Event()

    def mark_inbox(
        self,
        inbox_id: str,
        status: InboxStatus,
        *,
        now: datetime,
        error_type: str = "",
    ) -> None:
        if status == "completed":
            self.completion_started.set()
            self.allow_completion.wait(timeout=2.0)
        super().mark_inbox(inbox_id, status, now=now, error_type=error_type)


def _settings(path, **overrides: Any) -> WeComSettings:
    values: dict[str, Any] = {
        "enabled": False,
        "durable_mode": "sqlite",
        "durable_sqlite_path": str(path),
        "durable_secret": SecretStr(TEST_KEY_MATERIAL),
        "initial_wait_seconds": 0.01,
        "userid_resolve_mode": "",
    }
    values.update(overrides)
    return WeComSettings(**values)


def _payload(*, msgid: str = "durable-message-1", response_url: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "msgid": msgid,
        "msgtype": "text",
        "aibotid": "bot-1",
        "from": {"userid": "encrypted-user"},
        "text": {"content": "请回复持久化正常"},
    }
    if response_url:
        payload["responseurl"] = "https://qyapi.weixin.qq.com/durable-response-capability"
    return payload


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise WaitConditionTimeout


def test_completed_message_is_not_dispatched_again_after_restart(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "wecom.sqlite3"
        settings = _settings(path)
        first_calls: list[str] = []
        first_sender = RecordingSender()
        first_store = DelayedCompletionStore(path=path, secret=SecretStr(TEST_KEY_MATERIAL))
        first = WeComChannel(
            on_receive=None,
            settings=settings,
            transport=HeadlessCallbackTransport(),
            response_url_sender=first_sender,
            durable_store=first_store,
        )

        async def first_receive(message: ChannelMessage) -> None:
            first_calls.append(message.content)
            await first.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content="持久化正常",
                )
            )

        first.bind_receiver(first_receive)
        await first._handle_plain_message(_payload())
        completion_started = await asyncio.to_thread(first_store.completion_started.wait, 1.0)
        stop_task = asyncio.create_task(first.stop())
        await asyncio.sleep(0.05)
        stop_waited_for_completion = not stop_task.done()
        first_store.allow_completion.set()
        await stop_task

        second_calls: list[str] = []
        second = WeComChannel(
            on_receive=lambda message: second_calls.append(message.content),
            settings=settings,
            transport=HeadlessCallbackTransport(),
            response_url_sender=RecordingSender(),
        )
        duplicate_reply = await second._handle_plain_message(_payload())
        await asyncio.sleep(0.05)
        await second.stop()

        assert first_calls == ["请回复持久化正常"]
        assert completion_started is True
        assert first_sender.calls == [
            ("https://qyapi.weixin.qq.com/durable-response-capability", "持久化正常")
        ]
        assert stop_waited_for_completion is True
        assert second_calls == []
        assert duplicate_reply is not None
        assert "已经处理" in duplicate_reply

    asyncio.run(scenario())


def test_pending_inbox_is_recovered_and_delivered_on_start(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "wecom.sqlite3"
        settings = _settings(path)
        store = SqliteDurableMessageStore(path=path, secret=SecretStr(TEST_KEY_MATERIAL))
        payload = _payload(msgid="recover-inbox")
        now = datetime.now(UTC)
        address = callback_conversation_address(payload, tenant_id="tenant-1", interacted_at=now)
        store.admit_inbound(
            message_id="recover-inbox",
            address=address,
            stream_id="recover-stream",
            payload=payload,
            now=now,
        )
        sender = RecordingSender()
        received: list[str] = []
        channel = WeComChannel(
            on_receive=None,
            settings=settings,
            transport=HeadlessCallbackTransport(),
            response_url_sender=sender,
            durable_store=store,
        )

        async def on_receive(message: ChannelMessage) -> None:
            received.append(message.content)
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content="重启恢复正常",
                )
            )

        channel.bind_receiver(on_receive)
        await channel.start(asyncio.Event())
        await _wait_until(lambda: len(sender.calls) == 1)
        await channel.stop()

        assert received == ["请回复持久化正常"]
        assert sender.calls[0][1] == "重启恢复正常"
        assert store.claim_recoverable_inbox(
            now=datetime.now(UTC) + timedelta(minutes=2),
            owner="verification",
            lease_duration=timedelta(seconds=60),
            limit=10,
        ) == []

    asyncio.run(scenario())


def test_pending_markdown_outbox_is_delivered_before_inbox_replay(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "wecom.sqlite3"
        settings = _settings(path)
        store = SqliteDurableMessageStore(path=path, secret=SecretStr(TEST_KEY_MATERIAL))
        payload = _payload(msgid="recover-outbox")
        now = datetime.now(UTC)
        address = callback_conversation_address(payload, tenant_id="tenant-1", interacted_at=now)
        inbox = store.admit_inbound(
            message_id="recover-outbox",
            address=address,
            stream_id="recover-outbox-stream",
            payload=payload,
            now=now,
        ).record
        store.mark_inbox(inbox.inbox_id, "processing", now=now)
        store.enqueue_outbox(
            inbox_id=inbox.inbox_id,
            stream_id=inbox.stream_id,
            message_type="markdown",
            envelope={"response_url": payload["responseurl"], "content": "已生成但尚未投递"},
            reply_deadline=address.reply_deadline,
            now=now,
        )
        sender = RecordingSender()
        replayed: list[str] = []
        channel = WeComChannel(
            on_receive=lambda message: replayed.append(message.content),
            settings=settings,
            transport=HeadlessCallbackTransport(),
            response_url_sender=sender,
            durable_store=store,
        )

        await channel.start(asyncio.Event())
        await channel.stop()

        assert sender.calls == [(payload["responseurl"], "已生成但尚未投递")]
        assert replayed == []

    asyncio.run(scenario())


def test_template_card_outbox_requires_manual_reconciliation_after_restart(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "wecom.sqlite3"
        settings = _settings(path)
        store = SqliteDurableMessageStore(path=path, secret=SecretStr(TEST_KEY_MATERIAL))
        now = datetime.now(UTC)
        store.enqueue_outbox(
            inbox_id=None,
            stream_id="card-stream",
            message_type="template_card",
            envelope={
                "response_url": "https://qyapi.weixin.qq.com/card-capability",
                "template_card": {"card_type": "text_notice"},
            },
            reply_deadline=now + timedelta(hours=1),
            now=now,
        )
        sender = RecordingSender()
        channel = WeComChannel(
            on_receive=None,
            settings=settings,
            transport=HeadlessCallbackTransport(),
            response_url_sender=sender,
            durable_store=store,
        )

        await channel.start(asyncio.Event())
        await channel.stop()

        assert sender.calls == []
        assert sender.card_calls == []
        assert store.claim_recoverable_outbox(
            now=now + timedelta(minutes=2),
            owner="verification",
            lease_duration=timedelta(seconds=60),
            limit=10,
        ) == []

    asyncio.run(scenario())


def test_inbox_without_response_capability_is_blocked_on_restart(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "wecom.sqlite3"
        settings = _settings(path)
        store = SqliteDurableMessageStore(path=path, secret=SecretStr(TEST_KEY_MATERIAL))
        payload = _payload(msgid="no-capability", response_url=False)
        now = datetime.now(UTC)
        address = callback_conversation_address(payload, tenant_id="tenant-1", interacted_at=now)
        store.admit_inbound(
            message_id="no-capability",
            address=address,
            stream_id="no-capability-stream",
            payload=payload,
            now=now,
        )
        replayed: list[str] = []
        channel = WeComChannel(
            on_receive=lambda message: replayed.append(message.content),
            settings=settings,
            transport=HeadlessCallbackTransport(),
            response_url_sender=RecordingSender(),
            durable_store=store,
        )

        await channel.start(asyncio.Event())
        await channel.stop()

        assert replayed == []
        assert store.claim_recoverable_inbox(
            now=now + timedelta(seconds=1),
            owner="verification",
            lease_duration=timedelta(seconds=60),
            limit=10,
        ) == []

    asyncio.run(scenario())


def test_periodic_recovery_claims_a_lease_that_expires_after_startup(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "wecom.sqlite3"
        settings = _settings(path, durable_recovery_interval_seconds=0.05)
        store = SqliteDurableMessageStore(path=path, secret=SecretStr(TEST_KEY_MATERIAL))
        payload = _payload(msgid="expired-after-startup")
        now = datetime.now(UTC)
        address = callback_conversation_address(payload, tenant_id="tenant-1", interacted_at=now)
        inbox = store.admit_inbound(
            message_id="expired-after-startup",
            address=address,
            stream_id="expired-after-startup-stream",
            payload=payload,
            now=now,
        ).record
        claimed = store.claim_inbox(
            inbox.inbox_id,
            now=now,
            owner="dead-process",
            lease_duration=timedelta(seconds=0.12),
        )
        sender = RecordingSender()
        received: list[str] = []
        channel = WeComChannel(
            on_receive=None,
            settings=settings,
            transport=HeadlessCallbackTransport(),
            response_url_sender=sender,
            durable_store=store,
        )

        async def on_receive(message: ChannelMessage) -> None:
            received.append(message.content)
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content="周期恢复正常",
                )
            )

        channel.bind_receiver(on_receive)
        await channel.start(asyncio.Event())
        assert sender.calls == []
        await _wait_until(lambda: len(sender.calls) == 1)
        await channel.stop()

        assert claimed is not None
        assert received == ["请回复持久化正常"]
        assert sender.calls[0][1] == "周期恢复正常"

    asyncio.run(scenario())
