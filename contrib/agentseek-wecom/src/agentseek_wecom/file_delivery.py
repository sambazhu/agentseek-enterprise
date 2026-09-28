"""Explicit, requester-only native file delivery; no sandbox or automatic retry.

Capabilities are issued by the inbound channel, never by the model. The small
private ledger reserves before upload and deliberately has no recovery sender.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_BYTES = 20 * 1024 * 1024
STATE_KEY = "_native_file_delivery"
_CAPABILITIES: dict[str, tuple[float, Any]] = {}


class DeliveryDenied(ValueError):
    """A fixed-code rejection; never include exception text in tool results."""


def issue_capability(binding, *, clock=time.monotonic) -> str:
    now = clock()
    for token, (expiry, _) in tuple(_CAPABILITIES.items()):
        if expiry <= now:
            del _CAPABILITIES[token]
    if len(_CAPABILITIES) >= 1024:
        raise DeliveryDenied("capacity")
    token = secrets.token_hex(32)
    _CAPABILITIES[token] = (now + 600, binding)
    return token


def resolve_capability(token: str, scope: tuple[str, str, str], *, clock=time.monotonic):
    entry = _CAPABILITIES.get(token) if isinstance(token, str) else None
    if entry is None or entry[0] <= clock() or entry[1].scope != scope:
        raise DeliveryDenied("unavailable")
    return entry[1]


def explicit_request(text: str) -> tuple[str, bool] | None:
    """Full-message grammar: no quoted instructions, implicit latest or bulk sends."""
    text = text.strip()
    match = re.fullmatch(r"(?:请)?把\s*(\S{1,180}?)\s*(再)?发给我[。！!]?", text)
    if match:
        return match[1], bool(match[2])
    match = re.fullmatch(r"(?:请)?(发送|重发)工作区文件\s+(\S{1,180})", text)
    if match:
        return match[2], match[1] == "重发"
    return None


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def file_ref(record) -> str:
    return "workspace_" + _digest([record.relative_dir, record.sha256])


class DeliveryLedger:
    def __init__(self, directory: Path):
        if not directory.is_absolute() or directory.resolve() != directory:
            raise DeliveryDenied("private_directory_required")
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise DeliveryDenied("private_directory_required")
        self.path = directory / "native-file-deliveries.sqlite"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                raise DeliveryDenied("private_database_required")
        finally:
            os.close(fd)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, status TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, key: str) -> tuple[bool, str]:
        with self.connect() as db:
            inserted = db.execute("INSERT OR IGNORE INTO deliveries VALUES (?, 'inflight')", (key,)).rowcount
            status = db.execute("SELECT status FROM deliveries WHERE id=?", (key,)).fetchone()[0]
        return bool(inserted), status

    def finish(self, key: str, status: str):
        if status not in {"api_accepted", "upload_failed", "uncertain"}:
            raise DeliveryDenied("invalid_status")
        with self.connect() as db:
            db.execute("UPDATE deliveries SET status=? WHERE id=? AND status='inflight'", (status, key))


@dataclass
class FileDeliveryBinding:
    scope: tuple[str, str, str]
    message_id: str
    user_text: str
    recipient_key: str  # hash of configured application identity + trusted recipient
    store: Any
    ledger: DeliveryLedger
    upload: Any
    send: Any
    clock: Any = time.time

    def records(self):
        if not all(re.fullmatch(r"(?:hmac|sha256)-[a-f0-9]{64}", s) for s in self.scope):
            raise DeliveryDenied("scope")
        tenant, user, session = self.scope
        root = self.store.root_dir.resolve()
        base = root / tenant / user
        result = []
        for count, path in enumerate(base.glob(f"*/{session}/outbound/*/metadata.json")):
            if count >= 1000:
                raise DeliveryDenied("catalog_limit")
            if path.resolve() != path or path.stat().st_size > 65536:
                raise DeliveryDenied("metadata")
            record = self.store.load_record(path.parent.relative_to(root).as_posix())
            try:
                self._validate(record, path.parent)
            except DeliveryDenied as exc:
                if str(exc) == "expired":
                    continue
                raise
            result.append(record)
        return result

    def _validate(self, record, directory=None):
        if (
            (record.tenant_key, record.employee_key, record.session_key) != self.scope
            or record.direction != "outbound"
            or not re.fullmatch(r"[a-f0-9]{64}", record.sha256)
            or record.file_id != "file_" + record.sha256[:16]
            or type(record.size_bytes) is not int
            or not 5 < record.size_bytes <= MAX_BYTES
        ):
            raise DeliveryDenied("file_contract")
        expected = self.store.root_dir.resolve() / record.relative_dir
        if expected.resolve() != expected or (directory is not None and expected != directory):
            raise DeliveryDenied("file_path")
        parts = Path(record.relative_dir).parts
        if len(parts) != 6 or parts[:2] != self.scope[:2] or parts[3:] != (self.scope[2], "outbound", record.file_id):
            raise DeliveryDenied("file_path")
        expiry = datetime.fromisoformat(record.expires_at or "")
        if expiry.tzinfo is None or not math.isfinite(expiry.timestamp()):
            raise DeliveryDenied("expiry")
        if self.clock() >= expiry.timestamp():
            raise DeliveryDenied("expired")

    def list_files(self):
        return [
            {
                "file_ref": file_ref(r),
                "filename": r.sanitized_filename,
                "sha256": r.sha256,
                "size_bytes": r.size_bytes,
                "expires_at": r.expires_at,
            }
            for r in self.records()
        ]

    def read(self, record) -> bytes:
        current = self.store.load_record(record.relative_dir)
        self._validate(current)
        if current.to_dict() != record.to_dict():
            raise DeliveryDenied("file_changed")
        path = self.store.original_path(current)
        if path.resolve() != path:
            raise DeliveryDenied("file_path")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != current.size_bytes:
                raise DeliveryDenied("file_contract")
            data = stream.read(MAX_BYTES + 1)
        if len(data) != current.size_bytes or hashlib.sha256(data).hexdigest() != current.sha256:
            raise DeliveryDenied("digest_mismatch")
        return data

    async def deliver(self, selected_ref: str):
        intent = explicit_request(self.user_text)
        if intent is None or not self.message_id:
            raise DeliveryDenied("explicit_request_required")
        selector, resend = intent
        records = self.records()
        matches = [r for r in records if selector in {file_ref(r), r.sanitized_filename}]
        if len(matches) != 1:
            raise DeliveryDenied("explicit_unique_file_required")
        record = matches[0]
        if file_ref(record) != selected_ref:
            raise DeliveryDenied("selection_mismatch")
        # Recipient/application changes must never reuse another delivery's receipt.
        key = _digest([self.scope, self.recipient_key, selected_ref, self.message_id if resend else "initial"])
        owned, status = self.ledger.reserve(key)
        if not owned:
            return _receipt(key, status)
        status = "upload_failed"
        try:
            data = self.read(record)
            media_id = await self.upload(
                media_type="file", filename=record.sanitized_filename, content=data, content_type=record.mime_type
            )
            data = b""
            # Crash/cancel/exception from here is ambiguous: neither ledger nor outbox retries it.
            status = "uncertain"
            outcome = await self.send(media_id=media_id, idempotency_key="workspace:" + key)
            if outcome == "succeeded":
                status = "api_accepted"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: S110 - result records failure; never log provider exception text
            pass  # No provider exception, body, recipient or media ID in the model/log channel.
        finally:
            try:
                self.ledger.finish(key, status)
            except Exception:
                # The durable pre-upload reservation remains inflight. Never
                # describe a receipt-persistence failure as a safe rejection.
                status = "uncertain"
        return _receipt(key, status)


def _receipt(key: str, status: str):
    return {
        "delivery_id": key,
        "status": "uncertain" if status == "inflight" else status,
        "api_accepted": None if status in {"inflight", "uncertain"} else status == "api_accepted",
        "user_receipt_confirmed": False,
        "automatic_retry_allowed": False,
    }
