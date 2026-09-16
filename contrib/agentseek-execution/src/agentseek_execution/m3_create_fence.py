"""Persistent unresolved-create fence for one trusted installation.

The fixed filename covers all runs/tokens using this installed directory. Claim
before reserving the receipt intent or sending HTTP. Every claimed slot remains
pending, even on success, until the internal verified successor handoff replaces
it atomically and archives it. No empty release, reset, TTL, retry, CLI or automatic
directory provisioning is offered.
This conservative fence is not the batch's 2/4-create quota or a platform lock.
Root/same-UID deletion, directory substitution and rollback are outside its trust
boundary; the trusted installation must pin one directory for every creator.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from dataclasses import asdict, replace
from pathlib import Path

from .m3_create_receipt import CreateBinding
from .models import Code, ContractError, canonical, require


class CreateFence:
    def __init__(self, directory: Path):
        require(directory.is_absolute() and directory.resolve() == directory, Code.DENIED)
        self._directory = directory

    @staticmethod
    def _record(binding: CreateBinding) -> bytes:
        binding.validate()
        require(binding.intent_sha256 == "0" * 64, Code.DENIED)
        return canonical({
            "schema": 1,
            "state": "pending",
            "binding_sha256": hashlib.sha256(canonical(asdict(binding)).encode()).hexdigest(),
        }).encode()

    def verify_pending(self, binding: CreateBinding) -> None:
        """Read exact original binding; not release authority or receipt recovery."""
        expected = self._record(binding)
        try:
            directory = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                info = os.fstat(directory)
                require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
                fd = os.open("create.pending", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    info = os.fstat(fd)
                    require(
                        stat.S_ISREG(info.st_mode)
                        and info.st_uid == os.getuid()
                        and stat.S_IMODE(info.st_mode) == 0o600
                        and info.st_nlink == 1,
                        Code.DENIED,
                    )
                    require(os.read(fd, len(expected) + 1) == expected, Code.DENIED)
                finally:
                    os.close(fd)
            finally:
                os.close(directory)
        except Exception:
            raise ContractError(Code.DENIED) from None

    def claim(self, binding: CreateBinding) -> None:
        """At most one claimant; any failure is a no-send result.

        After O_EXCL, partial writes and failed fsync leave the fence in place.
        Only hashes are stored; this record does not replace the private plan,
        receipt intent or exact create response needed for reconciliation.
        Caller must bound all filesystem work with the creating-process watchdog.
        """
        raw = self._record(binding)
        try:
            directory = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                info = os.fstat(directory)
                require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd = os.open(
                    "create.pending", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
                )
                try:
                    offset = 0
                    while offset < len(raw):
                        written = os.write(fd, raw[offset:])
                        require(written > 0, Code.UNKNOWN)
                        offset += written
                    os.fsync(fd)
                    os.fsync(directory)
                finally:
                    os.close(fd)
            finally:
                os.close(directory)
        except Exception:
            raise ContractError(Code.UNKNOWN) from None

    def claim_successor(self, previous, vault, next_plan, approval_path, *, approval_digest,
                        sequence, collect_closeout):
        """Internal atomic handoff; not a send entry or quota reservation.

        The caller must reserve quota before calling and retain the outer process
        deadline. No retry after uncertainty. Archive the old fence without an
        empty interval; partial staging or archiving deliberately blocks retry.
        """
        from .m3_successor import check_successor

        old = replace(previous, intent_sha256="0" * 64)
        new = next_plan.binding(approval_digest)
        raw = self._record(new)
        suffix = hashlib.sha256(self._record(old)).hexdigest()
        try:
            directory = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                info = os.fstat(directory)
                require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.verify_pending(old)
                kwargs = {"approval_digest": approval_digest, "sequence": sequence, "collect_closeout": collect_closeout}
                check_successor(previous, vault, next_plan, approval_path, **kwargs)
                stage = f"create.next-{suffix}"
                fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    offset = 0
                    while offset < len(raw):
                        written = os.write(fd, raw[offset:])
                        require(written > 0, Code.UNKNOWN)
                        offset += written
                    os.fsync(fd)
                    os.fsync(directory)
                finally:
                    os.close(fd)
                # Recheck live closeout/approval after staging, before transition.
                check_successor(previous, vault, next_plan, approval_path, **kwargs)
                self.verify_pending(old)
                os.link("create.pending", f"create.closed-{suffix}",
                        src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                os.fsync(directory)
                os.replace(stage, "create.pending", src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            raise ContractError(Code.UNKNOWN) from None
        return new
