"""Offline orchestration seam; all dependencies are supplied by trusted code."""

from __future__ import annotations

import hashlib
from typing import Protocol

from .authorization import Authority, authorize
from .ledger import Ledger
from .models import Code, ContractError, Execution, FileEntry, Lease, OutputManifest, Scope, WorkspaceRevision, require


class Provider(Protocol):
    def create(self, token: str) -> None: ...

    def stopped(self, token: str) -> bool: ...

    def cancel(self, token: str) -> None: ...


class ContentStore(Protocol):
    def put(self, entry: FileEntry, content: bytes) -> None: ...

    def verify(self, entry: FileEntry) -> bool: ...


class Service:
    def __init__(
        self,
        ledger: Ledger,
        authority: Authority,
        provider: Provider,
        content: ContentStore,
        *,
        max_bytes: int = 16 * 1024 * 1024,
    ):
        require(type(max_bytes) is int and max_bytes >= 0)
        self.ledger = ledger
        self.authority = authority
        self.provider = provider
        self.content = content
        self.max_bytes = max_bytes

    def start(self, actor: Scope, execution: Execution, owner: str, now: float, ttl: float) -> Lease:
        authorize(self.authority, actor, execution, now)
        lease = self.ledger.begin(execution, owner, now, ttl)
        try:
            self.provider.create(lease.create_token)
        except Exception as exc:
            self.ledger.transition(lease, "reconciling", now)
            raise ContractError(Code.UNKNOWN) from exc
        self.ledger.transition(lease, "running", now)
        return lease

    def submit(
        self,
        actor: Scope,
        execution: Execution,
        lease: Lease,
        manifest: OutputManifest,
        contents: dict[str, bytes],
        token: str,
        now: float,
    ) -> WorkspaceRevision:
        authorize(self.authority, actor, execution, now)
        self.ledger.verify_execution(execution, lease)
        require(sum(f.size for f in manifest.files) <= self.max_bytes)
        require(set(contents) == {f.path for f in manifest.files})
        for entry in manifest.files:
            value = contents[entry.path]
            require(type(value) is bytes and len(value) == entry.size)
            require(hashlib.sha256(value).hexdigest() == entry.sha256)
        state = self.ledger.state(lease.execution_id)
        if state == "succeeded":
            # Ledger handles same-token/same-payload response-loss replay.
            return self.ledger.commit(lease, manifest, token, now)
        require(self.provider.stopped(lease.create_token), Code.UNKNOWN)
        # Partial exports can be resumed; content writes are immutable/idempotent.
        for source, target in (("running", "exporting"), ("exporting", "validating")):
            if self.ledger.state(lease.execution_id) == source:
                self.ledger.transition(lease, target, now)
        require(self.ledger.state(lease.execution_id) in {"validating", "committing"}, Code.CONFLICT)
        for entry in manifest.files:
            self.content.put(entry, contents[entry.path])
            require(self.content.verify(entry), Code.UNKNOWN)
        if self.ledger.state(lease.execution_id) == "validating":
            self.ledger.transition(lease, "committing", now)
        return self.ledger.commit(lease, manifest, token, now)

    def cancel(self, actor: Scope, execution: Execution, lease: Lease, now: float) -> None:
        authorize(self.authority, actor, execution, now)
        self.ledger.verify_execution(execution, lease)
        self.ledger.transition(lease, "cancel_requested", now)
        try:
            self.provider.cancel(lease.create_token)
            require(self.provider.stopped(lease.create_token), Code.UNKNOWN)
        except Exception as exc:
            self.ledger.transition(lease, "reconciling", now)
            raise ContractError(Code.UNKNOWN) from exc
        self.ledger.reconcile_stopped(lease, confirmed=True, cancelled=True)
