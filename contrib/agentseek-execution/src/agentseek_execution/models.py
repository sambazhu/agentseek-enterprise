"""Immutable, versioned contract values; identifiers are not authorization."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from enum import Enum


class Code(str, Enum):
    INVALID = "invalid_contract"
    DENIED = "authorization_denied"
    CONFLICT = "write_conflict"
    UNKNOWN = "outcome_unknown"
    FAILED = "execution_failed"


class ContractError(Exception):
    def __init__(self, code: Code):
        self.code = code
        super().__init__(code.value)


def require(condition: bool, code: Code = Code.INVALID) -> None:
    if not condition:
        raise ContractError(code)


def identifier(value: str) -> None:
    require(isinstance(value, str) and 0 < len(value) <= 256 and all(ord(c) >= 32 for c in value))


def relative_path(value: str) -> None:
    require(isinstance(value, str) and 0 < len(value) <= 1024)
    require(not any(c in value for c in ("\\", "\x00", ":")))
    require(all(part not in ("", ".", "..") for part in value.split("/")))
    require(all(ord(c) >= 32 for c in value))


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


@dataclass(frozen=True)
class Scope:
    tenant: str
    digital_employee: str
    conversation: str
    requester: str

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            identifier(value)


@dataclass(frozen=True)
class Task:
    task_id: str
    scope: Scope
    work_id: str | None = None
    phase: str | None = None

    def __post_init__(self) -> None:
        identifier(self.task_id)
        require(isinstance(self.scope, Scope))
        require((self.work_id is None) == (self.phase is None))
        if self.work_id is not None:
            identifier(self.work_id)
            identifier(self.phase or "")


@dataclass(frozen=True)
class FileEntry:
    file_id: str
    version: str
    path: str
    sha256: str
    size: int
    media_type: str

    def __post_init__(self) -> None:
        identifier(self.file_id)
        identifier(self.version)
        relative_path(self.path)
        require(isinstance(self.sha256, str) and re.fullmatch(r"[0-9a-f]{64}", self.sha256) is not None)
        require(type(self.size) is int and self.size >= 0)
        identifier(self.media_type)


@dataclass(frozen=True)
class InputFile:
    entry: FileEntry
    grant_id: str

    def __post_init__(self) -> None:
        require(isinstance(self.entry, FileEntry))
        identifier(self.grant_id)


def unique_paths(entries: tuple[FileEntry, ...]) -> None:
    paths = [entry.path for entry in entries]
    require(len(paths) == len(set(paths)))
    require(not any(path.startswith(other + "/") for path in paths for other in paths if path != other))


@dataclass(frozen=True)
class InputManifest:
    files: tuple[InputFile, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        require(type(self.schema_version) is int and self.schema_version == 1)
        require(isinstance(self.files, tuple) and all(isinstance(f, InputFile) for f in self.files))
        unique_paths(tuple(f.entry for f in self.files))


@dataclass(frozen=True)
class OutputManifest:
    files: tuple[FileEntry, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        require(type(self.schema_version) is int and self.schema_version == 1)
        require(isinstance(self.files, tuple) and all(isinstance(f, FileEntry) for f in self.files))
        unique_paths(self.files)

    def fingerprint(self) -> str:
        return hashlib.sha256(canonical(asdict(self)).encode()).hexdigest()


@dataclass(frozen=True)
class Execution:
    execution_id: str
    task: Task
    inputs: InputManifest
    template_version: str
    policy_version: str

    def __post_init__(self) -> None:
        identifier(self.execution_id)
        identifier(self.template_version)
        identifier(self.policy_version)
        require(isinstance(self.task, Task) and isinstance(self.inputs, InputManifest))


@dataclass(frozen=True)
class Lease:
    execution_id: str
    attempt: int
    owner: str
    fencing: int
    expires_at: float
    base_revision: int
    create_token: str

    def __post_init__(self) -> None:
        for value in (self.execution_id, self.owner, self.create_token):
            identifier(value)
        require(type(self.attempt) is int and self.attempt > 0)
        require(type(self.fencing) is int and self.fencing > 0)
        require(type(self.base_revision) is int and self.base_revision >= 0)
        require(type(self.expires_at) in (int, float) and math.isfinite(self.expires_at))


@dataclass(frozen=True)
class WorkspaceRevision:
    task_id: str
    revision: int
    parent: int
    execution_id: str
    attempt: int
    manifest: OutputManifest
    commit_token: str

    def __post_init__(self) -> None:
        for value in (self.task_id, self.execution_id, self.commit_token):
            identifier(value)
        require(type(self.revision) is int and self.revision > 0)
        require(type(self.parent) is int and self.parent == self.revision - 1)
        require(type(self.attempt) is int and self.attempt > 0)
        require(isinstance(self.manifest, OutputManifest))


@dataclass(frozen=True)
class ExecutionOutput:
    """Registered candidate, never a Work approval or delivery receipt."""

    workspace: WorkspaceRevision
    entry: FileEntry

    def __post_init__(self) -> None:
        require(self.entry in self.workspace.manifest.files)
