from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from Crypto.Cipher import AES
from pydantic import SecretStr

from agentseek_wecom.addressing import ConversationAddress

InboxStatus = Literal["pending", "processing", "completed", "failed", "blocked"]
OutboxStatus = Literal["pending", "sending", "sent", "delivered", "failed", "blocked"]

_SCHEMA_VERSION = "2"
_INBOX_STATUSES: frozenset[str] = frozenset({"pending", "processing", "completed", "failed", "blocked"})
_OUTBOX_STATUSES: frozenset[str] = frozenset({"pending", "sending", "sent", "delivered", "failed", "blocked"})


class DurableStoreError(RuntimeError):
    """Raised when durable messaging cannot operate safely."""


class DurableSchemaError(DurableStoreError):
    """Raised when the local database revision does not match this runtime."""


@dataclass(frozen=True, slots=True)
class InboxRecord:
    inbox_id: str
    stream_id: str
    status: InboxStatus
    payload: dict[str, Any]
    reply_deadline: datetime | None
    attempts: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InboxAdmission:
    record: InboxRecord
    admitted: bool


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    outbox_id: str
    inbox_id: str | None
    stream_id: str
    message_type: str
    envelope: dict[str, Any]
    status: OutboxStatus
    reply_deadline: datetime | None
    attempts: int
    created_at: datetime
    updated_at: datetime


class DurableMessageStore(Protocol):
    def remember_interaction(self, address: ConversationAddress, *, now: datetime) -> None: ...

    def has_interaction(self, address: ConversationAddress) -> bool: ...

    def admit_inbound(
        self,
        *,
        message_id: str,
        address: ConversationAddress,
        stream_id: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> InboxAdmission: ...

    def mark_inbox(self, inbox_id: str, status: InboxStatus, *, now: datetime, error_type: str = "") -> None: ...

    def claim_inbox(
        self,
        inbox_id: str,
        *,
        now: datetime,
        owner: str,
        lease_duration: timedelta,
    ) -> InboxRecord | None: ...

    def get_inbox(self, inbox_id: str) -> InboxRecord | None: ...

    def claim_recoverable_inbox(
        self,
        *,
        now: datetime,
        owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> list[InboxRecord]: ...

    def enqueue_outbox(
        self,
        *,
        inbox_id: str | None,
        stream_id: str,
        message_type: str,
        envelope: dict[str, Any],
        reply_deadline: datetime | None,
        now: datetime,
    ) -> OutboxRecord: ...

    def claim_outbox(
        self,
        outbox_id: str,
        *,
        now: datetime,
        owner: str,
        lease_duration: timedelta,
    ) -> OutboxRecord | None: ...

    def claim_recoverable_outbox(
        self,
        *,
        now: datetime,
        owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> list[OutboxRecord]: ...

    def mark_outbox(
        self,
        outbox_id: str,
        status: OutboxStatus,
        *,
        now: datetime,
        error_type: str = "",
    ) -> None: ...

    def release_owner(self, owner: str, *, now: datetime) -> None: ...


class SqliteDurableMessageStore:
    """Encrypted, process-restart-safe inbox and outbox for one host."""

    def __init__(self, *, path: str | Path, secret: SecretStr) -> None:
        if len(secret.get_secret_value()) < 32:
            raise ValueError("durable store secret must contain at least 32 characters")
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        self._key = hashlib.sha256(
            b"agentseek-wecom-durable-v1\0" + secret.get_secret_value().encode("utf-8")
        ).digest()
        self._prepare_path()
        self._initialize_schema()

    def admit_inbound(
        self,
        *,
        message_id: str,
        address: ConversationAddress,
        stream_id: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> InboxAdmission:
        now = _aware_utc(now)
        scope = "\x1f".join((address.tenant_id, address.bot_or_agent_id, address.transport, message_id))
        inbox_id = self._stable_id("inbox", scope)
        sealed_payload = self._seal(payload, aad=f"inbox:{inbox_id}")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM wecom_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
            if row is not None:
                return InboxAdmission(record=self._inbox_record(row), admitted=False)
            connection.execute(
                """
                INSERT INTO wecom_inbox (
                    inbox_id, stream_id, status, sealed_payload, reply_deadline,
                    attempts, lease_owner, lease_expires_at, last_error_type,
                    created_at, updated_at
                ) VALUES (?, ?, 'pending', ?, ?, 0, NULL, NULL, '', ?, ?)
                """,
                (
                    inbox_id,
                    stream_id,
                    sealed_payload,
                    _dump_datetime(address.reply_deadline),
                    _dump_datetime(now),
                    _dump_datetime(now),
                ),
            )
            row = connection.execute("SELECT * FROM wecom_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
            if row is None:
                raise DurableStoreError("inbox insert did not return a record")
            return InboxAdmission(record=self._inbox_record(row), admitted=True)

    def remember_interaction(self, address: ConversationAddress, *, now: datetime) -> None:
        now = _aware_utc(now)
        conversation_id = self._conversation_id(address)
        sealed_address = self._seal(
            {
                "tenant_id": address.tenant_id,
                "bot_or_agent_id": address.bot_or_agent_id,
                "transport": address.transport,
                "chat_type": address.chat_type,
                "chat_id": address.chat_id,
                "sender_userid": address.sender_userid,
                "plaintext_userid": address.plaintext_userid,
                "last_interacted_at": address.last_interacted_at.isoformat(),
            },
            aad=f"conversation:{conversation_id}",
        )
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO wecom_conversations (
                    conversation_id, sealed_address, last_interacted_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    sealed_address = excluded.sealed_address,
                    last_interacted_at = excluded.last_interacted_at,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    sealed_address,
                    _dump_datetime(address.last_interacted_at),
                    _dump_datetime(now),
                    _dump_datetime(now),
                ),
            )

    def has_interaction(self, address: ConversationAddress) -> bool:
        conversation_id = self._conversation_id(address)
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT sealed_address FROM wecom_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return False
        # Authenticate the encrypted record before treating it as qualification.
        self._open(bytes(row["sealed_address"]), aad=f"conversation:{conversation_id}")
        return True

    def mark_inbox(self, inbox_id: str, status: InboxStatus, *, now: datetime, error_type: str = "") -> None:
        if status not in _INBOX_STATUSES:
            raise ValueError(f"unsupported inbox status: {status}")
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE wecom_inbox
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    last_error_type = ?, updated_at = ?
                WHERE inbox_id = ?
                """,
                (status, _safe_error_type(error_type), _dump_datetime(_aware_utc(now)), inbox_id),
            )

    def claim_recoverable_inbox(
        self,
        *,
        now: datetime,
        owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> list[InboxRecord]:
        now = _aware_utc(now)
        lease_expires_at = now + lease_duration
        claimed: list[InboxRecord] = []
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM wecom_inbox
                WHERE status IN ('pending', 'processing')
                  AND (reply_deadline IS NULL OR reply_deadline > ?)
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY created_at
                LIMIT ?
                """,
                (_dump_datetime(now), _dump_datetime(now), max(1, limit)),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    """
                    UPDATE wecom_inbox
                    SET status = 'processing', attempts = attempts + 1,
                        lease_owner = ?, lease_expires_at = ?, updated_at = ?
                    WHERE inbox_id = ?
                      AND status IN ('pending', 'processing')
                      AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                    """,
                    (
                        owner,
                        _dump_datetime(lease_expires_at),
                        _dump_datetime(now),
                        row["inbox_id"],
                        _dump_datetime(now),
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                current = connection.execute(
                    "SELECT * FROM wecom_inbox WHERE inbox_id = ?",
                    (row["inbox_id"],),
                ).fetchone()
                if current is not None:
                    claimed.append(self._inbox_record(current))
        return claimed

    def claim_inbox(
        self,
        inbox_id: str,
        *,
        now: datetime,
        owner: str,
        lease_duration: timedelta,
    ) -> InboxRecord | None:
        now = _aware_utc(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE wecom_inbox
                SET status = 'processing', attempts = attempts + 1,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE inbox_id = ?
                  AND status IN ('pending', 'failed', 'processing')
                  AND (reply_deadline IS NULL OR reply_deadline > ?)
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (
                    owner,
                    _dump_datetime(now + lease_duration),
                    _dump_datetime(now),
                    inbox_id,
                    _dump_datetime(now),
                    _dump_datetime(now),
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute("SELECT * FROM wecom_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
            return self._inbox_record(row) if row is not None else None

    def get_inbox(self, inbox_id: str) -> InboxRecord | None:
        connection = self._connection()
        try:
            row = connection.execute("SELECT * FROM wecom_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
        finally:
            connection.close()
        return self._inbox_record(row) if row is not None else None

    def enqueue_outbox(
        self,
        *,
        inbox_id: str | None,
        stream_id: str,
        message_type: str,
        envelope: dict[str, Any],
        reply_deadline: datetime | None,
        now: datetime,
    ) -> OutboxRecord:
        now = _aware_utc(now)
        outbox_id = self._stable_id("outbox", f"{inbox_id or stream_id}\x1f{message_type}")
        sealed_envelope = self._seal(envelope, aad=f"outbox:{outbox_id}")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM wecom_outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO wecom_outbox (
                        outbox_id, inbox_id, stream_id, message_type, sealed_envelope,
                        status, reply_deadline, attempts, lease_owner, lease_expires_at,
                        last_error_type, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, NULL, NULL, '', ?, ?)
                    """,
                    (
                        outbox_id,
                        inbox_id,
                        stream_id,
                        message_type,
                        sealed_envelope,
                        _dump_datetime(reply_deadline),
                        _dump_datetime(now),
                        _dump_datetime(now),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM wecom_outbox WHERE outbox_id = ?",
                    (outbox_id,),
                ).fetchone()
            if row is None:
                raise DurableStoreError("outbox insert did not return a record")
            return self._outbox_record(row)

    def claim_outbox(
        self,
        outbox_id: str,
        *,
        now: datetime,
        owner: str,
        lease_duration: timedelta,
    ) -> OutboxRecord | None:
        now = _aware_utc(now)
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT status FROM wecom_outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if current is None:
                return None
            current_status = str(current["status"])
            claimed_status = "sent" if current_status == "sent" else "sending"
            cursor = connection.execute(
                """
                UPDATE wecom_outbox
                SET status = ?, attempts = attempts + 1,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE outbox_id = ?
                  AND status IN ('pending', 'failed', 'sending', 'sent')
                  AND (reply_deadline IS NULL OR reply_deadline > ?)
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (
                    claimed_status,
                    owner,
                    _dump_datetime(now + lease_duration),
                    _dump_datetime(now),
                    outbox_id,
                    _dump_datetime(now),
                    _dump_datetime(now),
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute("SELECT * FROM wecom_outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
            return self._outbox_record(row) if row is not None else None

    def claim_recoverable_outbox(
        self,
        *,
        now: datetime,
        owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> list[OutboxRecord]:
        now = _aware_utc(now)
        connection = self._connection()
        try:
            rows = connection.execute(
                """
                SELECT outbox_id FROM wecom_outbox
                WHERE status IN ('pending', 'failed', 'sending', 'sent')
                  AND (reply_deadline IS NULL OR reply_deadline > ?)
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY created_at
                LIMIT ?
                """,
                (_dump_datetime(now), _dump_datetime(now), max(1, limit)),
            ).fetchall()
        finally:
            connection.close()
        claimed: list[OutboxRecord] = []
        for row in rows:
            record = self.claim_outbox(
                str(row["outbox_id"]),
                now=now,
                owner=owner,
                lease_duration=lease_duration,
            )
            if record is not None:
                claimed.append(record)
        return claimed

    def mark_outbox(
        self,
        outbox_id: str,
        status: OutboxStatus,
        *,
        now: datetime,
        error_type: str = "",
    ) -> None:
        if status not in _OUTBOX_STATUSES:
            raise ValueError(f"unsupported outbox status: {status}")
        with self._transaction() as connection:
            if status == "sent":
                connection.execute(
                    """
                    UPDATE wecom_outbox
                    SET status = 'sent', last_error_type = ?, updated_at = ?
                    WHERE outbox_id = ?
                    """,
                    (_safe_error_type(error_type), _dump_datetime(_aware_utc(now)), outbox_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE wecom_outbox
                    SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                        last_error_type = ?, updated_at = ?
                    WHERE outbox_id = ?
                    """,
                    (status, _safe_error_type(error_type), _dump_datetime(_aware_utc(now)), outbox_id),
                )

    def release_owner(self, owner: str, *, now: datetime) -> None:
        updated_at = _dump_datetime(_aware_utc(now))
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE wecom_inbox
                SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE lease_owner = ? AND status = 'processing'
                """,
                (updated_at, owner),
            )
            connection.execute(
                """
                UPDATE wecom_outbox
                SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE lease_owner = ? AND status IN ('sending', 'sent')
                """,
                (updated_at, owner),
            )

    def _prepare_path(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise DurableStoreError("durable SQLite path must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            mode = os.fstat(descriptor).st_mode
            if not stat.S_ISREG(mode):
                raise DurableStoreError("durable SQLite path must be a regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _initialize_schema(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS wecom_durable_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT value FROM wecom_durable_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None and str(row["value"]) not in {"1", _SCHEMA_VERSION}:
                raise DurableSchemaError(
                    f"durable SQLite schema revision {row['value']} is unsupported; expected {_SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS wecom_inbox (
                    inbox_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sealed_payload BLOB NOT NULL,
                    reply_deadline TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    last_error_type TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_wecom_inbox_recovery ON wecom_inbox(status, lease_expires_at, created_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS wecom_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    inbox_id TEXT,
                    stream_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    sealed_envelope BLOB NOT NULL,
                    status TEXT NOT NULL,
                    reply_deadline TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    last_error_type TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(inbox_id) REFERENCES wecom_inbox(inbox_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_wecom_outbox_recovery ON wecom_outbox(status, lease_expires_at, created_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS wecom_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    sealed_address BLOB NOT NULL,
                    last_interacted_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            if row is None:
                connection.execute(
                    "INSERT INTO wecom_durable_meta(key, value) VALUES ('schema_version', ?)",
                    (_SCHEMA_VERSION,),
                )
            elif str(row["value"]) == "1":
                connection.execute(
                    "UPDATE wecom_durable_meta SET value = ? WHERE key = 'schema_version'",
                    (_SCHEMA_VERSION,),
                )

    def _inbox_record(self, row: sqlite3.Row) -> InboxRecord:
        status = str(row["status"])
        if status not in _INBOX_STATUSES:
            raise DurableStoreError(f"invalid inbox status in durable store: {status}")
        return InboxRecord(
            inbox_id=str(row["inbox_id"]),
            stream_id=str(row["stream_id"]),
            status=cast(InboxStatus, status),
            payload=self._open(bytes(row["sealed_payload"]), aad=f"inbox:{row['inbox_id']}"),
            reply_deadline=_load_datetime(row["reply_deadline"]),
            attempts=int(row["attempts"]),
            created_at=_required_datetime(row["created_at"]),
            updated_at=_required_datetime(row["updated_at"]),
        )

    def _outbox_record(self, row: sqlite3.Row) -> OutboxRecord:
        status = str(row["status"])
        if status not in _OUTBOX_STATUSES:
            raise DurableStoreError(f"invalid outbox status in durable store: {status}")
        return OutboxRecord(
            outbox_id=str(row["outbox_id"]),
            inbox_id=str(row["inbox_id"]) if row["inbox_id"] is not None else None,
            stream_id=str(row["stream_id"]),
            message_type=str(row["message_type"]),
            envelope=self._open(bytes(row["sealed_envelope"]), aad=f"outbox:{row['outbox_id']}"),
            status=cast(OutboxStatus, status),
            reply_deadline=_load_datetime(row["reply_deadline"]),
            attempts=int(row["attempts"]),
            created_at=_required_datetime(row["created_at"]),
            updated_at=_required_datetime(row["updated_at"]),
        )

    def _stable_id(self, kind: str, value: str) -> str:
        digest = hmac.new(self._key, f"{kind}\0{value}".encode(), hashlib.sha256).hexdigest()
        return f"{kind}_{digest}"

    def _conversation_id(self, address: ConversationAddress) -> str:
        scope = "\x1f".join(
            (
                address.tenant_id,
                address.bot_or_agent_id,
                address.transport,
                address.chat_type,
                address.chat_id,
            )
        )
        return self._stable_id("conversation", scope)

    def _seal(self, value: dict[str, Any], *, aad: str) -> bytes:
        try:
            plaintext = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise DurableStoreError("durable message envelope must be JSON serializable") from exc
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=os.urandom(12), mac_len=16)
        cipher.update(aad.encode())
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        return bytes(cipher.nonce) + tag + ciphertext

    def _open(self, sealed: bytes, *, aad: str) -> dict[str, Any]:
        if len(sealed) < 28:
            raise DurableStoreError("durable message envelope is truncated")
        nonce, tag, ciphertext = sealed[:12], sealed[12:28], sealed[28:]
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce, mac_len=16)
        cipher.update(aad.encode())
        try:
            plaintext = cipher.decrypt_and_verify(ciphertext, tag)
            value = json.loads(plaintext)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DurableStoreError("durable message envelope authentication failed") from exc
        if not isinstance(value, dict):
            raise DurableStoreError("durable message envelope must decode to an object")
        return value

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _transaction(self) -> _SqliteTransaction:
        return _SqliteTransaction(self._connection())


class _SqliteTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> None:
        try:
            self.connection.execute("ROLLBACK" if exc_type is not None else "COMMIT")
        finally:
            self.connection.close()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("durable timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _dump_datetime(value: datetime | None) -> str | None:
    return _aware_utc(value).isoformat() if value is not None else None


def _load_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    return _aware_utc(parsed)


def _required_datetime(value: Any) -> datetime:
    parsed = _load_datetime(value)
    if parsed is None:
        raise DurableStoreError("durable timestamp is missing")
    return parsed


def _safe_error_type(value: str) -> str:
    return "".join(character for character in value if character.isalnum() or character in {"_", "."})[:128]
