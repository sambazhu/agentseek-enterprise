from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any

import httpx
import pytest
from agentseek_wecom.channel import WeComChannel
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.crypto import WeComJsonCrypto
from agentseek_wecom.durable import SqliteDurableMessageStore
from agentseek_wecom.media import WeComMediaClient
from agentseek_wecom.transports.application import (
    WeComAppApiError,
    WeComAppPartialDelivery,
    WeComAppTarget,
    WeComAppTransport,
    WeComAppVisibility,
    WeComAppVisibilityDenied,
)
from bub.channels.message import ChannelMessage
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from rich.console import Console
from rich.traceback import Traceback

AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
SENSITIVE_APP_SECRET = "sensitive-application-secret"  # noqa: S105 - deterministic fake test credential
SENSITIVE_ACCESS_TOKEN = "sensitive-access-token"  # noqa: S105 - deterministic fake test credential


class WaitConditionTimeout(AssertionError):
    pass


class SyntheticFailureMissing(AssertionError):
    pass


async def wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise WaitConditionTimeout


def app_settings(**overrides: Any) -> WeComSettings:
    values: dict[str, Any] = {
        "enabled": False,
        "corp_id": "corp-1",
        "app_transport_enabled": True,
        "app_agent_id": "1000005",
        "app_transport_secret": SecretStr("application-secret"),
        "app_callback_token": SecretStr("AppCallbackToken1"),
        "app_callback_encoding_aes_key": SecretStr(AES_KEY),
        "app_allowed_digital_employee_ids": "industry-report,finance-assistant",
        "app_default_digital_employee_id": "industry-report",
    }
    values.update(overrides)
    return WeComSettings(**values)


def test_application_settings_are_independent_from_bot_mode() -> None:
    callback = app_settings(transport_mode="callback")
    long_connection = app_settings(
        transport_mode="long_connection",
        long_connection_bot_id="bot-1",
        long_connection_secret=SecretStr("bot-secret"),
    )

    assert callback.app_transport_enabled is True
    assert long_connection.app_transport_enabled is True
    with pytest.raises(ValidationError, match="app_default_digital_employee_id"):
        app_settings(app_default_digital_employee_id="unregistered")


def test_application_target_rejects_broadcast_and_unsafe_ids() -> None:
    with pytest.raises(ValueError, match="@all"):
        WeComAppTarget(users=("@all",))
    with pytest.raises(ValueError, match="must not contain"):
        WeComAppTarget(users=("user-1|user-2",))
    with pytest.raises(ValueError, match="at least one"):
        WeComAppTarget()
    with pytest.raises(ValueError, match="positive integers"):
        WeComAppTarget(parties=("department-name",))


def test_application_token_visibility_and_send_are_cached() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "token-1", "expires_in": 7200})
        if request.url.path == "/cgi-bin/agent/get":
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "agentid": 1000005,
                    "close": 0,
                    "allow_userinfos": {"user": [{"userid": "user-1"}]},
                    "allow_partys": {"partyid": [2]},
                    "allow_tags": {"tagid": [3]},
                },
            )
        body = json.loads(request.content)
        assert body["touser"] == "user-1"
        if body["msgtype"] == "text":
            assert body["toparty"] == "2"
            assert body["totag"] == "3"
        assert body["enable_duplicate_check"] == 1
        assert body["agentid"] == 1000005
        return httpx.Response(200, json={"errcode": 0})

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = WeComAppTransport(settings=app_settings(), tenant_id="tenant-1", client=client)
        await transport.start()
        await transport.send(
            target=WeComAppTarget(users=("user-1",), parties=("2",), tags=("3",)),
            message_type="text",
            payload={"content": "通知"},
        )
        await transport.send(
            target=WeComAppTarget(users=("user-1",)),
            message_type="markdown",
            payload={"content": "**通知**"},
        )
        await client.aclose()

    asyncio.run(scenario())

    assert [request.url.path for request in requests].count("/cgi-bin/gettoken") == 1
    assert [request.url.path for request in requests].count("/cgi-bin/agent/get") == 1
    assert [request.url.path for request in requests].count("/cgi-bin/message/send") == 2
    assert "application-secret" not in " ".join(str(request.content) for request in requests)


def test_application_credentials_are_redacted_from_rich_http_failure() -> None:
    async def scenario() -> str:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500, json={})))
        transport = WeComAppTransport(
            settings=app_settings(app_transport_secret=SecretStr(SENSITIVE_APP_SECRET)),
            tenant_id="tenant-1",
            client=client,
        )
        transport._access_token = SENSITIVE_ACCESS_TOKEN
        transport._access_token_expires_at = float("inf")
        transport._visibility = WeComAppVisibility(
            users=frozenset({"user-1"}),
            parties=frozenset(),
            tags=frozenset(),
            expires_at=float("inf"),
        )
        try:
            await transport.send(
                target=WeComAppTarget(users=("user-1",)),
                message_type="text",
                payload={"content": "safe content"},
            )
        except WeComAppApiError as exc:
            rendered = StringIO()
            console = Console(file=rendered, force_terminal=False, width=160)
            console.print(
                Traceback.from_exception(
                    type(exc),
                    exc,
                    exc.__traceback__,
                    show_locals=True,
                )
            )
            send_failure = rendered.getvalue()
        else:  # pragma: no cover - defensive assertion around the synthetic failure.
            raise SyntheticFailureMissing
        await client.aclose()

        token_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(500, json={}))
        )
        token_transport = WeComAppTransport(
            settings=app_settings(app_transport_secret=SecretStr(SENSITIVE_APP_SECRET)),
            tenant_id="tenant-1",
            client=token_client,
        )
        try:
            await token_transport.refresh_visibility()
        except WeComAppApiError as exc:
            rendered = StringIO()
            console = Console(file=rendered, force_terminal=False, width=160)
            console.print(
                Traceback.from_exception(
                    type(exc),
                    exc,
                    exc.__traceback__,
                    show_locals=True,
                )
            )
            token_failure = rendered.getvalue()
        else:  # pragma: no cover - defensive assertion around the synthetic failure.
            raise SyntheticFailureMissing
        await token_client.aclose()
        return send_failure + token_failure

    traceback_output = asyncio.run(scenario())

    assert SENSITIVE_APP_SECRET not in traceback_output
    assert SENSITIVE_ACCESS_TOKEN not in traceback_output


def test_application_visibility_and_partial_delivery_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/message/send":
            return httpx.Response(200, json={"errcode": 0, "invaliduser": "user-1"})
        raise AssertionError(request.url)

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = WeComAppTransport(settings=app_settings(), tenant_id="tenant-1", client=client)
        transport._access_token = "token-1"  # noqa: S105 - deterministic fake test credential
        transport._access_token_expires_at = float("inf")
        transport._visibility = WeComAppVisibility(
            users=frozenset({"user-1"}),
            parties=frozenset({"2"}),
            tags=frozenset(),
            expires_at=float("inf"),
        )
        with pytest.raises(WeComAppVisibilityDenied):
            await transport.send(
                target=WeComAppTarget(users=("user-outside",)),
                message_type="text",
                payload={"content": "no"},
            )
        with pytest.raises(WeComAppPartialDelivery):
            await transport.send(
                target=WeComAppTarget(users=("user-1",)),
                message_type="text",
                payload={"content": "partial"},
            )
        await client.aclose()

    asyncio.run(scenario())


def test_application_uploads_temporary_file_media() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/cgi-bin/media/upload"
            assert request.url.params["type"] == "file"
            assert b'filename="report.pdf"' in request.content
            assert b"PDF-DATA" in request.content
            return httpx.Response(200, json={"errcode": 0, "media_id": "media-1"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = WeComAppTransport(settings=app_settings(), tenant_id="tenant-1", client=client)
        transport._access_token = "token-1"  # noqa: S105 - deterministic fake test credential
        transport._access_token_expires_at = float("inf")
        media_id = await transport.upload_media(
            media_type="file",
            filename="report.pdf",
            content=b"PDF-DATA",
            content_type="application/pdf",
        )
        assert media_id == "media-1"
        await client.aclose()

    asyncio.run(scenario())


def test_application_encrypted_callback_is_normalized_and_acknowledged() -> None:
    settings = app_settings()
    transport = WeComAppTransport(settings=settings, tenant_id="tenant-1")
    app = FastAPI()
    received: list[dict[str, Any]] = []

    async def inbound(data: dict[str, Any]) -> None:
        received.append(data)

    transport.bind_inbound(inbound)
    transport.mount(app)
    transport._visibility = WeComAppVisibility(
        users=frozenset({"user-1"}),
        parties=frozenset(),
        tags=frozenset(),
        expires_at=float("inf"),
    )
    crypto = WeComJsonCrypto(token="AppCallbackToken1", encoding_aes_key=AES_KEY, receive_id="corp-1")
    plain = (
        "<xml><ToUserName>corp-1</ToUserName><FromUserName>user-1</FromUserName>"
        "<CreateTime>1788250000</CreateTime><MsgType>text</MsgType><Content>你好</Content>"
        "<MsgId>msg-app-1</MsgId><AgentID>1000005</AgentID></xml>"
    )
    encrypted = crypto.encrypt_message(plain, nonce="nonce-1", timestamp="1788250000")
    outer = f"<xml><Encrypt>{encrypted.encrypt}</Encrypt><AgentID>1000005</AgentID></xml>"

    with TestClient(app) as client:
        health = client.get("/health/wecom-app")
        response = client.post(
            settings.app_callback_path,
            params={
                "msg_signature": encrypted.msg_signature,
                "timestamp": encrypted.timestamp,
                "nonce": encrypted.nonce,
            },
            content=outer,
            headers={"content-type": "application/xml"},
        )

    assert health.json() == {
        "ok": True,
        "channel": "wecom",
        "transport": "application",
        "enabled": True,
        "visibility_loaded": True,
    }
    assert response.status_code == 200
    assert response.text == "success"
    assert received == [
        {
            "msgid": "msg-app-1",
            "msgtype": "text",
            "agentid": "1000005",
            "chattype": "single",
            "from": {"userid": "user-1"},
            "_agentseek_wecom_app": True,
            "text": {"content": "你好"},
        }
    ]


def test_application_encrypted_event_callback_gets_a_stable_private_message_id() -> None:
    settings = app_settings()
    transport = WeComAppTransport(settings=settings, tenant_id="tenant-1")
    app = FastAPI()
    received: list[dict[str, Any]] = []

    async def inbound(data: dict[str, Any]) -> None:
        received.append(data)

    transport.bind_inbound(inbound)
    transport.mount(app)
    crypto = WeComJsonCrypto(token="AppCallbackToken1", encoding_aes_key=AES_KEY, receive_id="corp-1")
    plain = (
        "<xml><ToUserName>corp-1</ToUserName><FromUserName>user-1</FromUserName>"
        "<CreateTime>1788250001</CreateTime><MsgType>event</MsgType><Event>click</Event>"
        "<EventKey>M06_CONFIRM</EventKey><AgentID>1000005</AgentID></xml>"
    )
    encrypted = crypto.encrypt_message(plain, nonce="nonce-2", timestamp="1788250001")
    outer = f"<xml><Encrypt>{encrypted.encrypt}</Encrypt><AgentID>1000005</AgentID></xml>"

    with TestClient(app) as client:
        first = client.post(
            settings.app_callback_path,
            params={
                "msg_signature": encrypted.msg_signature,
                "timestamp": encrypted.timestamp,
                "nonce": encrypted.nonce,
            },
            content=outer,
            headers={"content-type": "application/xml"},
        )
        second = client.post(
            settings.app_callback_path,
            params={
                "msg_signature": encrypted.msg_signature,
                "timestamp": encrypted.timestamp,
                "nonce": encrypted.nonce,
            },
            content=outer,
            headers={"content-type": "application/xml"},
        )

    assert first.status_code == second.status_code == 200
    assert len(received) == 2
    assert received[0]["msgid"] == received[1]["msgid"]
    assert received[0]["msgid"].startswith("app-event-")
    assert received[0]["event"] == {"eventtype": "click", "event_key": "M06_CONFIRM"}


def test_channel_application_delivery_is_durable_and_source_scoped(tmp_path) -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/message/send":
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"errcode": 0})
        raise AssertionError(request.url)

    async def scenario() -> None:
        settings = app_settings(
            durable_mode="sqlite",
            durable_sqlite_path=str(tmp_path / "messages.sqlite3"),
            durable_secret=SecretStr("durable-secret-material-that-is-long-enough"),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app_transport = WeComAppTransport(settings=settings, tenant_id="tenant-1", client=client)
        app_transport._access_token = "token-1"  # noqa: S105 - deterministic fake test credential
        app_transport._access_token_expires_at = float("inf")
        app_transport._visibility = WeComAppVisibility(
            users=frozenset({"user-1"}),
            parties=frozenset(),
            tags=frozenset(),
            expires_at=float("inf"),
        )
        store = SqliteDurableMessageStore(
            path=tmp_path / "messages.sqlite3",
            secret=SecretStr("durable-secret-material-that-is-long-enough"),
        )
        channel = WeComChannel(
            on_receive=None,
            settings=settings,
            app_transport=app_transport,
            durable_store=store,
        )
        target = WeComAppTarget(users=("user-1",))
        first = await channel.send_application_message(
            digital_employee_id="industry-report",
            target=target,
            message_type="text",
            payload={"content": "通知"},
            idempotency_key="work-1:notice-1",
        )
        duplicate = await channel.send_application_message(
            digital_employee_id="industry-report",
            target=target,
            message_type="text",
            payload={"content": "通知"},
            idempotency_key="work-1:notice-1",
        )
        assert first == "succeeded"
        assert duplicate == "skipped"
        assert len(calls) == 1
        rows = store.claim_recoverable_outbox(
            now=datetime.now(UTC),
            owner="test-owner",
            lease_duration=timedelta(seconds=60),
            limit=10,
        )
        assert rows == []
        with pytest.raises(WeComAppVisibilityDenied):
            await channel.send_application_message(
                digital_employee_id="unregistered",
                target=target,
                message_type="text",
                payload={"content": "通知"},
                idempotency_key="work-1:notice-2",
            )
        await client.aclose()

    asyncio.run(scenario())


def test_application_probe_targets_user_party_tag_and_file(tmp_path) -> None:
    sends: list[dict[str, Any]] = []
    upload_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_count
        if request.url.path == "/cgi-bin/media/upload":
            upload_count += 1
            return httpx.Response(200, json={"errcode": 0, "media_id": "media-probe-1"})
        if request.url.path == "/cgi-bin/message/send":
            sends.append(json.loads(request.content))
            return httpx.Response(200, json={"errcode": 0})
        raise AssertionError(request.url)

    async def scenario() -> None:
        probe_file = tmp_path / "m06-probe.txt"
        probe_file.write_text("M0.6 harmless probe", encoding="utf-8")
        settings = app_settings(
            durable_mode="sqlite",
            durable_sqlite_path=str(tmp_path / "messages.sqlite3"),
            durable_secret=SecretStr("durable-secret-material-that-is-long-enough"),
            app_proactive_probe_trigger="M0.6自建应用主动探针",
            app_proactive_probe_userid="user-1",
            app_proactive_probe_party_id="2",
            app_proactive_probe_tag_id="3",
            app_proactive_probe_file_path=str(probe_file),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app_transport = WeComAppTransport(settings=settings, tenant_id="tenant-1", client=client)
        app_transport._access_token = "token-1"  # noqa: S105 - deterministic fake test credential
        app_transport._access_token_expires_at = float("inf")
        app_transport._visibility = WeComAppVisibility(
            users=frozenset({"user-1"}),
            parties=frozenset({"2"}),
            tags=frozenset({"3"}),
            expires_at=float("inf"),
        )
        channel = WeComChannel(
            on_receive=None,
            settings=settings,
            app_transport=app_transport,
        )
        await channel._run_application_proactive_probe("inbound-message-1")
        await channel.stop()
        await client.aclose()

    asyncio.run(scenario())

    assert upload_count == 1
    assert len(sends) == 4
    assert [(item["msgtype"], item.get("touser"), item.get("toparty"), item.get("totag")) for item in sends] == [
        ("text", "user-1", None, None),
        ("markdown", None, "2", None),
        ("textcard", None, None, "3"),
        ("file", "user-1", None, None),
    ]
    assert sends[-1]["file"] == {"media_id": "media-probe-1"}


def test_application_inbound_media_uses_transport_secret() -> None:
    settings = app_settings(app_secret="identity-helper-secret")
    channel = WeComChannel(on_receive=None, settings=settings)

    client = channel._get_application_media_client()

    assert isinstance(client, WeComMediaClient)
    assert client.app_secret == "application-secret"  # noqa: S105 - deterministic fake test credential
    assert client.app_secret != settings.app_secret


def test_application_inbox_recovers_without_a_callback_response_capability(tmp_path) -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/message/send":
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"errcode": 0})
        raise AssertionError(request.url)

    async def scenario() -> None:
        path = tmp_path / "messages.sqlite3"
        settings = app_settings(
            durable_mode="sqlite",
            durable_sqlite_path=str(path),
            durable_secret=SecretStr("durable-secret-material-that-is-long-enough"),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app_transport = WeComAppTransport(settings=settings, tenant_id="tenant-1", client=client)
        app_transport._access_token = "token-1"  # noqa: S105 - deterministic fake test credential
        app_transport._access_token_expires_at = float("inf")
        app_transport._visibility = WeComAppVisibility(
            users=frozenset({"user-1"}),
            parties=frozenset(),
            tags=frozenset(),
            expires_at=float("inf"),
        )
        store = SqliteDurableMessageStore(
            path=path,
            secret=SecretStr("durable-secret-material-that-is-long-enough"),
        )
        payload = {
            "msgid": "app-recovery-1",
            "msgtype": "text",
            "agentid": "1000005",
            "chattype": "single",
            "from": {"userid": "user-1"},
            "_agentseek_wecom_app": True,
            "text": {"content": "请回复应用恢复正常"},
        }
        now = datetime.now(UTC)
        admission = store.admit_inbound(
            message_id="app-recovery-1",
            address=app_transport.address_for(payload),
            stream_id="app-recovery-stream",
            payload=payload,
            now=now,
        )
        record = store.claim_inbox(
            admission.record.inbox_id,
            now=now,
            owner="dead-owner",
            lease_duration=timedelta(seconds=1),
        )
        assert record is not None
        store.release_owner("dead-owner", now=now)
        recovered = store.claim_recoverable_inbox(
            now=now + timedelta(seconds=2),
            owner="recovery-owner",
            lease_duration=timedelta(seconds=60),
            limit=10,
        )
        assert len(recovered) == 1
        received: list[str] = []
        channel = WeComChannel(
            on_receive=None,
            settings=settings,
            app_transport=app_transport,
            durable_store=store,
        )

        async def on_receive(message: ChannelMessage) -> None:
            received.append(message.content)
            await channel.send(
                ChannelMessage(
                    session_id=message.session_id,
                    channel="wecom",
                    chat_id=message.chat_id,
                    content="应用恢复正常",
                )
            )

        channel.bind_receiver(on_receive)
        await channel._recover_inbox(recovered[0])
        await wait_until(lambda: len(calls) == 1)
        await channel.stop()
        await client.aclose()

        assert received == ["请回复应用恢复正常"]
        assert calls[0]["touser"] == "user-1"
        assert calls[0]["text"] == {"content": "应用恢复正常"}
        assert store.get_inbox(admission.record.inbox_id).status == "completed"

    asyncio.run(scenario())


def test_application_partial_and_ambiguous_outbox_fail_closed(tmp_path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/cgi-bin/message/send":
            calls += 1
            return httpx.Response(200, json={"errcode": 0, "invaliduser": "user-1"})
        raise AssertionError(request.url)

    async def scenario() -> None:
        path = tmp_path / "messages.sqlite3"
        settings = app_settings(
            durable_mode="sqlite",
            durable_sqlite_path=str(path),
            durable_secret=SecretStr("durable-secret-material-that-is-long-enough"),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app_transport = WeComAppTransport(settings=settings, tenant_id="tenant-1", client=client)
        app_transport._access_token = "token-1"  # noqa: S105 - deterministic fake test credential
        app_transport._access_token_expires_at = float("inf")
        app_transport._visibility = WeComAppVisibility(
            users=frozenset({"user-1"}),
            parties=frozenset(),
            tags=frozenset(),
            expires_at=float("inf"),
        )
        store = SqliteDurableMessageStore(
            path=path,
            secret=SecretStr("durable-secret-material-that-is-long-enough"),
        )
        channel = WeComChannel(
            on_receive=None,
            settings=settings,
            app_transport=app_transport,
            durable_store=store,
        )
        with pytest.raises(WeComAppPartialDelivery):
            await channel.send_application_message(
                digital_employee_id="industry-report",
                target=WeComAppTarget(users=("user-1",)),
                message_type="text",
                payload={"content": "partial"},
                idempotency_key="partial-1",
            )
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT status, last_error_type FROM wecom_outbox"
            ).fetchone() == ("blocked", "partial_delivery")

        now = datetime.now(UTC)
        ambiguous = store.enqueue_outbox(
            inbox_id=None,
            stream_id="ambiguous-stream",
            message_type="wecom_app_text",
            envelope={
                "digital_employee_id": "industry-report",
                "target": {"users": ["user-1"], "parties": [], "tags": []},
                "payload": {"content": "ambiguous"},
            },
            reply_deadline=now + timedelta(hours=1),
            now=now,
        )
        first_claim = store.claim_outbox(
            ambiguous.outbox_id,
            now=now,
            owner="dead-owner",
            lease_duration=timedelta(seconds=1),
        )
        assert first_claim is not None and first_claim.attempts == 1
        recovered = store.claim_recoverable_outbox(
            now=now + timedelta(seconds=2),
            owner="recovery-owner",
            lease_duration=timedelta(seconds=60),
            limit=10,
        )
        assert len(recovered) == 1 and recovered[0].attempts == 2
        await channel._recover_application_outbox(recovered[0])
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT status, last_error_type FROM wecom_outbox WHERE outbox_id = ?",
                (ambiguous.outbox_id,),
            ).fetchone() == ("blocked", "delivery_outcome_ambiguous")
        await channel.stop()
        await client.aclose()

    asyncio.run(scenario())

    assert calls == 1
