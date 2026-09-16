"""Durable exact-target history for UNKNOWN observation, never release authority.

An initial empty list remains unresolved. A later empty list after a recorded
exact identity is distinguishable, but still does not prove absence of late
creates or authorize a new batch. No create, delete, token recovery or cleanup.
The trusted caller supplies one private tracking directory per installation and
an outer watchdog. Same-owner deletion/rollback is outside this local journal.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from dataclasses import asdict, replace
from pathlib import Path

from .m3_create_reconciliation import observe_pending
from .m3_probe_process import _decode, config_bytes
from .models import Code, ContractError, canonical, require


def track_pending(reader, fence, binding, directory: Path):
    """Persist the first exact identity; never replace it with another guest."""
    require(directory.is_absolute() and directory.resolve() == directory, Code.DENIED)
    info = directory.stat()
    require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
    fence.verify_pending(binding)
    digest = hashlib.sha256(canonical(asdict(binding)).encode()).hexdigest()
    path = directory / "create.target"
    previous = _read(path, digest)
    observed = observe_pending(reader, fence, binding)
    require(0 <= time.monotonic() - observed.observed_mono <= 2, Code.DENIED)
    if previous is not None:
        if observed.sandbox_id is not None:
            require(observed.sandbox_id == previous, Code.DENIED)
        if observed.state == "empty_unresolved":
            observed = replace(observed, state="known_target_absent", sandbox_id=previous)
    elif observed.state in {"exact_running_observed", "exact_terminal_observed", "exact_unresolved"}:
        _write(directory, digest, observed.sandbox_id)
    fence.verify_pending(binding)
    if previous is not None or observed.sandbox_id is not None:
        require(_read(path, digest) == (previous if previous is not None else observed.sandbox_id), Code.DENIED)
    # fsync and final private-file checks consume freshness too. Keep any
    # durable target on failure, but never relabel an old observation as fresh.
    require(0 <= time.monotonic() - observed.observed_mono <= 2, Code.DENIED)
    return observed


def _read(path: Path, digest: str) -> str | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    record = _decode(config_bytes(path))
    require(type(record) is dict and set(record) == {"schema", "binding_sha256", "sandbox_id"}, Code.DENIED)
    require(type(record["schema"]) is int and record["schema"] == 1 and record["binding_sha256"] == digest, Code.DENIED)
    sid = record["sandbox_id"]
    require(type(sid) is str and re.fullmatch(r"[A-Za-z0-9-]{1,256}", sid) is not None, Code.DENIED)
    return sid


def _write(directory: Path, digest: str, sid: str) -> None:
    raw = canonical({"schema": 1, "binding_sha256": digest, "sandbox_id": sid}).encode()
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
        try:
            target = os.open("create.target", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        except FileExistsError:
            # Another observer won the race: verify, never overwrite.
            require(_read(directory / "create.target", digest) == sid, Code.DENIED)
            return
        try:
            offset = 0
            while offset < len(raw):
                count = os.write(target, raw[offset:])
                require(count > 0, Code.UNKNOWN)
                offset += count
            os.fsync(target)
            os.fsync(fd)
        finally:
            os.close(target)
    except Exception:
        raise ContractError(Code.UNKNOWN) from None
    finally:
        os.close(fd)
