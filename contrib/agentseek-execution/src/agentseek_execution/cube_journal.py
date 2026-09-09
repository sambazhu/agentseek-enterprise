"""Encrypted create-intent/receipt journal; trusted Broker and SDK worker only."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .models import Code, ContractError, canonical, require
from .secure_ownership import private_file


class CubeJournal:
    def __init__(self, directory: Path, key: bytes):
        require(type(key) is bytes and len(key) == 32, Code.DENIED)
        require(directory.is_absolute() and directory.resolve() == directory, Code.DENIED)
        info = directory.stat()
        require(info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o700, Code.DENIED)
        path = directory / "cube-journal.sqlite"
        try:
            fd = private_file(path, create=True)
        except FileExistsError:
            fd = private_file(path)
        os.close(fd)
        self._cipher = AESGCM(key)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, sealed BLOB NOT NULL)")
        # Metadata shares the same authenticated table; wrong keys fail at startup.
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT sealed FROM jobs WHERE id='metadata'").fetchone()
            if row is None:
                require(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0, Code.DENIED)
                self._put("metadata", {"schema": 1})
            else:
                require(self._decode("metadata", row[0]) == {"schema": 1}, Code.DENIED)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            self.db.close()
            raise

    def _decode(self, resource_id: str, sealed: bytes) -> dict:
        try:
            data = self._cipher.decrypt(sealed[:12], sealed[12:], ("cube-journal-v1:" + resource_id).encode())
            result = json.loads(data)
            require(type(result) is dict, Code.DENIED)
        except (InvalidTag, ValueError, TypeError):
            raise ContractError(Code.DENIED) from None
        return result

    def _put(self, resource_id: str, value: dict) -> None:
        nonce = os.urandom(12)
        sealed = nonce + self._cipher.encrypt(
            nonce, canonical(value).encode(), ("cube-journal-v1:" + resource_id).encode()
        )
        self.db.execute(
            "INSERT INTO jobs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET sealed=excluded.sealed", (resource_id, sealed)
        )

    def get(self, resource_id: str) -> dict | None:
        require(resource_id != "metadata", Code.DENIED)
        row = self.db.execute("SELECT sealed FROM jobs WHERE id=?", (resource_id,)).fetchone()
        return None if row is None else self._decode(resource_id, row[0])

    def all(self) -> tuple[dict, ...]:
        return tuple(
            self._decode(row[0], row[1])
            for row in self.db.execute("SELECT id,sealed FROM jobs WHERE id NOT IN ('metadata','socket') ORDER BY id")
        )

    def require_job(self, resource_id: str) -> dict:
        value = self.get(resource_id)
        if value is None:
            raise ContractError(Code.DENIED)
        return value

    def socket_identity(self, value: dict | None = None) -> dict | None:
        if value is not None:
            self._put("socket", value)
        row = self.db.execute("SELECT sealed FROM jobs WHERE id='socket'").fetchone()
        return None if row is None else self._decode("socket", row[0])

    def insert(self, resource_id: str, value: dict) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            require(self.get(resource_id) is None, Code.CONFLICT)
            self._put(resource_id, value)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def patch(self, resource_id: str, **updates: object) -> dict:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            value = self.require_job(resource_id)
            value.update(updates)
            self._put(resource_id, value)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return value

    def close(self) -> None:
        self.db.close()
