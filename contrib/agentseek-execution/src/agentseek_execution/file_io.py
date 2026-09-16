"""Bounded descriptor-relative file reads for trusted M3 adapters.

This is not a remote filesystem collector or a sandbox. A caller must separately
quiesce the guest writers before exporting. No path or OS error is returned to
the public Broker, and this module is deliberately not registered with M2.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .models import Code, ContractError, FileEntry, relative_path, require


@dataclass(frozen=True)
class FileLimits:
    max_file_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024
    max_files: int = 32
    max_depth: int = 8

    def __post_init__(self) -> None:
        for value in (self.max_file_bytes, self.max_total_bytes, self.max_files, self.max_depth):
            require(type(value) is int and value > 0)
        require(self.max_file_bytes <= self.max_total_bytes)

    def check(self, entries: tuple[FileEntry, ...]) -> None:
        require(len(entries) <= self.max_files)
        require(sum(entry.size for entry in entries) <= self.max_total_bytes)
        for entry in entries:
            relative_path(entry.path)
            require(entry.size <= self.max_file_bytes and len(entry.path.split("/")) <= self.max_depth)


def verify_bytes(entry: FileEntry, content: bytes) -> None:
    require(type(content) is bytes and len(content) == entry.size)
    require(hashlib.sha256(content).hexdigest() == entry.sha256)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_regular(root: Path, path: str, *, max_bytes: int) -> bytes:
    """Read one regular, single-link file without following any path component.

    The root must be an absolute canonical directory selected by trusted code.
    The descriptor walk prevents symlink traversal, not same-UID/root tampering.
    Before/after stat rejects observed mutation; it does not prove quiescence.
    """
    relative_path(path)
    require(type(max_bytes) is int and max_bytes >= 0)
    require(root.is_absolute() and root.resolve() == root, Code.DENIED)
    descriptors: list[int] = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        parts = path.split("/")
        for part in parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        # O_NONBLOCK avoids hanging on a FIFO before fstat can reject it.
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        descriptors.append(fd)
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, Code.DENIED)
        require(before.st_size <= max_bytes)
        chunks: list[bytes] = []
        size = 0
        while size <= max_bytes:
            chunk = os.read(fd, min(64 * 1024, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        require(size <= max_bytes)
        after = os.fstat(fd)
        require(_identity(before) == _identity(after) and size == after.st_size, Code.UNKNOWN)
        # Detect replacement of the leaf while reading its original descriptor.
        linked = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        require(_identity(after) == _identity(linked), Code.UNKNOWN)
        return b"".join(chunks)
    except OSError:
        raise ContractError(Code.DENIED) from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
