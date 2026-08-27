from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

from agentseek_wecom.addressing import callback_conversation_address
from agentseek_wecom.channel import WeComChannel
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.transport import InboundMessageHandler
from agentseek_wecom.transports.callback import AiBotCallbackTransport


class HeadlessTransport:
    app = None

    def __init__(self) -> None:
        self.handler: InboundMessageHandler | None = None

    @property
    def kind(self) -> Literal["aibot_long_connection"]:
        return "aibot_long_connection"

    def bind_inbound(self, handler: InboundMessageHandler) -> None:
        self.handler = handler

    def address_for(self, data, *, plaintext_userid=None):
        return callback_conversation_address(
            data,
            tenant_id="tenant-1",
            plaintext_userid=plaintext_userid,
        )

    async def start(self, stop_event: asyncio.Event) -> None:
        del stop_event

    async def stop(self) -> None:
        return None


def test_callback_address_preserves_direct_message_session_compatibility() -> None:
    interacted_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    payload = {
        "msgid": "msg-direct",
        "msgtype": "text",
        "aibotid": "bot-1",
        "from": {"userid": "encrypted-user"},
        "responseurl": "https://qyapi.weixin.qq.com/redacted",
    }

    encrypted = callback_conversation_address(
        payload,
        tenant_id="tenant-1",
        interacted_at=interacted_at,
    )
    plaintext = encrypted.with_plaintext_userid("zhuchunlin")

    assert encrypted.transport == "aibot_callback"
    assert encrypted.chat_type == "single"
    assert encrypted.chat_id == "encrypted-user"
    assert encrypted.session_id == "wecom:encrypted-user"
    assert encrypted.reply_deadline == interacted_at + timedelta(hours=1)
    assert plaintext.sender_userid == "encrypted-user"
    assert plaintext.plaintext_userid == "zhuchunlin"
    assert plaintext.chat_id == "zhuchunlin"
    assert plaintext.session_id == "wecom:zhuchunlin"


def test_callback_address_keeps_group_boundary_when_userid_is_decrypted() -> None:
    payload = {
        "msgid": "msg-group",
        "msgtype": "text",
        "chattype": "group",
        "chatid": "chat-alpha",
        "aibotid": "bot-1",
        "from": {"userid": "encrypted-user"},
    }

    address = callback_conversation_address(payload, tenant_id="tenant-1")
    plaintext = address.with_plaintext_userid("zhuchunlin")

    assert address.session_id == "wecom:bot-1:group:chat-alpha"
    assert plaintext.session_id == address.session_id
    assert plaintext.chat_id == "chat-alpha"
    assert plaintext.reply_deadline == address.last_interacted_at + timedelta(minutes=6)


def test_callback_address_isolates_malformed_group_by_message_id() -> None:
    address = callback_conversation_address(
        {
            "msgid": "msg-without-chat",
            "chattype": "group",
            "aibotid": "bot-1",
            "from": {"userid": "encrypted-user"},
        },
        tenant_id="tenant-1",
    )

    assert address.chat_id == "missing-chatid:msg-without-chat"
    assert address.session_id == "wecom:bot-1:group:missing-chatid:msg-without-chat"


def test_safe_address_context_never_contains_response_capability() -> None:
    address = callback_conversation_address(
        {
            "msgid": "msg-direct",
            "aibotid": "bot-1",
            "from": {"userid": "encrypted-user"},
            "responseurl": "https://qyapi.weixin.qq.com/secret-capability",
        },
        tenant_id="tenant-1",
    )

    context = address.to_safe_context()

    assert context["transport"] == "aibot_callback"
    assert context["reply_deadline"] is not None
    assert "response" not in " ".join(str(value) for value in context.values()).lower()
    assert "secret-capability" not in str(context)


def test_callback_transport_owns_http_lifecycle_and_addressing() -> None:
    transport = AiBotCallbackTransport(
        settings=WeComSettings(enabled=False),
        tenant_id="tenant-1",
    )

    address = transport.address_for({"from": {"userid": "user-1"}})

    assert transport.kind == "aibot_callback"
    assert transport.app is not None
    assert address.tenant_id == "tenant-1"
    assert address.session_id == "wecom:user-1"


def test_channel_kernel_accepts_a_headless_transport() -> None:
    transport = HeadlessTransport()

    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(enabled=False),
        transport=transport,
    )

    assert channel.transport is transport
    assert channel.app is None
    assert transport.handler is not None
