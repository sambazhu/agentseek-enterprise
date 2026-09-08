"""Test doubles only. These do not execute scripts or confer production authority."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .models import Code, ContractError, FileEntry, Scope, Task, require


@dataclass
class FakeAuthority:
    tasks: set[Task] = field(default_factory=set)
    works: set[tuple[Scope, str, str]] = field(default_factory=set)
    grants: dict[str, tuple[Scope, FileEntry, float]] = field(default_factory=dict)

    def task_allowed(self, actor: Scope, task: Task) -> bool:
        return actor == task.scope and task in self.tasks

    def work_allowed(self, actor: Scope, work_id: str, phase: str) -> bool:
        return (actor, work_id, phase) in self.works

    def file_allowed(self, actor: Scope, entry: FileEntry, grant_id: str, now: float) -> bool:
        grant = self.grants.get(grant_id)
        return grant is not None and grant[:2] == (actor, entry) and now < grant[2]


@dataclass
class FakeProvider:
    created: set[str] = field(default_factory=set)
    finished: set[str] = field(default_factory=set)
    create_calls: int = 0
    lose_create_response: bool = False
    lose_cancel_response: bool = False

    def create(self, token: str) -> None:
        self.create_calls += 1
        self.created.add(token)
        if self.lose_create_response:
            raise ContractError(Code.UNKNOWN)

    def stopped(self, token: str) -> bool:
        return token in self.finished

    def cancel(self, token: str) -> None:
        if self.lose_cancel_response:
            raise ContractError(Code.UNKNOWN)
        self.finished.add(token)


@dataclass
class FakeContentStore:
    objects: dict[str, bytes] = field(default_factory=dict)
    lose_write_response: bool = False

    def put(self, entry: FileEntry, content: bytes) -> None:
        require(hashlib.sha256(content).hexdigest() == entry.sha256)
        require(entry.sha256 not in self.objects or self.objects[entry.sha256] == content, Code.CONFLICT)
        self.objects[entry.sha256] = content
        if self.lose_write_response:
            raise ContractError(Code.UNKNOWN)

    def verify(self, entry: FileEntry) -> bool:
        content = self.objects.get(entry.sha256)
        return (
            content is not None and len(content) == entry.size and hashlib.sha256(content).hexdigest() == entry.sha256
        )
