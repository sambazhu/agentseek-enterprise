from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

WeComTransportKind = Literal["aibot_callback", "aibot_long_connection", "wecom_app"]
WeComChatType = Literal["single", "group"]

_CALLBACK_STREAM_REPLY_TTL = timedelta(minutes=6)
_CALLBACK_RESPONSE_URL_TTL = timedelta(hours=1)
_LONG_CONNECTION_REPLY_TTL = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class ConversationAddress:
    """Transport-neutral address for one WeCom conversation interaction."""

    tenant_id: str
    bot_or_agent_id: str
    transport: WeComTransportKind
    chat_type: WeComChatType
    chat_id: str
    sender_userid: str | None
    plaintext_userid: str | None
    last_interacted_at: datetime
    reply_deadline: datetime | None

    @property
    def effective_userid(self) -> str | None:
        return self.plaintext_userid or self.sender_userid

    @property
    def session_id(self) -> str:
        if self.chat_type == "group":
            return f"wecom:{self.bot_or_agent_id}:group:{self.chat_id}"
        return f"wecom:{self.effective_userid or 'unknown'}"

    def with_plaintext_userid(self, plaintext_userid: str | None) -> ConversationAddress:
        value = plaintext_userid.strip() if plaintext_userid else None
        if self.chat_type == "single" and value:
            return replace(self, plaintext_userid=value, chat_id=value)
        return replace(self, plaintext_userid=value or None)

    def to_safe_context(self) -> dict[str, str | None]:
        """Return address metadata without reply capabilities or signed URLs."""

        return {
            "tenant_id": self.tenant_id,
            "bot_or_agent_id": self.bot_or_agent_id,
            "transport": self.transport,
            "chat_type": self.chat_type,
            "chat_id": self.chat_id,
            "sender_userid": self.sender_userid,
            "plaintext_userid": self.plaintext_userid,
            "last_interacted_at": self.last_interacted_at.isoformat(),
            "reply_deadline": self.reply_deadline.isoformat() if self.reply_deadline else None,
        }


def callback_conversation_address(
    data: dict[str, Any],
    *,
    tenant_id: str,
    plaintext_userid: str | None = None,
    interacted_at: datetime | None = None,
) -> ConversationAddress:
    """Normalize an AI Bot callback payload into the shared address contract."""

    now = interacted_at or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("interacted_at must be timezone-aware")

    sender_userid = _extract_sender_userid(data)
    chat_type: WeComChatType = "group" if str(data.get("chattype") or "single") == "group" else "single"
    bot_id = str(data.get("aibotid") or "unknown-bot").strip() or "unknown-bot"
    if chat_type == "group":
        chat_id = str(data.get("chatid") or "").strip()
        if not chat_id:
            message_id = str(data.get("msgid") or "").strip()
            chat_id = f"missing-chatid:{message_id or uuid4().hex}"
    else:
        effective_userid = (plaintext_userid or sender_userid or "").strip()
        chat_id = effective_userid or "wecom:unknown"

    has_response_url = bool(data.get("responseurl") or data.get("response_url"))
    reply_ttl = _CALLBACK_RESPONSE_URL_TTL if has_response_url else _CALLBACK_STREAM_REPLY_TTL
    return ConversationAddress(
        tenant_id=tenant_id.strip() or "default",
        bot_or_agent_id=bot_id,
        transport="aibot_callback",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_userid=sender_userid,
        plaintext_userid=plaintext_userid.strip() if plaintext_userid else None,
        last_interacted_at=now,
        reply_deadline=now + reply_ttl,
    )


def long_connection_conversation_address(
    data: dict[str, Any],
    *,
    tenant_id: str,
    plaintext_userid: str | None = None,
    interacted_at: datetime | None = None,
) -> ConversationAddress:
    """Normalize an AI Bot long-connection callback into the shared address contract."""

    now = interacted_at or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("interacted_at must be timezone-aware")

    sender_userid = _extract_sender_userid(data)
    chat_type: WeComChatType = "group" if str(data.get("chattype") or "single") == "group" else "single"
    bot_id = str(data.get("aibotid") or "unknown-bot").strip() or "unknown-bot"
    if chat_type == "group":
        chat_id = str(data.get("chatid") or "").strip()
        if not chat_id:
            message_id = str(data.get("msgid") or "").strip()
            chat_id = f"missing-chatid:{message_id or uuid4().hex}"
    else:
        effective_userid = (plaintext_userid or sender_userid or "").strip()
        chat_id = effective_userid or "wecom:unknown"

    return ConversationAddress(
        tenant_id=tenant_id.strip() or "default",
        bot_or_agent_id=bot_id,
        transport="aibot_long_connection",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_userid=sender_userid,
        plaintext_userid=plaintext_userid.strip() if plaintext_userid else None,
        last_interacted_at=now,
        reply_deadline=now + _LONG_CONNECTION_REPLY_TTL,
    )


def app_conversation_address(
    data: dict[str, Any],
    *,
    tenant_id: str,
    agent_id: str,
    interacted_at: datetime | None = None,
) -> ConversationAddress:
    """Normalize one self-built application callback into the shared address contract."""

    now = interacted_at or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("interacted_at must be timezone-aware")
    sender_userid = _extract_sender_userid(data)
    effective_userid = (sender_userid or "").strip()
    return ConversationAddress(
        tenant_id=tenant_id.strip() or "default",
        bot_or_agent_id=agent_id.strip() or "unknown-agent",
        transport="wecom_app",
        chat_type="single",
        chat_id=effective_userid or "wecom:unknown",
        sender_userid=sender_userid,
        plaintext_userid=effective_userid or None,
        last_interacted_at=now,
        reply_deadline=None,
    )


def _extract_sender_userid(data: dict[str, Any]) -> str | None:
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
