"""Ports implemented by trusted server adapters, never by request bodies."""

from typing import Protocol

from .models import Code, Execution, FileEntry, Scope, Task, require


class Authority(Protocol):
    def task_allowed(self, actor: Scope, task: Task) -> bool: ...

    def work_allowed(self, actor: Scope, work_id: str, phase: str) -> bool: ...

    def file_allowed(self, actor: Scope, entry: FileEntry, grant_id: str, now: float) -> bool: ...


def authorize(authority: Authority, actor: Scope, execution: Execution, now: float) -> None:
    task = execution.task
    require(actor == task.scope, Code.DENIED)
    require(authority.task_allowed(actor, task), Code.DENIED)
    if task.work_id is not None:
        require(authority.work_allowed(actor, task.work_id, task.phase or ""), Code.DENIED)
    for item in execution.inputs.files:
        require(authority.file_allowed(actor, item.entry, item.grant_id, now), Code.DENIED)
