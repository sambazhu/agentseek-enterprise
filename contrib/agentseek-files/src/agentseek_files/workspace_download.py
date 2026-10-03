"""Short-lived links for owner-scoped outbound CSV files, without media sending.

Only the trusted application issues links. A browser redeems an opaque bearer
token, not an authenticated user identity; forwarding a link delegates that file
until expiry. Tokens are stored hashed and never put in URL paths or queries.
"""

from __future__ import annotations

import hashlib
import hmac
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
from urllib.parse import urlsplit

from agentseek_files.models import FileRecord, FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore

MAX_BYTES = 65536
ROUTE_PATH = "/ai-server/workspace-files"


class WorkspaceDownloadNotFound(ValueError):
    pass


class WorkspaceDownloadExpired(ValueError):
    pass


@dataclass(frozen=True)
class WorkspaceDownloadSettings:
    public_base_url: str
    grants_directory: Path
    ttl_seconds: int = 600

    def __post_init__(self):
        url = urlsplit(self.public_base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path != ROUTE_PATH
                or any(c.isspace() or ord(c) < 33 for c in self.public_base_url)
                or "\\" in self.public_base_url
                or type(self.ttl_seconds) is not int or not 1 <= self.ttl_seconds <= 3600):
            raise ValueError("invalid workspace download configuration")
        _ = url.port  # Validate a supplied port before accepting configuration.

    @classmethod
    def from_env(cls):
        mode = os.getenv("AGENTSEEK_WORKSPACE_DOWNLOAD_MODE", "disabled").strip()
        if mode == "disabled":
            return None
        if mode != "signed_link":
            raise ValueError("invalid workspace download mode")
        return cls(
            public_base_url=os.environ["AGENTSEEK_WORKSPACE_DOWNLOAD_BASE_URL"].strip(),
            grants_directory=Path(os.environ["AGENTSEEK_WORKSPACE_DOWNLOAD_GRANTS_DIR"]),
            ttl_seconds=int(os.getenv("AGENTSEEK_WORKSPACE_DOWNLOAD_TTL_SECONDS", "600")),
        )


class WorkspaceDownloads:
    def __init__(self, *, store: LocalFileStore, settings: WorkspaceDownloadSettings, clock=time.time):
        self.store, self.settings, self.clock = store, settings, clock
        directory = settings.grants_directory
        if not directory.is_absolute() or directory.resolve() != directory:
            raise ValueError("canonical grant directory required")
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("private grant directory required")
        self.path = directory / "workspace-downloads.sqlite"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("private grant database required")
        finally:
            os.close(fd)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS workspace_downloads(
                id TEXT PRIMARY KEY, token_sha256 TEXT NOT NULL,
                record TEXT NOT NULL, expires REAL NOT NULL)""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def _read_csv(self, expected: FileRecord) -> bytes:
        record = self.store.load_record(expected.relative_dir)
        fields = ("file_id", "tenant_key", "employee_key", "session_key", "relative_dir", "direction",
                  "filename", "sha256", "size_bytes")
        if (any(getattr(record, f) != getattr(expected, f) for f in fields)
                or record.direction != "outbound" or record.filename != "summary.csv"
                or type(record.size_bytes) is not int or not 0 < record.size_bytes <= MAX_BYTES):
            raise WorkspaceDownloadNotFound("workspace file unavailable")
        if record.expires_at is None:
            raise WorkspaceDownloadNotFound("workspace file expiry missing")
        expiry = datetime.fromisoformat(record.expires_at)
        if expiry.tzinfo is None or not math.isfinite(expiry.timestamp()):
            raise WorkspaceDownloadNotFound("workspace file expiry invalid")
        if self.clock() >= expiry.timestamp():
            raise WorkspaceDownloadExpired("workspace file expired")
        fd = os.open(self.store.original_path(record), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise WorkspaceDownloadNotFound("workspace file unavailable")
            data = stream.read(MAX_BYTES + 1)
        if (len(data) != record.size_bytes or hashlib.sha256(data).hexdigest() != record.sha256
                or not data.startswith(b"group,total\n")):
            raise WorkspaceDownloadNotFound("workspace file changed")
        data.decode("utf-8")
        return data

    def issue(self, scope: FileScope, record: FileRecord) -> dict:
        if (record.tenant_key, record.employee_key, record.session_key) != (
                scope.tenant_key, scope.employee_key, scope.session_key):
            raise WorkspaceDownloadNotFound("workspace scope mismatch")
        self._read_csv(record)
        now = self.clock()
        if not math.isfinite(now):
            raise ValueError("invalid clock")
        expires = min(now + self.settings.ttl_seconds, datetime.fromisoformat(record.expires_at).timestamp())
        link_id, token = "workspace_" + secrets.token_hex(32), secrets.token_urlsafe(32)
        with self._connect() as db:
            db.execute("INSERT INTO workspace_downloads VALUES (?,?,?,?)", (
                link_id, hashlib.sha256(token.encode("ascii")).hexdigest(),
                json.dumps(record.to_dict(), sort_keys=True), expires))
        return {"state": "available", "url": f"{self.settings.public_base_url}/{link_id}#{token}",
                "expires_epoch": expires}

    def redeem(self, link_id: str, token: str) -> bytes:
        if (not re.fullmatch(r"workspace_[a-f0-9]{64}", link_id)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)):
            raise WorkspaceDownloadNotFound("workspace link unavailable")
        with self._connect() as db:
            row = db.execute("SELECT token_sha256,record,expires FROM workspace_downloads WHERE id=?", (link_id,)).fetchone()
        if row is None or not hmac.compare_digest(row[0], hashlib.sha256(token.encode("ascii")).hexdigest()):
            raise WorkspaceDownloadNotFound("workspace link unavailable")
        now = self.clock()
        if not math.isfinite(now) or now >= row[2]:
            raise WorkspaceDownloadExpired("workspace link expired")
        return self._read_csv(FileRecord.from_dict(json.loads(row[1])))


def configured_workspace_downloads(store=None):
    settings = WorkspaceDownloadSettings.from_env()
    if settings is None:
        return None
    return WorkspaceDownloads(store=store or LocalFileStore(FilesSettings.from_env()), settings=settings)
