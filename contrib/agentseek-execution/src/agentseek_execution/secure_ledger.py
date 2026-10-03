"""Sensitive-field encryption for the transactional ledger (not SQLCipher).

Opaque UUID IDs, states, times, revisions and contract digests remain queryable.
Owners, create tokens, manifests and historical snapshots are AEAD sealed.
This does not authenticate all SQL bookkeeping or detect whole-database rollback.
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .ledger import Ledger
from .models import Code, ContractError, Execution, Lease, require
from .secure_ownership import private_file


class SecureLedger(Ledger):
    def __init__(self, directory: Path, key: bytes):
        require(type(key) is bytes and len(key) == 32, Code.DENIED)
        require(directory.is_absolute() and directory.resolve() == directory, Code.DENIED)
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), Code.DENIED)
        require(stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
        path = directory / "execution.sqlite"
        try:
            fd = private_file(path, create=True)
        except FileExistsError:
            fd = private_file(path)
        os.close(fd)
        material = HKDF(algorithm=hashes.SHA256(), length=96, salt=None, info=b"agentseek-execution-ledger-v1").derive(
            key
        )
        self._cipher = AESGCM(material[:32])
        self._index = AESSIV(material[32:])
        super().__init__(path)
        try:
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.transaction():
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS encryption_metadata (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)"
                )
                row = self.db.execute("SELECT value FROM encryption_metadata WHERE id=1").fetchone()
                if row is None:
                    # Existing M1 plaintext data is never silently adopted/migrated.
                    for query in (
                        "SELECT count(*) FROM tasks",
                        "SELECT count(*) FROM executions",
                        "SELECT count(*) FROM revisions",
                        "SELECT count(*) FROM attempt_history",
                    ):
                        require(self.db.execute(query).fetchone()[0] == 0, Code.DENIED)
                    self.db.execute(
                        "INSERT INTO encryption_metadata VALUES(1,?)", (self._protect("schema-1", "metadata"),)
                    )
                else:
                    require(self._reveal(row[0], "metadata") == "schema-1", Code.DENIED)
        except BaseException:
            self.close()
            raise

    def _protect(self, value: str, context: str) -> str:
        data = value.encode()
        aad = context.encode()
        if context == "commit-token-index":
            # Deterministic authenticated encryption permits unique token lookup.
            sealed = self._index.encrypt(data, [aad])
        else:
            nonce = os.urandom(12)
            sealed = nonce + self._cipher.encrypt(nonce, data, aad)
        return base64.b64encode(sealed).decode("ascii")

    def _reveal(self, value: str, context: str) -> str:
        try:
            sealed = base64.b64decode(value, validate=True)
            if context == "commit-token-index":
                data = self._index.decrypt(sealed, [context.encode()])
            else:
                data = self._cipher.decrypt(sealed[:12], sealed[12:], context.encode())
            return data.decode()
        except (InvalidTag, ValueError, TypeError, UnicodeError):
            raise ContractError(Code.DENIED) from None

    def begin(self, execution: Execution, owner: str, now: float, ttl: float) -> Lease:
        for value in (execution.execution_id, execution.task.task_id):
            try:
                require(UUID(value).hex == value, Code.INVALID)
            except (ValueError, AttributeError):
                raise ContractError(Code.INVALID) from None
        return super().begin(execution, owner, now, ttl)
