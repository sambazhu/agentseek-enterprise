from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from agentseek_files.inbound import InboundFileResult
from agentseek_files.models import FileRecord
from agentseek_files.store import FileStoreError
from agentseek_wecom.channel import StreamReply, WeComChannel
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.crypto import WeComJsonCrypto
from agentseek_wecom.media import MediaDownload
from agentseek_wecom.outbound import (
    ArtifactDownload,
    ArtifactDownloadGone,
    TemplateCardIntent,
    register_artifact_download_resolver,
    register_template_card_intent,
)
from agentseek_wecom.transports.callback import AiBotCallbackTransport
from bub.channels.message import ChannelMessage
from fastapi.testclient import TestClient
from republic import StreamEvent

LEGACY_DOC_NOTICE = "暂不支持旧版 Office 格式 .doc，请转换为 .docx 后重新上传。"


class FakeUseridResolver:
    def __init__(self, userid: str | None) -> None:
        self.userid = userid
        self.calls: list[str] = []

    def resolve(self, open_userid: str) -> str | None:
        self.calls.append(open_userid)
        return self.userid


class FakeMediaClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def download(self, media_id: str, *, fallback_filename: str, fallback_mime_type: str) -> MediaDownload:
        self.calls.append(media_id)
        return MediaDownload(
            media_id=media_id,
            data=b"hello file",
            filename=fallback_filename,
            mime_type=fallback_mime_type,
        )

    def download_media(
        self,
        url: str,
        *,
        aes_key: bytes,
        fallback_filename: str,
        fallback_mime_type: str,
    ) -> MediaDownload:
        assert len(aes_key) == 32
        self.calls.append(url)
        return MediaDownload(
            media_id="redacted-url",
            data=b"hello file",
            filename=fallback_filename or "document_20260710_000000_000000.txt",
            mime_type="text/plain" if not fallback_filename else fallback_mime_type,
        )


class FakeResponseUrlSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.card_calls: list[tuple[str, dict[str, Any]]] = []

    def send_markdown(self, response_url: str, content: str) -> None:
        self.calls.append((response_url, content))

    def send_template_card(self, response_url: str, template_card: Mapping[str, Any]) -> None:
        self.card_calls.append((response_url, dict(template_card)))


class FakeFileService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def handle_bytes(
        self,
        *,
        scope: Any,
        filename: str,
        data: bytes,
        mime_type: str,
    ) -> InboundFileResult:
        self.calls.append({"scope": scope, "filename": filename, "data": data, "mime_type": mime_type})
        record = FileRecord(
            file_id="file_abc",
            direction="inbound",
            tenant_key=scope.tenant_key,
            employee_key=scope.employee_key,
            session_key=scope.session_key,
            date="2026-07-09",
            filename=filename,
            sanitized_filename=filename,
            mime_type=mime_type,
            size_bytes=len(data),
            sha256="abc",
            relative_dir="hmac-t/hmac-e/2026-07-09/hmac-s/inbound/file_abc",
            created_at="2026-07-09T00:00:00+00:00",
            extract_status="done",
            extract_chars=10,
        )
        return InboundFileResult(
            record=record,
            context_block="[CurrentFiles]\n- file_id: file_abc\n  excerpt: hello file",
            user_notice="已收到并解析文件：report.txt。",
            extract_text="hello file",
        )

    async def poll_pending(self, record: Any) -> Any:
        raise UnexpectedPollError


class UnexpectedPollError(AssertionError):
    pass


class PendingThenDoneFileService(FakeFileService):
    def __init__(self) -> None:
        super().__init__()
        self.settings = SimpleNamespace(mineru_poll_timeout_s=300.0, mineru_poll_interval_s=2.0)
        self.poll_calls = 0

    async def handle_bytes(self, **kwargs: Any) -> InboundFileResult:
        result = await super().handle_bytes(**kwargs)
        result.record.extract_status = "pending"
        result.record.extract_chars = 0
        result.context_block = "[CurrentFiles]\n  extract_status: pending\n[/CurrentFiles]"
        result.user_notice = "文件正在解析"
        result.extract_text = ""
        result.pending = True
        return result

    async def poll_pending(self, record: FileRecord) -> InboundFileResult:
        self.poll_calls += 1
        record.extract_status = "done"
        record.extract_chars = 120
        return InboundFileResult(
            record=record,
            context_block="[CurrentFiles]\n  extract_status: done\n  excerpt: OCR正文\n[/CurrentFiles]",
            user_notice="已收到并解析文件：report.pdf。",
            extract_text="OCR正文",
            pending=False,
        )


class LegacyOfficeRejectingFileService(FakeFileService):
    async def handle_bytes(self, **kwargs: Any) -> InboundFileResult:
        del kwargs
        raise FileStoreError(
            ".doc",
            user_notice=LEGACY_DOC_NOTICE,
        )


class BlockingFileService(FakeFileService):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    async def handle_bytes(self, **kwargs: Any) -> InboundFileResult:
        self.started.set()
        await self.release.wait()
        return await super().handle_bytes(**kwargs)


def _channel(userid_resolver: FakeUseridResolver | None = None) -> WeComChannel:
    return WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            callback_path="/callback/{botid}",
            initial_wait_seconds=0.05,
            userid_resolve_mode="",
        ),
        userid_resolver=userid_resolver,
    )


def test_queue_backpressure_defaults() -> None:
    assert WeComSettings.model_fields["initial_wait_seconds"].default == 0.5
    assert WeComSettings.model_fields["session_queue_maxsize"].default == 3
    assert WeComSettings.model_fields["queue_wait_timeout_seconds"].default == 240.0


def test_file_message_downloads_media_and_injects_file_context() -> None:
    received: list[ChannelMessage] = []
    media_client = FakeMediaClient()
    file_service = FakeFileService()
    sender = FakeResponseUrlSender()
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            callback_path="/callback/{botid}",
            initial_wait_seconds=0.05,
            userid_resolve_mode="",
        ),
        media_client=media_client,
        file_service=file_service,
        response_url_sender=sender,
    )

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="文件处理完成",
            )
        )

    async def scenario() -> dict[str, Any]:
        channel.bind_receiver(on_receive)
        plain = await channel._handle_plain_message({
            "msgid": "file-msg-1",
            "msgtype": "file",
            "from": {"userid": "chenkang2"},
            "responseurl": (
                "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?"
                "response_code=immediate-file"
            ),
            "file": {
                "url": "https://ww-aibot-img.example.com/report.txt?sign=secret",
                "filesize": 10,
            },
        })
        await asyncio.gather(*list(channel._dispatch_tasks))
        return json.loads(plain or "{}")

    payload = asyncio.run(scenario())

    assert payload["stream"]["content"] == "已收到，正在处理..."
    assert payload["stream"]["finish"] is True
    assert media_client.calls == ["https://ww-aibot-img.example.com/report.txt?sign=secret"]
    assert file_service.calls[0]["filename"] == "document_20260710_000000_000000.txt"
    assert received[0].context["files"]["current_files_context"].startswith("[CurrentFiles]")
    assert received[0].context["wecom"]["raw"]["file"]["has_url"] is True
    assert "url" not in received[0].context["wecom"]["raw"]["file"]
    assert "已收到并解析文件" in received[0].content
    assert sender.calls == [
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=immediate-file",
            "文件处理完成",
        )
    ]


def test_media_io_starts_only_after_first_callback_and_reserves_session_order() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        received: list[str] = []
        channel = WeComChannel(
            on_receive=None,
            settings=WeComSettings(
                enabled=False,
                encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
                initial_wait_seconds=0.01,
                userid_resolve_mode="",
            ),
            media_client=FakeMediaClient(),
            file_service=BlockingFileService(started, release),
            response_url_sender=FakeResponseUrlSender(),
        )

        async def on_receive(message: ChannelMessage) -> None:
            received.append(message.content)
            await channel.send(ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="done",
            ))

        channel.bind_receiver(on_receive)
        first = await channel._handle_plain_message({
            "msgid": "ordered-file",
            "msgtype": "file",
            "from": {"userid": "employee-1"},
            "responseurl": "https://qyapi.weixin.qq.com/file-response",
            "file": {"url": "https://ww-aibot-img.example.com/file?sign=secret"},
        })
        assert json.loads(first or "{}")["stream"]["finish"] is True
        assert started.is_set() is False

        await started.wait()
        second = await channel._handle_plain_message({
            "msgid": "ordered-text",
            "msgtype": "text",
            "from": {"userid": "employee-1"},
            "responseurl": "https://qyapi.weixin.qq.com/text-response",
            "text": {"content": "第二条消息"},
        })
        assert json.loads(second or "{}")["stream"]["finish"] is True
        assert received == []

        release.set()
        await asyncio.gather(*list(channel._dispatch_tasks))
        assert "已收到并解析文件" in received[0]
        assert received[1] == "第二条消息"

    asyncio.run(scenario())


def test_image_message_routes_with_its_original_msgtype(monkeypatch) -> None:
    channel = _channel()
    routed_msgtypes: list[str] = []

    async def handle_media(data: dict[str, Any], *, fallback_content: str = "") -> str:
        del fallback_content
        routed_msgtypes.append(str(data.get("msgtype")))
        return "image-routed"

    monkeypatch.setattr(channel, "_handle_media_message", handle_media)

    result = asyncio.run(
        channel._handle_plain_message({
            "msgtype": "image",
            "image": {"url": "https://ww-aibot-img.example.com/opaque?sign=secret"},
        })
    )

    assert result == "image-routed"
    assert routed_msgtypes == ["image"]


def test_legacy_office_file_returns_conversion_notice() -> None:
    received: list[ChannelMessage] = []
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            callback_path="/callback/{botid}",
            initial_wait_seconds=0.05,
            userid_resolve_mode="",
        ),
        media_client=FakeMediaClient(),
        file_service=LegacyOfficeRejectingFileService(),
    )

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content=message.content,
            )
        )

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        channel.bind_receiver(on_receive)
        plain = await channel._handle_plain_message({
            "msgid": "legacy-doc-1",
            "msgtype": "file",
            "from": {"userid": "chenkang2"},
            "file": {
                "url": "https://ww-aibot-img.example.com/legacy.doc?sign=secret",
                "filename": "legacy.doc",
                "mime_type": "application/msword",
            },
        })
        initial = json.loads(plain or "{}")
        await asyncio.gather(*list(channel._dispatch_tasks))
        final = json.loads(await channel._handle_stream_poll({"stream": {"id": initial["stream"]["id"]}}))
        return initial, final

    initial, payload = asyncio.run(scenario())

    assert received[0].content == LEGACY_DOC_NOTICE
    assert initial["stream"]["finish"] is False
    assert payload["stream"]["content"] == LEGACY_DOC_NOTICE


def test_pending_file_waits_for_extract_before_dispatching_model() -> None:
    received: list[ChannelMessage] = []
    file_service = PendingThenDoneFileService()
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            callback_path="/callback/{botid}",
            initial_wait_seconds=0.05,
            userid_resolve_mode="",
        ),
        media_client=FakeMediaClient(),
        file_service=file_service,
    )

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="PDF核心内容",
            )
        )

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        channel.bind_receiver(on_receive)
        plain = await channel._handle_plain_message({
            "msgid": "pending-pdf-1",
            "msgtype": "file",
            "from": {"userid": "chenkang2"},
            "file": {
                "url": "https://ww-aibot-img.example.com/report.pdf?sign=secret",
                "filename": "report.pdf",
                "mime_type": "application/pdf",
            },
        })
        initial = json.loads(plain or "{}")
        await asyncio.gather(*list(channel._dispatch_tasks))
        final = json.loads(await channel._handle_stream_poll({"stream": {"id": initial["stream"]["id"]}}))
        return initial, final

    initial, payload = asyncio.run(scenario())

    assert initial["stream"]["finish"] is False
    assert payload["stream"]["finish"] is True
    assert payload["stream"]["content"] == "PDF核心内容"
    assert file_service.poll_calls == 1
    assert len(received) == 1
    assert "已收到并解析文件" in received[0].content
    assert "extract_status: done" in received[0].context["files"]["current_files_context"]


def test_pending_file_reserves_response_url_for_async_completion() -> None:
    sender = FakeResponseUrlSender()
    file_service = PendingThenDoneFileService()
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            callback_path="/callback/{botid}",
            initial_wait_seconds=0.01,
            userid_resolve_mode="",
        ),
        media_client=FakeMediaClient(),
        file_service=file_service,
        response_url_sender=sender,
    )

    async def on_receive(message: ChannelMessage) -> None:
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="PDF异步解析完成",
            )
        )

    async def scenario() -> dict[str, Any]:
        channel.bind_receiver(on_receive)
        plain = await channel._handle_plain_message({
            "msgid": "pending-pdf-response-url",
            "msgtype": "file",
            "from": {"userid": "chenkang2"},
            "responseurl": (
                "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?"
                "response_code=pending-file"
            ),
            "file": {
                "url": "https://ww-aibot-img.example.com/report.pdf?sign=secret",
                "filename": "report.pdf",
                "mime_type": "application/pdf",
            },
        })
        await asyncio.gather(*list(channel._dispatch_tasks))
        return json.loads(plain or "{}")

    payload = asyncio.run(scenario())

    assert payload["stream"]["content"] == "已收到，正在处理..."
    assert payload["stream"]["finish"] is True
    assert sender.calls == [
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=pending-file",
            "PDF异步解析完成",
        )
    ]


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
        channel._handle_plain_message({
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "text": {"content": "帮我查一下制度"},
        })
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
        channel._handle_plain_message({
            "msgtype": "text",
            "from": {"userid": "encrypted-open-userid"},
            "text": {"content": "你好"},
        })
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
    assert received[0].context["wecom"]["address"]["transport"] == "aibot_callback"
    assert received[0].context["wecom"]["address"]["sender_userid"] == "encrypted-open-userid"
    assert received[0].context["wecom"]["address"]["plaintext_userid"] == "zhuchunlin"
    assert received[0].context["wecom"]["address"]["chat_id"] == "zhuchunlin"


def test_group_messages_are_isolated_by_bot_and_chat_after_userid_resolution() -> None:
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

    async def scenario() -> None:
        channel.bind_receiver(on_receive)
        for msgid, botid, chatid in (
            ("group-1", "bot-1", "chat-alpha"),
            ("group-2", "bot-1", "chat-beta"),
            ("group-3", "bot-2", "chat-alpha"),
        ):
            await channel._handle_plain_message({
                "msgid": msgid,
                "aibotid": botid,
                "chatid": chatid,
                "chattype": "group",
                "msgtype": "text",
                "from": {"userid": "encrypted-open-userid"},
                "text": {"content": "请帮我处理"},
            })

    asyncio.run(scenario())

    assert [message.session_id for message in received] == [
        "wecom:bot-1:group:chat-alpha",
        "wecom:bot-1:group:chat-beta",
        "wecom:bot-2:group:chat-alpha",
    ]
    assert [message.chat_id for message in received] == ["chat-alpha", "chat-beta", "chat-alpha"]
    assert [message.context["userid"] for message in received] == [
        "zhuchunlin",
        "zhuchunlin",
        "zhuchunlin",
    ]
    assert [message.context["wecom"]["chat_type"] for message in received] == [
        "group",
        "group",
        "group",
    ]
    assert [message.context["wecom"]["address"]["bot_or_agent_id"] for message in received] == [
        "bot-1",
        "bot-1",
        "bot-2",
    ]
    assert all(
        message.context["wecom"]["address"]["plaintext_userid"] == "zhuchunlin"
        for message in received
    )


def test_text_message_includes_quoted_text_without_response_capability() -> None:
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
        channel._handle_plain_message({
            "msgid": "quoted-1",
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "text": {"content": "请按这条继续"},
            "quote": {
                "msgtype": "text",
                "responseurl": "https://example.invalid/secret",
                "text": {"content": "原始需求内容"},
            },
        })
    )

    assert received[0].content == "请按这条继续\n\n引用消息（文本）：\n原始需求内容"
    assert received[0].context["wecom"]["raw"]["quote"] == {
        "msgtype": "text",
        "text": {"content": "原始需求内容"},
    }
    assert "responseurl" not in received[0].context["wecom"]["raw"]["quote"]


def test_text_message_returns_placeholder_before_slow_receive_completes() -> None:
    channel = _channel()

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        proceed = asyncio.Event()

        async def on_receive(message: ChannelMessage) -> None:
            await proceed.wait()
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content="慢任务处理完成",
                )
            )

        channel.bind_receiver(on_receive)

        first_plain = await channel._handle_plain_message({
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "text": {"content": "确认"},
        })
        first_payload = json.loads(first_plain or "{}")
        proceed.set()

        for _ in range(20):
            final_plain = await channel._handle_plain_message({
                "msgtype": "stream",
                "stream": {"id": first_payload["stream"]["id"]},
            })
            final_payload = json.loads(final_plain or "{}")
            if final_payload["stream"]["finish"]:
                return first_payload, final_payload
            await asyncio.sleep(0.01)
        return first_payload, final_payload

    first_payload, final_payload = asyncio.run(scenario())

    assert first_payload["stream"]["content"] == "已收到，正在处理..."
    assert first_payload["stream"]["finish"] is False
    assert final_payload["stream"]["content"] == "慢任务处理完成"
    assert final_payload["stream"]["finish"] is True


def test_ai_bot_callback_finishes_ack_then_delivers_final_once_via_response_url() -> None:
    sender = FakeResponseUrlSender()
    release_resolver = threading.Event()
    resolver_started = threading.Event()
    received: list[ChannelMessage] = []

    class BlockingResolver:
        def resolve(self, open_userid: str) -> str:
            assert open_userid == "encrypted-open-userid"
            resolver_started.set()
            release_resolver.wait(timeout=1.0)
            return "zhuchunlin"

    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            initial_wait_seconds=0.01,
            turn_timeout_seconds=1.0,
        ),
        userid_resolver=BlockingResolver(),
        response_url_sender=sender,
    )

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        callback = {
            "msgid": "response-url-turn",
            "msgtype": "text",
            "from": {"userid": "encrypted-open-userid"},
            "responseurl": "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=turn",
            "text": {"content": "你好"},
        }

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
        response = await asyncio.wait_for(
            channel._handle_plain_message(callback),
            timeout=0.2,
        )
        assert await asyncio.to_thread(resolver_started.wait, 0.2)
        assert received == []
        release_resolver.set()
        await asyncio.gather(*list(channel._dispatch_tasks))
        duplicate = await channel._handle_plain_message(callback)
        return json.loads(response or "{}"), json.loads(duplicate or "{}")

    payload, duplicate_payload = asyncio.run(scenario())

    assert payload["stream"]["content"] == "已收到，正在处理..."
    assert payload["stream"]["finish"] is True
    assert duplicate_payload["stream"]["id"] == payload["stream"]["id"]
    assert duplicate_payload["stream"]["content"] == "已收到，正在处理..."
    assert duplicate_payload["stream"]["finish"] is True
    assert received[0].session_id == "wecom:zhuchunlin"
    assert sender.calls == [
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=turn",
            "处理完成",
        )
    ]


def test_ai_bot_fast_turn_still_returns_ack_before_running_background_work() -> None:
    sender = FakeResponseUrlSender()
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            initial_wait_seconds=0.05,
            userid_resolve_mode="",
        ),
        response_url_sender=sender,
    )

    async def on_receive(message: ChannelMessage) -> None:
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="快速处理完成",
            )
        )

    async def scenario() -> dict[str, Any]:
        channel.bind_receiver(on_receive)
        plain = await channel._handle_plain_message({
            "msgid": "response-url-fast-turn",
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "responseurl": (
                "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?"
                "response_code=fast-turn"
            ),
            "text": {"content": "你好"},
        })
        await asyncio.gather(*list(channel._dispatch_tasks))
        return json.loads(plain or "{}")

    payload = asyncio.run(scenario())

    assert payload["stream"]["content"] == "已收到，正在处理..."
    assert payload["stream"]["finish"] is True
    assert sender.calls == [
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=fast-turn",
            "快速处理完成",
        )
    ]


def test_ai_bot_concurrent_duplicate_replays_ack_and_delivers_final_once() -> None:
    sender = FakeResponseUrlSender()
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            initial_wait_seconds=0.01,
            userid_resolve_mode="",
        ),
        response_url_sender=sender,
    )

    async def scenario() -> list[dict[str, Any]]:
        release = asyncio.Event()
        received = 0

        async def on_receive(message: ChannelMessage) -> None:
            nonlocal received
            received += 1
            await release.wait()
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content="去重后处理完成",
                )
            )

        callback = {
            "msgid": "response-url-concurrent-duplicate",
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "responseurl": (
                "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?"
                "response_code=concurrent-duplicate"
            ),
            "text": {"content": "你好"},
        }
        channel.bind_receiver(on_receive)
        replies = await asyncio.gather(
            channel._handle_plain_message(callback),
            channel._handle_plain_message(callback),
        )
        release.set()
        await asyncio.gather(*list(channel._dispatch_tasks))
        assert received == 1
        return [json.loads(reply or "{}") for reply in replies]

    payloads = asyncio.run(scenario())

    assert payloads[0] == payloads[1]
    assert payloads[0]["stream"]["content"] == "已收到，正在处理..."
    assert payloads[0]["stream"]["finish"] is True
    assert sender.calls == [
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?"
            "response_code=concurrent-duplicate",
            "去重后处理完成",
        )
    ]


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
        channel._handle_plain_message({
            "msgid": "m1",
            "aibotid": "bot-1",
            "chattype": "single",
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "responseurl": "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=secret",
            "text": {"content": "你好"},
        })
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


def test_response_url_probe_consumes_url_once_without_dispatching_to_agent() -> None:
    sender = FakeResponseUrlSender()
    received: list[ChannelMessage] = []
    channel = WeComChannel(
        on_receive=received.append,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            userid_resolve_mode="",
            response_url_probe_trigger="probe-challenge",
            response_url_probe_delay_seconds=0.01,
        ),
        response_url_sender=sender,
    )

    async def scenario() -> dict[str, Any]:
        response = await channel._handle_plain_message({
            "msgid": "probe-message",
            "msgtype": "text",
            "from": {"userid": "probe-user"},
            "responseurl": "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=sensitive",
            "text": {"content": "probe-challenge"},
        })
        await asyncio.gather(*list(channel._dispatch_tasks))
        return json.loads(response or "{}")

    payload = asyncio.run(scenario())

    assert payload["stream"]["finish"] is True
    assert "探针已启动" in payload["stream"]["content"]
    assert received == []
    assert sender.calls == [
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=sensitive",
            "AgentSeek v0.1.0 M0：短连接 response_url 延迟回复探针成功。",
        )
    ]


def test_response_url_probe_fails_closed_when_callback_has_no_url() -> None:
    sender = FakeResponseUrlSender()
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            userid_resolve_mode="",
            response_url_probe_trigger="probe-challenge",
        ),
        response_url_sender=sender,
    )

    response = asyncio.run(
        channel._handle_plain_message({
            "msgid": "probe-message",
            "msgtype": "text",
            "from": {"userid": "probe-user"},
            "text": {"content": "probe-challenge"},
        })
    )
    payload = json.loads(response or "{}")

    assert payload["stream"]["finish"] is True
    assert "未包含 response_url" in payload["stream"]["content"]
    assert sender.calls == []


def test_template_card_probe_consumes_url_once_without_dispatching_to_agent() -> None:
    sender = FakeResponseUrlSender()
    received: list[ChannelMessage] = []
    channel = WeComChannel(
        on_receive=received.append,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            userid_resolve_mode="",
            response_url_template_card_probe_trigger="probe-template-card",
            response_url_probe_delay_seconds=0.01,
        ),
        response_url_sender=sender,
    )

    async def scenario() -> dict[str, Any]:
        response = await channel._handle_plain_message({
            "msgid": "probe-card-message",
            "msgtype": "text",
            "from": {"userid": "probe-user"},
            "responseurl": "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=sensitive",
            "text": {"content": "probe-template-card"},
        })
        await asyncio.gather(*list(channel._dispatch_tasks))
        return json.loads(response or "{}")

    payload = asyncio.run(scenario())

    assert payload["stream"]["finish"] is True
    assert "模板卡片探针已启动" in payload["stream"]["content"]
    assert received == []
    assert len(sender.card_calls) == 1
    response_url, card = sender.card_calls[0]
    assert response_url.endswith("response_code=sensitive")
    assert card["card_type"] == "text_notice"
    assert card["card_action"] == {
        "type": 1,
        "url": "https://developer.work.weixin.qq.com/document/path/101138",
    }


def test_template_card_probe_fails_closed_when_callback_has_no_url() -> None:
    sender = FakeResponseUrlSender()
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            userid_resolve_mode="",
            response_url_template_card_probe_trigger="probe-template-card",
        ),
        response_url_sender=sender,
    )

    response = asyncio.run(
        channel._handle_plain_message({
            "msgid": "probe-card-message",
            "msgtype": "text",
            "from": {"userid": "probe-user"},
            "text": {"content": "probe-template-card"},
        })
    )
    payload = json.loads(response or "{}")

    assert payload["stream"]["finish"] is True
    assert "未包含 response_url" in payload["stream"]["content"]
    assert sender.card_calls == []


def test_template_card_event_probe_sends_interactive_card() -> None:
    sender = FakeResponseUrlSender()
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            template_card_event_probe_trigger="probe-card-event",
        ),
        response_url_sender=sender,
    )

    async def scenario() -> dict[str, Any]:
        response = await channel._handle_plain_message({
            "msgid": "probe-card-event-message",
            "msgtype": "text",
            "from": {"userid": "probe-user"},
            "response_url": "https://qyapi.weixin.qq.com/card-event-capability",
            "text": {"content": "probe-card-event"},
        })
        await asyncio.gather(*list(channel._dispatch_tasks))
        return json.loads(response or "{}")

    payload = asyncio.run(scenario())

    assert payload["stream"]["finish"] is True
    assert "点击随后卡片" in payload["stream"]["content"]
    assert len(sender.card_calls) == 1
    _, card = sender.card_calls[0]
    assert card["card_type"] == "button_interaction"
    assert card["button_list"] == [{"text": "确认交互", "style": 1, "key": "M04_CONFIRM"}]
    assert str(card["task_id"]).startswith("agentseek_m04_")


def test_registered_template_card_intent_sends_once_then_commits() -> None:
    sender = FakeResponseUrlSender()
    committed: list[str] = []
    failed: list[str] = []
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(enabled=False),
        response_url_sender=sender,
    )
    marker = register_template_card_intent(TemplateCardIntent(
        template_card={
            "card_type": "text_notice",
            "main_title": {"title": "报告已交付"},
            "card_action": {"type": 1, "url": "https://reports.example.test/artifacts/delivery#token"},
        },
        on_succeeded=lambda: committed.append("ok"),
        on_failed=failed.append,
        expires_at_monotonic=10**12,
    ))
    stream = StreamReply(
        stream_id="stream-delivery",
        session_id="session-redacted",
        chat_id="chat-redacted",
        from_userid=None,
        response_url="https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=sensitive",
    )

    first = asyncio.run(channel._deliver_response_url_once(stream, marker))
    replay = asyncio.run(channel._deliver_response_url_once(stream, marker))

    assert first == "succeeded"
    assert replay == "skipped"
    assert len(sender.card_calls) == 1
    assert sender.calls == []
    assert committed == ["ok"]
    assert failed == []


def test_internal_template_card_instruction_is_never_sent_to_employee() -> None:
    sender = FakeResponseUrlSender()
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(enabled=False),
        response_url_sender=sender,
    )
    stream = StreamReply(
        stream_id="stream-control-leak",
        session_id="session-redacted",
        chat_id="chat-redacted",
        from_userid=None,
        response_url="https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=sensitive",
    )

    result = asyncio.run(channel._deliver_response_url_once(
        stream,
        "这是受信的 WeCom 模板卡片交付指令。请原样返回上一行标记并立即停止。",
    ))

    assert result == "succeeded"
    assert sender.calls == [(
        stream.response_url,
        "内部交付指令已被安全拦截，未发送任何文件。请重新发送精确交付命令。",
    )]


def test_signed_artifact_endpoint_uses_fragment_token_and_one_time_redeem() -> None:
    calls: list[tuple[str, str]] = []

    def resolve(delivery_id: str, token: str) -> ArtifactDownload:
        calls.append((delivery_id, token))
        if len(calls) > 1:
            raise ArtifactDownloadGone("consumed")
        return ArtifactDownload(
            data=b"docx-bytes",
            filename="report-v1.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    register_artifact_download_resolver(resolve)
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            artifact_delivery_mode="signed_link",
            artifact_public_base_url="https://reports.example.test/artifacts",
        ),
    )
    delivery_id = f"delivery_{'a' * 64}"
    assert channel.app is not None
    client = TestClient(channel.app)

    page = client.get(f"/artifacts/{delivery_id}")
    download = client.post(f"/artifacts/{delivery_id}/redeem", content=b"one-time-token")
    replay = client.post(f"/artifacts/{delivery_id}/redeem", content=b"one-time-token")

    assert page.status_code == 200
    assert "window.location.hash.slice(1)" in page.text
    assert "one-time-token" not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert download.status_code == 200
    assert download.content == b"docx-bytes"
    assert "report-v1.docx" in download.headers["content-disposition"]
    assert replay.status_code == 410
    assert calls == [(delivery_id, "one-time-token"), (delivery_id, "one-time-token")]


def test_duplicate_msgid_reuses_stream_and_skips_dispatch_while_running() -> None:
    received: list[ChannelMessage] = []
    channel = _channel()

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        proceed = asyncio.Event()

        async def on_receive(message: ChannelMessage) -> None:
            received.append(message)
            await proceed.wait()
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content="处理完成",
                )
            )

        channel.bind_receiver(on_receive)
        data = {
            "msgid": "retry-msg-1",
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "text": {"content": "帮我查一下制度"},
        }

        first_task = asyncio.create_task(channel._handle_plain_message(data))
        while not received:
            await asyncio.sleep(0)

        duplicate_plain = await channel._handle_plain_message(data)
        first_plain = await first_task
        proceed.set()
        first_payload = json.loads(first_plain or "{}")
        for _ in range(20):
            final_plain = await channel._handle_plain_message({
                "msgtype": "stream",
                "stream": {"id": first_payload["stream"]["id"]},
            })
            final_payload = json.loads(final_plain or "{}")
            if final_payload["stream"]["finish"]:
                break
            await asyncio.sleep(0.01)
        return first_payload, json.loads(duplicate_plain or "{}"), final_payload

    first_payload, duplicate_payload, final_payload = asyncio.run(scenario())

    assert len(received) == 1
    assert duplicate_payload["stream"]["id"] == first_payload["stream"]["id"]
    assert duplicate_payload["stream"]["finish"] is False
    assert first_payload["stream"]["finish"] is False
    assert final_payload["stream"]["finish"] is True
    assert final_payload["stream"]["content"] == "处理完成"


def test_duplicate_msgid_can_reprocess_after_stream_cache_ttl() -> None:
    received: list[ChannelMessage] = []
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            callback_path="/callback/{botid}",
            initial_wait_seconds=0.05,
            cache_ttl_seconds=0,
            userid_resolve_mode="",
        ),
        userid_resolver=None,
    )

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)
        await channel.send(
            ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content=f"处理完成{len(received)}",
            )
        )

    channel.bind_receiver(on_receive)
    data = {
        "msgid": "retry-msg-2",
        "msgtype": "text",
        "from": {"userid": "chenkang2"},
        "text": {"content": "你好"},
    }

    first_plain = asyncio.run(channel._handle_plain_message(data))
    second_plain = asyncio.run(channel._handle_plain_message(data))
    first_payload = json.loads(first_plain or "{}")
    second_payload = json.loads(second_plain or "{}")

    assert len(received) == 2
    assert first_payload["stream"]["id"] != second_payload["stream"]["id"]
    assert second_payload["stream"]["content"] == "处理完成2"


def test_distinct_messages_in_same_session_are_processed_serially() -> None:
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            initial_wait_seconds=0.005,
            turn_timeout_seconds=1.0,
            session_queue_maxsize=16,
            userid_resolve_mode="",
        ),
    )

    async def scenario() -> tuple[list[str], int, list[dict[str, Any]]]:
        received: list[str] = []
        active = 0
        max_active = 0

        async def on_receive(message: ChannelMessage) -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            received.append(message.content)
            await asyncio.sleep(0.01)
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content=f"完成:{message.content}",
                )
            )
            active -= 1

        channel.bind_receiver(on_receive)
        replies = await asyncio.gather(*(
            channel._handle_plain_message({
                "msgid": f"burst-{index}",
                "msgtype": "text",
                "from": {"userid": "chenkang2"},
                "text": {"content": f"消息{index}"},
            })
            for index in range(8)
        ))
        await asyncio.gather(*list(channel._dispatch_tasks))
        payloads: list[dict[str, Any]] = []
        for reply in replies:
            initial = json.loads(reply or "{}")
            final = await channel._handle_plain_message({
                "msgtype": "stream",
                "stream": {"id": initial["stream"]["id"]},
            })
            payloads.append(json.loads(final or "{}"))
        return received, max_active, payloads

    received, max_active, payloads = asyncio.run(scenario())

    assert received == [f"消息{index}" for index in range(8)]
    assert max_active == 1
    assert [payload["stream"]["content"] for payload in payloads] == [
        f"完成:消息{index}" for index in range(8)
    ]
    assert all(payload["stream"]["finish"] for payload in payloads)


def test_distinct_sessions_remain_concurrent() -> None:
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            initial_wait_seconds=0.005,
            turn_timeout_seconds=1.0,
            userid_resolve_mode="",
        ),
    )

    async def scenario() -> int:
        both_entered = asyncio.Event()
        active = 0
        max_active = 0

        async def on_receive(message: ChannelMessage) -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=0.5)
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content="处理完成",
                )
            )
            active -= 1

        channel.bind_receiver(on_receive)
        await asyncio.gather(*(
            channel._handle_plain_message({
                "msgid": f"parallel-{userid}",
                "msgtype": "text",
                "from": {"userid": userid},
                "text": {"content": "你好"},
            })
            for userid in ("employee-a", "employee-b")
        ))
        await asyncio.gather(*list(channel._dispatch_tasks))
        return max_active

    assert asyncio.run(scenario()) == 2


def test_session_queue_reports_positions_and_rejects_above_pending_limit() -> None:
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            initial_wait_seconds=0.005,
            turn_timeout_seconds=1.0,
            session_queue_maxsize=3,
            userid_resolve_mode="",
        ),
    )

    async def scenario() -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
        release = asyncio.Event()
        received: list[str] = []

        async def on_receive(message: ChannelMessage) -> None:
            received.append(message.content)
            if message.content == "消息0":
                await release.wait()
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content=f"完成:{message.content}",
                )
            )

        channel.bind_receiver(on_receive)
        replies = await asyncio.gather(*(
            channel._handle_plain_message({
                "msgid": f"limited-{index}",
                "msgtype": "text",
                "from": {"userid": "chenkang2"},
                "text": {"content": f"消息{index}"},
            })
            for index in range(5)
        ))
        initial_payloads = [json.loads(reply or "{}") for reply in replies]
        status_reply = await channel._handle_plain_message({
            "msgid": "queue-status-1",
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "text": {"content": "查看消息队列"},
        })
        status_payload = json.loads(status_reply or "{}")
        release.set()
        await asyncio.gather(*list(channel._dispatch_tasks))
        return received, initial_payloads, status_payload

    received, payloads, status = asyncio.run(scenario())

    assert received == [f"消息{index}" for index in range(4)]
    assert "等待队列第 1 位" in payloads[1]["stream"]["content"]
    assert "等待队列第 2 位" in payloads[2]["stream"]["content"]
    assert "等待队列第 3 位" in payloads[3]["stream"]["content"]
    assert payloads[4]["stream"]["finish"] is True
    assert "本条消息未进入队列" in payloads[4]["stream"]["content"]
    assert status["stream"]["finish"] is True
    assert "正在处理：1 条" in status["stream"]["content"]
    assert "等待处理：3 条" in status["stream"]["content"]


def test_ai_bot_queue_feedback_uses_callback_and_final_replies_use_response_url() -> None:
    sender = FakeResponseUrlSender()
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            initial_wait_seconds=0.01,
            turn_timeout_seconds=1.0,
            session_queue_maxsize=3,
            userid_resolve_mode="",
        ),
        response_url_sender=sender,
    )

    async def scenario() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        release = asyncio.Event()
        received: list[str] = []

        async def on_receive(message: ChannelMessage) -> None:
            received.append(message.content)
            if message.content == "消息0":
                await release.wait()
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content=f"完成:{message.content}",
                )
            )

        channel.bind_receiver(on_receive)
        replies = await asyncio.gather(*(
            channel._handle_plain_message({
                "msgid": f"response-queue-{index}",
                "msgtype": "text",
                "from": {"userid": "chenkang2"},
                "responseurl": (
                    "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?"
                    f"response_code=queue-{index}"
                ),
                "text": {"content": f"消息{index}"},
            })
            for index in range(5)
        ))
        payloads = [json.loads(reply or "{}") for reply in replies]
        duplicate_rejection = await channel._handle_plain_message({
            "msgid": "response-queue-4",
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "responseurl": (
                "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?"
                "response_code=queue-4"
            ),
            "text": {"content": "消息4"},
        })
        payloads.append(json.loads(duplicate_rejection or "{}"))
        release.set()
        await asyncio.gather(*list(channel._dispatch_tasks))
        final_payloads = [
            json.loads(await channel._handle_plain_message({
                "msgtype": "stream",
                "stream": {"id": payloads[index]["stream"]["id"]},
            }) or "{}")
            for index in range(4)
        ]
        return payloads, final_payloads, received

    payloads, final_payloads, received = asyncio.run(scenario())

    assert all(payload["stream"]["finish"] is True for payload in payloads[:4])
    assert payloads[0]["stream"]["content"] == "已收到，正在处理..."
    assert "等待队列第 1 位" in payloads[1]["stream"]["content"]
    assert "等待队列第 2 位" in payloads[2]["stream"]["content"]
    assert "等待队列第 3 位" in payloads[3]["stream"]["content"]
    assert "另有 3 条等待处理" in payloads[4]["stream"]["content"]
    assert payloads[4]["stream"]["finish"] is True
    assert payloads[5] == payloads[4]
    assert received == [f"消息{index}" for index in range(4)]
    assert [payload["stream"]["content"] for payload in final_payloads] == [
        f"完成:消息{index}" for index in range(4)
    ]
    assert all(payload["stream"]["finish"] is True for payload in final_payloads)
    assert sender.calls == [
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=queue-0",
            "完成:消息0",
        ),
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=queue-1",
            "完成:消息1",
        ),
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=queue-2",
            "完成:消息2",
        ),
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=queue-3",
            "完成:消息3",
        ),
    ]


def test_pending_message_expires_without_entering_agent() -> None:
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            initial_wait_seconds=0.005,
            turn_timeout_seconds=1.0,
            session_queue_maxsize=3,
            queue_wait_timeout_seconds=0.05,
            userid_resolve_mode="",
        ),
    )

    async def scenario() -> tuple[list[str], dict[str, Any], dict[str, Any]]:
        release = asyncio.Event()
        received: list[str] = []

        async def on_receive(message: ChannelMessage) -> None:
            received.append(message.content)
            await release.wait()
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content="第一条完成",
                )
            )

        channel.bind_receiver(on_receive)
        first_reply = await channel._handle_plain_message({
            "msgid": "queue-ttl-1",
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "text": {"content": "第一条"},
        })
        second_reply = await channel._handle_plain_message({
            "msgid": "queue-ttl-2",
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "text": {"content": "第二条"},
        })
        await asyncio.sleep(0.08)
        second_stream_id = json.loads(second_reply or "{}")["stream"]["id"]
        expired_reply = await channel._handle_plain_message({
            "msgtype": "stream",
            "stream": {"id": second_stream_id},
        })
        status_reply = await channel._handle_plain_message({
            "msgid": "queue-status-2",
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "text": {"content": "查看排队状态"},
        })
        release.set()
        await asyncio.gather(*list(channel._dispatch_tasks))
        del first_reply
        return received, json.loads(expired_reply or "{}"), json.loads(status_reply or "{}")

    received, expired, status = asyncio.run(scenario())

    assert received == ["第一条"]
    assert expired["stream"]["finish"] is True
    assert expired["stream"]["content"] == "等待处理时间过长，本条消息已取消，请稍后重新发送。"
    assert "正在处理：1 条" in status["stream"]["content"]
    assert "等待处理：0 条" in status["stream"]["content"]


def test_ai_bot_pending_timeout_delivers_once_via_response_url() -> None:
    sender = FakeResponseUrlSender()
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            initial_wait_seconds=0.005,
            turn_timeout_seconds=1.0,
            session_queue_maxsize=3,
            queue_wait_timeout_seconds=0.05,
            userid_resolve_mode="",
        ),
        response_url_sender=sender,
    )

    async def scenario() -> tuple[list[str], dict[str, Any]]:
        release = asyncio.Event()
        received: list[str] = []

        async def on_receive(message: ChannelMessage) -> None:
            received.append(message.content)
            await release.wait()
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content="第一条完成",
                )
            )

        channel.bind_receiver(on_receive)
        payloads: list[dict[str, Any]] = []
        for index in range(2):
            payload = json.loads(await channel._handle_plain_message({
                "msgid": f"response-ttl-{index}",
                "msgtype": "text",
                "from": {"userid": "chenkang2"},
                "responseurl": (
                    "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?"
                    f"response_code=ttl-{index}"
                ),
                "text": {"content": f"消息{index}"},
            }) or "{}")
            assert payload["stream"]["finish"] is True
            assert payload["stream"]["content"]
            payloads.append(payload)
        await asyncio.sleep(0.08)
        expired = json.loads(await channel._handle_plain_message({
            "msgtype": "stream",
            "stream": {"id": payloads[1]["stream"]["id"]},
        }) or "{}")
        release.set()
        await asyncio.gather(*list(channel._dispatch_tasks))
        return received, expired

    received, expired = asyncio.run(scenario())

    assert received == ["消息0"]
    assert expired["stream"]["finish"] is True
    assert expired["stream"]["content"] == "等待处理时间过长，本条消息已取消，请稍后重新发送。"
    assert sender.calls == [
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=ttl-1",
            "等待处理时间过长，本条消息已取消，请稍后重新发送。",
        ),
        (
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=ttl-0",
            "第一条完成",
        ),
    ]


def test_queue_status_command_is_immediate_when_session_is_idle() -> None:
    received: list[ChannelMessage] = []
    channel = _channel()
    channel.bind_receiver(received.append)

    reply = asyncio.run(
        channel._handle_plain_message({
            "msgid": "queue-status-idle",
            "msgtype": "text",
            "from": {"userid": "chenkang2"},
            "text": {"content": "查看消息队列"},
        })
    )
    payload = json.loads(reply or "{}")

    assert payload["stream"]["finish"] is True
    assert payload["stream"]["content"] == "当前没有正在处理或等待处理的消息。"
    assert received == []


def test_timed_out_turn_releases_same_session_queue() -> None:
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            initial_wait_seconds=0.005,
            turn_timeout_seconds=0.05,
            userid_resolve_mode="",
        ),
    )

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        calls = 0

        async def on_receive(message: ChannelMessage) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                await channel.send(
                    ChannelMessage(
                        session_id=message.session_id,
                        channel="wecom",
                        chat_id=message.chat_id,
                        content="第二条已处理",
                    )
                )

        channel.bind_receiver(on_receive)
        first, second = await asyncio.gather(
            channel._handle_plain_message({
                "msgid": "timeout-1",
                "msgtype": "text",
                "from": {"userid": "chenkang2"},
                "text": {"content": "第一条"},
            }),
            channel._handle_plain_message({
                "msgid": "timeout-2",
                "msgtype": "text",
                "from": {"userid": "chenkang2"},
                "text": {"content": "第二条"},
            }),
        )
        await asyncio.gather(*list(channel._dispatch_tasks))
        payloads = []
        for reply in (first, second):
            stream_id = json.loads(reply or "{}")["stream"]["id"]
            final = await channel._handle_plain_message({"msgtype": "stream", "stream": {"id": stream_id}})
            payloads.append(json.loads(final or "{}"))
        return payloads[0], payloads[1]

    first, second = asyncio.run(scenario())

    assert first["stream"]["content"] == "本次处理超时，请稍后重试。"
    assert first["stream"]["finish"] is True
    assert second["stream"]["content"] == "第二条已处理"
    assert second["stream"]["finish"] is True


def test_stop_cancels_server_task_after_graceful_shutdown_timeout() -> None:
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            shutdown_timeout_seconds=0.01,
            userid_resolve_mode="",
        ),
    )

    async def scenario() -> tuple[bool, bool]:
        async def hung_server() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(hung_server())
        await asyncio.sleep(0)
        server = SimpleNamespace(should_exit=False)
        transport = channel.transport
        assert isinstance(transport, AiBotCallbackTransport)
        transport._server = server  # ty: ignore[invalid-assignment]
        transport._server_task = task

        await channel.stop()

        return server.should_exit, task.cancelled()

    should_exit, cancelled = asyncio.run(scenario())

    assert should_exit is True
    assert cancelled is True


def test_stop_does_not_wait_forever_when_dispatch_task_suppresses_cancellation() -> None:
    channel = WeComChannel(
        on_receive=None,
        settings=WeComSettings(
            enabled=False,
            token="token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            shutdown_timeout_seconds=0.01,
            userid_resolve_mode="",
        ),
    )

    async def scenario() -> tuple[bool, bool]:
        first_cancellation_seen = asyncio.Event()
        release = asyncio.Event()

        async def stubborn_dispatch() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                first_cancellation_seen.set()
                await release.wait()

        task = asyncio.create_task(stubborn_dispatch())
        channel._dispatch_tasks.add(task)
        await asyncio.sleep(0)

        await channel.stop()
        returned_while_pending = not task.done()
        release.set()
        await task
        return first_cancellation_seen.is_set(), returned_while_pending

    assert asyncio.run(scenario()) == (True, True)


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
    message._agentseek_wecom_stream_id = stream.stream_id  # ty: ignore[unresolved-attribute]

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

    plain = asyncio.run(channel._handle_plain_message({"msgtype": "event", "event": {"eventtype": "enter_chat"}}))
    payload = json.loads(plain or "{}")

    assert payload["msgtype"] == "text"
    assert "企业数字员工" in payload["text"]["content"]


def test_template_card_event_is_deduplicated_and_dispatched_through_session_queue() -> None:
    async def scenario() -> None:
        received: list[ChannelMessage] = []
        sender = FakeResponseUrlSender()
        channel = WeComChannel(
            on_receive=None,
            settings=WeComSettings(enabled=False, initial_wait_seconds=0.01, userid_resolve_mode=""),
            response_url_sender=sender,
        )

        async def on_receive(message: ChannelMessage) -> None:
            received.append(message)
            await channel.send(ChannelMessage(
                session_id=message.session_id,
                channel="wecom",
                chat_id=message.chat_id,
                content="卡片操作已记录",
            ))

        channel.bind_receiver(on_receive)
        payload = {
            "msgid": "card-event-1",
            "msgtype": "event",
            "aibotid": "bot-1",
            "from": {"userid": "employee-1"},
            "chatid": "group-1",
            "chattype": "group",
            "response_url": "https://qyapi.weixin.qq.com/card-event-response",
            "event": {
                "eventtype": "template_card_event",
                "template_card_event": {
                    "card_type": "vote_interaction",
                    "event_key": "submit_vote",
                    "task_id": "task-1",
                    "selected_items": {
                        "selected_item": [{
                            "question_key": "risk_level",
                            "option_ids": {"option_id": ["medium", "high"]},
                        }],
                    },
                },
            },
        }
        first = await channel._handle_plain_message(payload)
        duplicate = await channel._handle_plain_message(payload)
        await asyncio.gather(*list(channel._dispatch_tasks))

        first_payload = json.loads(first or "{}")
        duplicate_payload = json.loads(duplicate or "{}")
        assert first_payload["stream"]["finish"] is True
        assert duplicate_payload["stream"]["id"] == first_payload["stream"]["id"]
        assert len(received) == 1
        assert "submit_vote" in received[0].content
        assert "medium、high" in received[0].content
        assert received[0].context["wecom"]["raw"]["event"]["template_card_event"] == {
            "card_type": "vote_interaction",
            "event_key": "submit_vote",
            "task_id": "task-1",
            "selected_items": [{"question_key": "risk_level", "option_ids": ["medium", "high"]}],
        }
        assert sender.calls == [("https://qyapi.weixin.qq.com/card-event-response", "卡片操作已记录")]

    asyncio.run(scenario())


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
    assert isinstance(channel.transport, AiBotCallbackTransport)
    assert channel.app is not None
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
    assert received[0].context["wecom"]["address"]["bot_or_agent_id"] == "bot-1"


def test_outbound_routes_to_oldest_unfinished_stream_per_session() -> None:
    """A reply must land on the stream of the turn that produced it.

    The framework rebuilds outbound messages without the inbound stream id, so
    routing cannot correlate by attribute. Turns are serialized per session, so
    replies complete in the same order their streams were created: the reply for
    the oldest running turn must reach the oldest still-open stream. Routing to
    the newest stream instead would write an earlier turn's reply onto a later
    turn's stream and lose the later reply.
    """
    channel = _channel()
    stream_a = asyncio.run(channel._create_stream(session_id="wecom:u1", chat_id="u1", from_userid="u1"))
    stream_b = asyncio.run(channel._create_stream(session_id="wecom:u1", chat_id="u1", from_userid="u1"))

    # Outbound carries no stream id, mirroring bub's render_outbound rebuild.
    reply_first = ChannelMessage(session_id="wecom:u1", channel="wecom", chat_id="u1", content="第一条回复")
    asyncio.run(channel.send(reply_first))

    assert stream_a.content == "第一条回复"
    assert stream_a.finish is True
    assert stream_b.content != "第一条回复"

    reply_second = ChannelMessage(session_id="wecom:u1", channel="wecom", chat_id="u1", content="第二条回复")
    asyncio.run(channel.send(reply_second))

    assert stream_b.content == "第二条回复"
    assert stream_b.finish is True


def test_outbound_with_stream_id_attr_routes_to_that_stream() -> None:
    """When the outbound carries the stream id, it routes to that exact stream
    regardless of creation order — the attribute path is authoritative."""
    channel = _channel()
    stream_a = asyncio.run(channel._create_stream(session_id="wecom:u1", chat_id="u1", from_userid="u1"))
    stream_b = asyncio.run(channel._create_stream(session_id="wecom:u1", chat_id="u1", from_userid="u1"))

    reply = ChannelMessage(session_id="wecom:u1", channel="wecom", chat_id="u1", content="定向回复")
    reply._agentseek_wecom_stream_id = stream_a.stream_id  # ty: ignore[unresolved-attribute]
    asyncio.run(channel.send(reply))

    assert stream_a.content == "定向回复"
    assert stream_b.content != "定向回复"
