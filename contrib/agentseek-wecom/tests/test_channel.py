from __future__ import annotations

import asyncio
import json
from typing import Any

from agentseek_wecom.channel import WeComChannel
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.crypto import WeComJsonCrypto
from bub.channels.message import ChannelMessage
from fastapi.testclient import TestClient
from republic import StreamEvent


class FakeUseridResolver:
    def __init__(self, userid: str | None) -> None:
        self.userid = userid
        self.calls: list[str] = []

    def resolve(self, open_userid: str) -> str | None:
        self.calls.append(open_userid)
        return self.userid


def _channel(userid_resolver: FakeUseridResolver | None = None) -> WeComChannel:
    return WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            callback_path="/callback/{botid}",
            initial_wait_seconds=0,
            userid_resolve_mode="",
        ),
        userid_resolver=userid_resolver,
    )


def test_text_message_creates_stream_and_emits_channel_message() -> None:
    received: list[ChannelMessage] = []
    channel = _channel()

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="处理完成",
            )
        )

    channel.bind_receiver(on_receive)

    plain = asyncio.run(
        channel._handle_plain_message(
            {
                "msgtype": "text",
                "from": {"userid": "chenkang2"},
                "text": {"content": "帮我查一下制度"},
            }
        )
    )
    payload = json.loads(plain or "{}")

    assert payload["msgtype"] == "stream"
    assert payload["stream"]["finish"] is True
    assert payload["stream"]["content"] == "处理完成"
    assert received[0].session_id == "wecom:chenkang2"
    assert received[0].context["oa_account"] == "chenkang2"
    assert received[0].content == "帮我查一下制度"


def test_text_message_resolves_open_userid_before_dispatch() -> None:
    received: list[ChannelMessage] = []
    resolver = FakeUseridResolver("zhuchunlin")
    channel = _channel(userid_resolver=resolver)

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="处理完成",
            )
        )

    channel.bind_receiver(on_receive)

    plain = asyncio.run(
        channel._handle_plain_message(
            {
                "msgtype": "text",
                "from": {"userid": "encrypted-open-userid"},
                "text": {"content": "你好"},
            }
        )
    )
    payload = json.loads(plain or "{}")

    assert resolver.calls == ["encrypted-open-userid"]
    assert payload["stream"]["content"] == "处理完成"
    assert received[0].session_id == "wecom:zhuchunlin"
    assert received[0].chat_id == "zhuchunlin"
    assert received[0].context["from_userid"] == "encrypted-open-userid"
    assert received[0].context["userid"] == "zhuchunlin"
    assert received[0].context["oa_account"] == "zhuchunlin"
    assert received[0].context["wecom"]["open_userid"] == "encrypted-open-userid"
    assert received[0].context["wecom"]["resolved_userid"] == "zhuchunlin"


def test_text_message_sanitizes_wecom_raw_payload() -> None:
    received: list[ChannelMessage] = []
    channel = _channel()

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="处理完成",
            )
        )

    channel.bind_receiver(on_receive)

    asyncio.run(
        channel._handle_plain_message(
            {
                "msgid": "m1",
                "aibotid": "bot-1",
                "chattype": "single",
                "msgtype": "text",
                "from": {"userid": "chenkang2"},
                "responseurl": "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=secret",
                "text": {"content": "你好"},
            }
        )
    )

    raw = received[0].context["wecom"]["raw"]
    assert raw == {
        "msgid": "m1",
        "aibotid": "bot-1",
        "chattype": "single",
        "msgtype": "text",
        "from": {"userid": "chenkang2"},
        "text": {"content": "你好"},
    }
    assert "responseurl" not in raw


def test_stream_poll_returns_latest_content() -> None:
    channel = _channel()
    stream = asyncio.run(channel._create_stream(session_id="wecom:u1", chat_id="u1", from_userid="u1"))
    stream.update(content="当前答案", finish=False)

    plain = asyncio.run(channel._handle_plain_message({"msgtype": "stream", "stream": {"id": stream.stream_id}}))
    payload = json.loads(plain or "{}")

    assert payload["stream"]["id"] == stream.stream_id
    assert payload["stream"]["content"] == "当前答案"
    assert payload["stream"]["finish"] is False


def test_stream_events_appends_text_chunks() -> None:
    channel = _channel()
    stream = asyncio.run(channel._create_stream(session_id="wecom:u1", chat_id="u1", from_userid="u1"))
    message = ChannelMessage(session_id="wecom:u1", channel="wecom", chat_id="u1", content="hi")
    setattr(message, "_agentseek_wecom_stream_id", stream.stream_id)

    async def events():
        yield StreamEvent("text", {"delta": "你"})
        yield StreamEvent("text", {"delta": "好"})

    async def collect() -> list[Any]:
        return [event async for event in channel.stream_events(message, events())]

    collected = asyncio.run(collect())

    assert [event.kind for event in collected] == ["text", "text"]
    assert stream.content.endswith("你好")


def test_enter_chat_event_returns_welcome_text() -> None:
    channel = _channel()

    plain = asyncio.run(
        channel._handle_plain_message({"msgtype": "event", "event": {"eventtype": "enter_chat"}})
    )
    payload = json.loads(plain or "{}")

    assert payload["msgtype"] == "text"
    assert "企业数字员工" in payload["text"]["content"]


def test_http_callback_decrypts_dispatches_and_encrypts_stream_response() -> None:
    received: list[ChannelMessage] = []
    channel = _channel()
    crypto = WeComJsonCrypto(token="token", encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG")

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="HTTP处理完成",
            )
        )

    channel.bind_receiver(on_receive)
    client = TestClient(channel.app)
    encrypted_request = crypto.encrypt_message(
        json.dumps(
            {
                "msgtype": "text",
                "from": {"userid": "chenkang2"},
                "text": {"content": "测试HTTP回调"},
            },
            ensure_ascii=False,
        ),
        nonce="nonce",
        timestamp="1",
    )

    response = client.post(
        "/callback/bot-1",
        params={
            "msg_signature": encrypted_request.msg_signature,
            "timestamp": "1",
            "nonce": "nonce",
        },
        json={"encrypt": encrypted_request.encrypt},
    )

    assert response.status_code == 200
    response_body = response.json()
    decrypted_response = crypto.decrypt_message(
        post_data=json.dumps({"encrypt": response_body["encrypt"]}),
        msg_signature=response_body["msgsignature"],
        timestamp=response_body["timestamp"],
        nonce=response_body["nonce"],
    )
    stream_payload = json.loads(decrypted_response)

    assert stream_payload["stream"]["content"] == "HTTP处理完成"
    assert stream_payload["stream"]["finish"] is True
    assert received[0].content == "测试HTTP回调"
