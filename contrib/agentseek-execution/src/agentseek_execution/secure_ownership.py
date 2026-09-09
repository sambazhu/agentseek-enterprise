"""Encrypted Broker ownership repository. Not an encrypted execution ledger.

Uses a dedicated service-owned 0700 directory and 0600 SQLite/key files.
AEAD binds each payload to its opaque index and owner index. No plaintext fallback.
This does not protect against root/same-UID memory access or database rollback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
from dataclasses import asdict
from pathlib import Path
from threading import RLock

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .broker_access import Resource
from .models import Code, ContractError, Scope, canonical, require


def private_file(path: Path, *, create: bool = False) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, Code.DENIED)
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600, Code.DENIED)
    except BaseException:
        os.close(fd)
        raise
    return fd


def load_key(path: Path) -> bytes:
    """Read exactly 32 binary bytes; provisioning is an out-of-band operation."""
    fd = private_file(path)
    try:
        key = os.read(fd, 33)
        require(len(key) == 32, Code.DENIED)
        return key
    finally:
        os.close(fd)


def load_service_key(path: Path) -> str:
    """ASCII service credential in a private file, independent of encryption key."""
    fd = private_file(path)
    try:
        value = os.read(fd, 258)
        require(len(value) <= 257, Code.DENIED)
        result = value.decode("ascii").removesuffix("\n")
        require(32 <= len(result) <= 256 and all(33 <= ord(char) <= 126 for char in result), Code.DENIED)
        return result
    finally:
        os.close(fd)


class SecureOwnership:
    def __init__(self, directory: Path, key: bytes):
        require(type(key) is bytes and len(key) == 32, Code.DENIED)
        # Parent symlinks are rejected rather than silently followed.
        require(directory.is_absolute() and directory.resolve() == directory, Code.DENIED)
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), Code.DENIED)
        require(stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
        path = directory / "ownership.sqlite"
        try:
            fd = private_file(path, create=True)
        except FileExistsError:
            fd = private_file(path)
        os.close(fd)
        self._cipher = AESGCM(key)
        self._index_key = hmac.digest(key, b"agentseek-broker-index-v1", "sha256")
        self._lock = RLock()
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), sealed BLOB NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS resources (id TEXT PRIMARY KEY, owner TEXT NOT NULL, sealed BLOB NOT NULL)"
            )
            row = self._db.execute("SELECT sealed FROM metadata WHERE id=1").fetchone()
            if row is None:
                require(self._db.execute("SELECT count(*) FROM resources").fetchone()[0] == 0, Code.DENIED)
                self._db.execute("INSERT INTO metadata VALUES (1,?)", (self._seal(b"schema-1", b"metadata-v1"),))
            else:
                require(self._open(row[0], b"metadata-v1") == b"schema-1", Code.DENIED)
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            self._db.close()
            raise

    def _index(self, purpose: str, value: str) -> str:
        return hmac.new(self._index_key, canonical([purpose, value]).encode(), hashlib.sha256).hexdigest()

    def _seal(self, payload: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._cipher.encrypt(nonce, payload, aad)

    def _open(self, sealed: bytes, aad: bytes) -> bytes:
        try:
            return self._cipher.decrypt(sealed[:12], sealed[12:], aad)
        except (InvalidTag, ValueError, TypeError):
            raise ContractError(Code.DENIED) from None

    def _decode(self, row: tuple[str, str, bytes]) -> Resource:
        index, owner, sealed = row
        try:
            data = json.loads(self._open(sealed, canonical(["resource-v1", index, owner]).encode()))
            scope = Scope(**data.pop("scope"))
            resource = Resource(scope=scope, **data)
            require(self._index("id", resource.resource_id) == index, Code.DENIED)
            require(self._index("owner", resource.principal_id) == owner, Code.DENIED)
        except (KeyError, TypeError, ValueError):
            raise ContractError(Code.DENIED) from None
        return resource

    def register(self, resource: Resource) -> None:
        """Trusted creation/reconciliation only; immutable, idempotent mapping."""
        require(isinstance(resource, Resource))
        index = self._index("id", resource.resource_id)
        owner = self._index("owner", resource.principal_id)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT id,owner,sealed FROM resources WHERE id=?", (index,)).fetchone()
                if row is not None:
                    require(self._decode(row) == resource, Code.CONFLICT)
                else:
                    aad = canonical(["resource-v1", index, owner]).encode()
                    sealed = self._seal(canonical(asdict(resource)).encode(), aad)
                    self._db.execute("INSERT INTO resources VALUES (?,?,?)", (index, owner, sealed))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def get(self, resource_id: str) -> Resource | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id,owner,sealed FROM resources WHERE id=?", (self._index("id", resource_id),)
            ).fetchone()
            return None if row is None else self._decode(row)

    def owned(self, principal_id: str) -> tuple[Resource, ...]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id,owner,sealed FROM resources WHERE owner=? ORDER BY id", (self._index("owner", principal_id),)
            ).fetchall()
            return tuple(self._decode(row) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._db.close()
