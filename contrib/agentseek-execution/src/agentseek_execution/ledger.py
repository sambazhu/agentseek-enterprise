"""Local transactional reference ledger. M1 uses disposable test databases only."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .models import (
    Code,
    Execution,
    FileEntry,
    Lease,
    OutputManifest,
    WorkspaceRevision,
    canonical,
    identifier,
    require,
)

TRANSITIONS = {
    "preparing": {"running", "failed", "reconciling", "cancel_requested"},
    "running": {"exporting", "failed", "reconciling", "cancel_requested"},
    "exporting": {"validating", "failed", "reconciling", "cancel_requested"},
    "validating": {"committing", "failed", "reconciling", "cancel_requested"},
    "committing": {"reconciling"},
    "cancel_requested": {"reconciling"},
    "reconciling": set(),
}


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Ledger:
    def _protect(self, value: str, context: str) -> str:
        """Storage codec seam; the M1 reference ledger remains plaintext."""
        return value

    def _reveal(self, value: str, context: str) -> str:
        return value

    @staticmethod
    def _context(execution_id: str, attempt: int, field: str) -> str:
        return canonical([execution_id, attempt, field])

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0, fence INTEGER NOT NULL DEFAULT 0,
                active TEXT
            );
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
                fingerprint TEXT NOT NULL, attempt INTEGER NOT NULL,
                state TEXT NOT NULL, owner TEXT NOT NULL, fence INTEGER NOT NULL,
                expires REAL NOT NULL, base INTEGER NOT NULL, create_token TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
                task TEXT NOT NULL REFERENCES tasks(id), revision INTEGER NOT NULL,
                execution TEXT NOT NULL, attempt INTEGER NOT NULL, parent INTEGER NOT NULL,
                token TEXT NOT NULL UNIQUE, manifest TEXT NOT NULL,
                PRIMARY KEY(task, revision), UNIQUE(execution, attempt)
            );
            CREATE TABLE IF NOT EXISTS attempt_history (
                execution TEXT NOT NULL, attempt INTEGER NOT NULL, snapshot TEXT NOT NULL,
                PRIMARY KEY(execution, attempt)
            );
        """)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def begin(self, execution: Execution, owner: str, now: float, ttl: float) -> Lease:
        identifier(owner)
        require(math.isfinite(now) and math.isfinite(ttl) and ttl > 0 and math.isfinite(now + ttl))
        task = execution.task
        with self.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO tasks(id,fingerprint) VALUES (?,?)", (task.task_id, digest(asdict(task)))
            )
            row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task.task_id,)).fetchone()
            require(row["fingerprint"] == digest(asdict(task)), Code.DENIED)
            # Expiry does NOT prove the previous worker/VM has stopped.
            require(row["active"] is None, Code.CONFLICT)
            old = self.db.execute("SELECT * FROM executions WHERE id=?", (execution.execution_id,)).fetchone()
            if old is not None:
                require(old["fingerprint"] == digest(asdict(execution)), Code.CONFLICT)
                require(old["state"] in {"failed", "cancelled"}, Code.CONFLICT)
                self.db.execute(
                    "INSERT INTO attempt_history VALUES (?,?,?)",
                    (
                        execution.execution_id,
                        old["attempt"],
                        self._protect(
                            canonical(dict(old)), self._context(execution.execution_id, old["attempt"], "history")
                        ),
                    ),
                )
            attempt = old["attempt"] + 1 if old is not None else 1
            fence = row["fence"] + 1
            token = uuid4().hex
            self.db.execute(
                """INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET attempt=excluded.attempt,state=excluded.state,
                owner=excluded.owner,fence=excluded.fence,expires=excluded.expires,
                base=excluded.base,create_token=excluded.create_token""",
                (
                    execution.execution_id,
                    task.task_id,
                    digest(asdict(execution)),
                    attempt,
                    "preparing",
                    self._protect(owner, self._context(execution.execution_id, attempt, "owner")),
                    fence,
                    now + ttl,
                    row["revision"],
                    self._protect(token, self._context(execution.execution_id, attempt, "create_token")),
                ),
            )
            self.db.execute(
                "UPDATE tasks SET fence=?,active=? WHERE id=?", (fence, execution.execution_id, task.task_id)
            )
            return Lease(execution.execution_id, attempt, owner, fence, now + ttl, row["revision"], token)

    def _current(self, lease: Lease, now: float | None = None) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT e.*,t.active,t.fence AS current_fence FROM executions e JOIN tasks t ON e.task=t.id WHERE e.id=?",
            (lease.execution_id,),
        ).fetchone()
        require(row is not None, Code.CONFLICT)
        require(
            (
                row["attempt"],
                self._reveal(row["owner"], self._context(lease.execution_id, row["attempt"], "owner")),
                row["fence"],
                row["base"],
                self._reveal(row["create_token"], self._context(lease.execution_id, row["attempt"], "create_token")),
            )
            == (lease.attempt, lease.owner, lease.fencing, lease.base_revision, lease.create_token),
            Code.CONFLICT,
        )
        require(row["active"] == lease.execution_id and row["current_fence"] == lease.fencing, Code.CONFLICT)
        if now is not None:
            require(math.isfinite(now) and now < row["expires"], Code.CONFLICT)
        return row

    def transition(self, lease: Lease, state: str, now: float) -> None:
        with self.transaction():
            row = self._current(lease, now)
            require(state in TRANSITIONS.get(row["state"], set()), Code.CONFLICT)
            self.db.execute("UPDATE executions SET state=? WHERE id=?", (state, lease.execution_id))
            # Even failed attempts retain ownership until provider stop is confirmed.

    def renew(self, lease: Lease, now: float, ttl: float) -> Lease:
        require(math.isfinite(ttl) and ttl > 0 and math.isfinite(now + ttl))
        with self.transaction():
            row = self._current(lease, now)
            require(row["state"] in TRANSITIONS and row["state"] != "reconciling", Code.CONFLICT)
            expiry = max(row["expires"], now + ttl)
            self.db.execute("UPDATE executions SET expires=? WHERE id=?", (expiry, lease.execution_id))
            return Lease(
                lease.execution_id,
                lease.attempt,
                lease.owner,
                lease.fencing,
                expiry,
                lease.base_revision,
                lease.create_token,
            )

    def reconcile_stopped(self, lease: Lease, *, confirmed: bool, cancelled: bool = False) -> None:
        """Trusted reconciler only: caller must obtain provider cessation evidence."""
        require(confirmed, Code.UNKNOWN)
        with self.transaction():
            row = self._current(lease)
            require(row["state"] != "succeeded", Code.CONFLICT)
            self.db.execute(
                "UPDATE executions SET state=? WHERE id=?", ("cancelled" if cancelled else "failed", lease.execution_id)
            )
            self.db.execute("UPDATE tasks SET active=NULL WHERE id=?", (row["task"],))

    def commit(self, lease: Lease, manifest: OutputManifest, token: str, now: float) -> WorkspaceRevision:
        """Internal ledger operation; authorized service verifies durable bytes first."""
        identifier(token)
        payload = canonical(asdict(manifest))
        with self.transaction():
            stored_token = self._protect(token, "commit-token-index")
            old = self.db.execute("SELECT * FROM revisions WHERE token=?", (stored_token,)).fetchone()
            if old is not None:
                owner = self.db.execute("SELECT * FROM executions WHERE id=?", (lease.execution_id,)).fetchone()
                require(owner is not None, Code.CONFLICT)
                require(
                    (
                        owner["attempt"],
                        self._reveal(owner["owner"], self._context(lease.execution_id, owner["attempt"], "owner")),
                        owner["fence"],
                        owner["base"],
                        self._reveal(
                            owner["create_token"], self._context(lease.execution_id, owner["attempt"], "create_token")
                        ),
                    )
                    == (lease.attempt, lease.owner, lease.fencing, lease.base_revision, lease.create_token),
                    Code.CONFLICT,
                )
                require(
                    (
                        old["execution"],
                        old["attempt"],
                        self._reveal(old["manifest"], self._context(old["execution"], old["attempt"], "manifest")),
                    )
                    == (lease.execution_id, lease.attempt, payload),
                    Code.CONFLICT,
                )
                return self._revision(old)
            row = self._current(lease, now)
            require(row["state"] == "committing", Code.CONFLICT)
            task = self.db.execute("SELECT * FROM tasks WHERE id=?", (row["task"],)).fetchone()
            require(task["revision"] == lease.base_revision, Code.CONFLICT)
            revision = task["revision"] + 1
            self.db.execute(
                "INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
                (
                    row["task"],
                    revision,
                    lease.execution_id,
                    lease.attempt,
                    lease.base_revision,
                    stored_token,
                    self._protect(payload, self._context(lease.execution_id, lease.attempt, "manifest")),
                ),
            )
            self.db.execute("UPDATE tasks SET revision=?,active=NULL WHERE id=?", (revision, row["task"]))
            self.db.execute("UPDATE executions SET state='succeeded' WHERE id=?", (lease.execution_id,))
            return WorkspaceRevision(
                row["task"], revision, lease.base_revision, lease.execution_id, lease.attempt, manifest, token
            )

    def _revision(self, row: sqlite3.Row) -> WorkspaceRevision:
        value = json.loads(self._reveal(row["manifest"], self._context(row["execution"], row["attempt"], "manifest")))
        manifest = OutputManifest(tuple(FileEntry(**item) for item in value["files"]), value["schema_version"])
        return WorkspaceRevision(
            row["task"],
            row["revision"],
            row["parent"],
            row["execution"],
            row["attempt"],
            manifest,
            self._reveal(row["token"], "commit-token-index"),
        )

    def state(self, execution_id: str) -> str | None:
        row = self.db.execute("SELECT state FROM executions WHERE id=?", (execution_id,)).fetchone()
        return row["state"] if row is not None else None

    def verify_execution(self, execution: Execution, lease: Lease) -> None:
        row = self.db.execute("SELECT fingerprint FROM executions WHERE id=?", (lease.execution_id,)).fetchone()
        require(execution.execution_id == lease.execution_id and row is not None, Code.DENIED)
        require(row["fingerprint"] == digest(asdict(execution)), Code.DENIED)
