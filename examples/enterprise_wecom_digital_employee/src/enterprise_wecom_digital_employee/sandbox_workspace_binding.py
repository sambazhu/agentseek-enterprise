"""Gateway-only immutable task-to-file bindings. Queries never materialize files."""

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from agentseek_files.models import FileRecord, FileScope
from agentseek_wecom.file_delivery import file_ref

from .sandbox_authorization import scoped_owner


def _private(path, directory=False):
    info = path.lstat()
    if (path.resolve() != path or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError("private binding storage required")


class WorkspaceBindings:
    def __init__(self, runner, files):
        self.runner, self.files = runner, files
        self.path = runner.store.path.parent / "workspace-bindings.sqlite"

    def _identity(self, scope, request, outcome):
        if (len(scope) != 3 or any(not isinstance(p, str) or p in {"", ".", ".."}
                                  or "/" in p or "\\" in p for p in scope)):
            raise ValueError("invalid scope")
        actual = self.runner.store.snapshot(request)
        if (request.owner_id != scoped_owner(scope) or actual != outcome
                or outcome.state != "succeeded" or not outcome.cleanup_confirmed):
            raise ValueError("binding outcome mismatch")
        data = self.runner.store.read(request.owner_id, outcome.artifact_ref)
        value = {"scope": list(scope), "request": asdict(request), "attempt": outcome.attempt,
                 "artifact_ref": outcome.artifact_ref, "sha256": hashlib.sha256(data).hexdigest()}
        return json.dumps(value, sort_keys=True, separators=(",", ":")), data

    def _validate(self, scope, payload, data, now):
        record = FileRecord.from_dict(json.loads(payload))
        digest = hashlib.sha256(data).hexdigest()
        parts = Path(record.relative_dir).parts
        if (len(parts) != 6 or parts[:2] != tuple(scope[:2]) or parts[2] != record.date
                or parts[3:] != (scope[2], "outbound", "file_" + digest[:16])
                or (record.tenant_key, record.employee_key, record.session_key) != tuple(scope)
                or record.direction != "outbound" or record.sha256 != digest or record.size_bytes != len(data)
                or record.file_id != "file_" + digest[:16] or record.filename != "summary.csv"):
            raise ValueError("workspace binding mismatch")
        path = self.files.root_dir.resolve() / record.relative_dir
        if (path.resolve() != path or self.files.original_path(record).resolve() != path / "original"
                or (path / "metadata.json").is_symlink()):
            raise ValueError("workspace path mismatch")
        current = self.files.load_record(record.relative_dir)
        expiry = datetime.fromisoformat(record.expires_at or "")
        if current.to_dict() != record.to_dict() or expiry.tzinfo is None or now >= expiry:
            raise ValueError("workspace changed or expired")
        fd = os.open(self.files.original_path(record), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != len(data) or source.read(len(data) + 1) != data:
                raise ValueError("workspace digest mismatch")
        return record

    def read(self, scope, request, outcome, *, now=None):
        identity, data = self._identity(scope, request, outcome)
        _private(self.path.parent, directory=True)
        _private(self.path)
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT identity,record,status FROM bindings WHERE attempt=?", (outcome.attempt,)).fetchone()
        if row is None or row[0] != identity or row[2] != "ready":
            raise ValueError("workspace association unavailable")
        return self._validate(scope, row[1], data, now or datetime.now(UTC))

    def publish(self, scope, request, outcome, *, now=None):
        """Fresh run only. Interrupted intents remain pending, never automatically retried."""
        identity, data = self._identity(scope, request, outcome)
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("aware timestamp required")
        now = now.astimezone(UTC)
        _private(self.path.parent, directory=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        os.close(fd)
        _private(self.path)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS bindings(attempt TEXT PRIMARY KEY,identity TEXT NOT NULL,record TEXT,status TEXT NOT NULL)")
            with db:
                inserted = db.execute("INSERT OR IGNORE INTO bindings VALUES (?,?,NULL,'pending')",
                                      (outcome.attempt, identity)).rowcount
            if not inserted:
                return self.read(scope, request, outcome, now=now)
            # Commit intent before file I/O; serialize publishers sharing this ledger.
            db.execute("BEGIN IMMEDIATE")
            relative = Path(scope[0]) / scope[1] / now.date().isoformat() / scope[2] / "outbound" / ("file_" + hashlib.sha256(data).hexdigest()[:16])
            target = self.files.root_dir.resolve() / relative
            if target.resolve() != target:
                raise ValueError("workspace path mismatch")
            if target.exists() or target.is_symlink():
                record = self.files.load_record(relative.as_posix())
            else:
                record = self.files.store_bytes(scope=FileScope(*scope), filename="summary.csv", data=data,
                                               mime_type="text/csv", direction="outbound", now=now)
            payload = json.dumps(record.to_dict(), sort_keys=True)
            self._validate(scope, payload, data, now)
            db.execute("UPDATE bindings SET record=?,status='ready' WHERE attempt=? AND status='pending'",
                       (payload, outcome.attempt))
            db.commit()
        return record


def association(record, request, outcome):
    return {"state": "verified", "request_id": request.request_id, "attempt": outcome.attempt,
            "artifact_ref": outcome.artifact_ref, "file_ref": file_ref(record), "sha256": record.sha256}
