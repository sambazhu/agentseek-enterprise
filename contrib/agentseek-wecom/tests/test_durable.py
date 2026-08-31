from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from agentseek_wecom.addressing import callback_conversation_address
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.durable import DurableSchemaError, DurableStoreError, SqliteDurableMessageStore
from pydantic import SecretStr, ValidationError

TEST_KEY_MATERIAL = "durable-test-key-material-with-at-least-32-characters"


def _store(tmp_path, *, secret: str = TEST_KEY_MATERIAL) -> SqliteDurableMessageStore:
    return SqliteDurableMessageStore(
        path=tmp_path / "runtime" / "wecom.sqlite3",
        secret=SecretStr(secret),
    )


def _payload(*, msgid: str = "message-1") -> dict[str, object]:
    return {
        "msgid": msgid,
        "msgtype": "text",
        "aibotid": "bot-1",
        "from": {"userid": "encrypted-user"},
        "responseurl": "https://qyapi.weixin.qq.com/secret-response-capability",
        "text": {"content": "CONFIDENTIAL-MESSAGE-CONTENT"},
    }


def test_sqlite_settings_are_explicit_and_hide_the_secret() -> None:
    settings = WeComSettings(durable_mode="sqlite", durable_secret=SecretStr(TEST_KEY_MATERIAL))

    assert settings.durable_mode == "sqlite"
    assert TEST_KEY_MATERIAL not in repr(settings)

    with pytest.raises(ValidationError):
        WeComSettings(durable_mode="sqlite", durable_secret=SecretStr("too-short"))


def test_default_memory_mode_does_not_require_a_secret() -> None:
    settings = WeComSettings()

    assert settings.durable_mode == "memory"
    assert settings.durable_secret.get_secret_value() == ""


def test_inbox_admission_is_idempotent_across_store_instances(tmp_path) -> None:
    now = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    payload = _payload()
    address = callback_conversation_address(payload, tenant_id="tenant-1", interacted_at=now)
    first = _store(tmp_path)

    admitted = first.admit_inbound(
        message_id="message-1",
        address=address,
        stream_id="stream-original",
        payload=payload,
        now=now,
    )
    duplicate = _store(tmp_path).admit_inbound(
        message_id="message-1",
        address=address,
        stream_id="stream-new",
        payload=payload,
        now=now + timedelta(seconds=1),
    )

    assert admitted.admitted is True
    assert duplicate.admitted is False
    assert duplicate.record.inbox_id == admitted.record.inbox_id
    assert duplicate.record.stream_id == "stream-original"
    assert duplicate.record.payload == payload


def test_same_message_id_is_isolated_by_tenant_and_bot(tmp_path) -> None:
    now = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    store = _store(tmp_path)
    payload_a = _payload()
    payload_b = {**payload_a, "aibotid": "bot-2"}
    address_a = callback_conversation_address(payload_a, tenant_id="tenant-a", interacted_at=now)
    address_b = callback_conversation_address(payload_b, tenant_id="tenant-b", interacted_at=now)

    first = store.admit_inbound(
        message_id="message-1", address=address_a, stream_id="stream-a", payload=payload_a, now=now
    )
    second = store.admit_inbound(
        message_id="message-1", address=address_b, stream_id="stream-b", payload=payload_b, now=now
    )

    assert first.record.inbox_id != second.record.inbox_id


def test_payload_and_response_capability_are_encrypted_at_rest(tmp_path) -> None:
    now = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    payload = _payload()
    address = callback_conversation_address(payload, tenant_id="tenant-1", interacted_at=now)
    store = _store(tmp_path)
    inbox = store.admit_inbound(
        message_id="message-1", address=address, stream_id="stream-1", payload=payload, now=now
    ).record
    store.enqueue_outbox(
        inbox_id=inbox.inbox_id,
        stream_id=inbox.stream_id,
        message_type="markdown",
        envelope={
            "response_url": payload["responseurl"],
            "content": "CONFIDENTIAL-OUTBOUND-CONTENT",
        },
        reply_deadline=address.reply_deadline,
        now=now,
    )

    database_bytes = (tmp_path / "runtime" / "wecom.sqlite3").read_bytes()

    assert b"CONFIDENTIAL-MESSAGE-CONTENT" not in database_bytes
    assert b"CONFIDENTIAL-OUTBOUND-CONTENT" not in database_bytes
    assert b"secret-response-capability" not in database_bytes
    assert b"message-1" not in database_bytes


def test_wrong_secret_fails_closed_when_record_is_read(tmp_path) -> None:
    now = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    payload = _payload()
    address = callback_conversation_address(payload, tenant_id="tenant-1", interacted_at=now)
    _store(tmp_path).admit_inbound(
        message_id="message-1", address=address, stream_id="stream-1", payload=payload, now=now
    )
    wrong = _store(tmp_path, secret="different-durable-secret-with-32-characters")

    with pytest.raises(DurableStoreError, match="authentication failed"):
        wrong.claim_recoverable_inbox(
            now=now,
            owner="new-process",
            lease_duration=timedelta(seconds=60),
            limit=10,
        )


def test_outbox_recovery_uses_a_lease_and_stops_after_delivery(tmp_path) -> None:
    now = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    store = _store(tmp_path)
    outbox = store.enqueue_outbox(
        inbox_id=None,
        stream_id="stream-1",
        message_type="markdown",
        envelope={"response_url": "https://example.invalid/capability", "content": "done"},
        reply_deadline=now + timedelta(hours=1),
        now=now,
    )

    first_claim = store.claim_recoverable_outbox(
        now=now, owner="process-a", lease_duration=timedelta(seconds=60), limit=10
    )
    competing_claim = _store(tmp_path).claim_outbox(
        outbox.outbox_id,
        now=now + timedelta(seconds=1),
        owner="process-b",
        lease_duration=timedelta(seconds=60),
    )
    store.mark_outbox(outbox.outbox_id, "delivered", now=now + timedelta(seconds=2))
    after_delivery = store.claim_recoverable_outbox(
        now=now + timedelta(seconds=120), owner="process-c", lease_duration=timedelta(seconds=60), limit=10
    )

    assert [record.outbox_id for record in first_claim] == [outbox.outbox_id]
    assert competing_claim is None
    assert after_delivery == []


def test_graceful_owner_release_makes_inbox_immediately_recoverable(tmp_path) -> None:
    now = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    payload = _payload()
    address = callback_conversation_address(payload, tenant_id="tenant-1", interacted_at=now)
    store = _store(tmp_path)
    inbox = store.admit_inbound(
        message_id="message-1",
        address=address,
        stream_id="stream-1",
        payload=payload,
        now=now,
    ).record
    claimed = store.claim_inbox(
        inbox.inbox_id,
        now=now,
        owner="old-process",
        lease_duration=timedelta(minutes=10),
    )

    store.release_owner("old-process", now=now + timedelta(seconds=1))
    recovered = store.claim_recoverable_inbox(
        now=now + timedelta(seconds=1),
        owner="new-process",
        lease_duration=timedelta(minutes=10),
        limit=10,
    )

    assert claimed is not None
    assert [record.inbox_id for record in recovered] == [inbox.inbox_id]


def test_expired_reply_windows_are_not_recovered(tmp_path) -> None:
    now = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    payload = _payload()
    address = callback_conversation_address(payload, tenant_id="tenant-1", interacted_at=now)
    store = _store(tmp_path)
    store.admit_inbound(
        message_id="message-1", address=address, stream_id="stream-1", payload=payload, now=now
    )
    store.enqueue_outbox(
        inbox_id=None,
        stream_id="expired-stream",
        message_type="markdown",
        envelope={"response_url": "https://example.invalid/capability", "content": "done"},
        reply_deadline=now - timedelta(seconds=1),
        now=now,
    )

    assert store.claim_recoverable_inbox(
        now=now + timedelta(hours=2), owner="process", lease_duration=timedelta(seconds=60), limit=10
    ) == []
    assert store.claim_recoverable_outbox(
        now=now, owner="process", lease_duration=timedelta(seconds=60), limit=10
    ) == []


def test_sqlite_file_permissions_are_owner_only(tmp_path) -> None:
    store = _store(tmp_path)

    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o077 == 0


def test_schema_version_mismatch_fails_closed(tmp_path) -> None:
    path = tmp_path / "runtime" / "wecom.sqlite3"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE wecom_durable_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO wecom_durable_meta(key, value) VALUES ('schema_version', '999')")
    connection.commit()
    connection.close()

    with pytest.raises(DurableSchemaError, match="revision 999"):
        SqliteDurableMessageStore(path=path, secret=SecretStr(TEST_KEY_MATERIAL))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
def test_sqlite_path_rejects_symlinks(tmp_path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(target)

    with pytest.raises(DurableStoreError, match="symlink"):
        SqliteDurableMessageStore(path=link, secret=SecretStr(TEST_KEY_MATERIAL))
