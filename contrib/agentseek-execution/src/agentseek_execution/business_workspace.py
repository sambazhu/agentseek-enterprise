"""Immutable, owner-scoped CSV workspace outside the guest filesystem.

This pilot exports one fixed file. It does not fabricate an M3 WorkspaceRevision,
mount a host workspace into the guest, or send a chat attachment.
"""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .csv_business import MAX_RESULT_BYTES
from .file_io import read_regular


def _private_directory(path):
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("canonical directory required")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("private workspace required")


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _immutable(path, data):
    _private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
    _sync_directory(path.parent)
    if read_regular(path.parent, path.name, max_bytes=len(data)) != data:
        raise ValueError("immutable workspace conflict")


class CsvWorkspace:
    def __init__(self, directory: Path):
        _private_directory(directory)
        self.directory = directory

    def _target(self, request, attempt, *, create=False):
        expected = hashlib.sha256(json.dumps([request.owner_id, request.request_id]).encode()).hexdigest()
        if attempt != expected:
            raise ValueError("attempt mismatch")
        owner = hashlib.sha256(request.owner_id.encode()).hexdigest()
        parent = self.directory
        for name in (owner, attempt):
            _private_directory(parent)
            child = parent / name
            if create:
                try:
                    child.mkdir(mode=0o700)
                    _sync_directory(parent)
                except FileExistsError:
                    pass
            _private_directory(child)
            parent = child
        return parent

    def publish(self, request, attempt, artifact, data):
        if type(data) is not bytes or not 0 < len(data) <= MAX_RESULT_BYTES or not data.startswith(b"group,total\n"):
            raise ValueError("invalid CSV")
        data.decode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        expected = "artifact_" + hashlib.sha256((attempt + digest).encode()).hexdigest()
        if artifact != expected:
            raise ValueError("artifact mismatch")
        target = self._target(request, attempt, create=True)
        metadata = dict(schema=1, owner_id=request.owner_id, request_id=request.request_id, attempt=attempt,
                        file_id=artifact, filename="summary.csv", sha256=digest, size_bytes=len(data))
        _immutable(target / "summary.csv", data)
        _immutable(target / "manifest.json", json.dumps(metadata, sort_keys=True).encode())
        return self.reference(request, attempt, artifact)

    def reference(self, request, attempt, artifact):
        target = self._target(request, attempt)
        metadata = json.loads(read_regular(target, "manifest.json", max_bytes=32768))
        if (set(metadata) != {"schema", "owner_id", "request_id", "attempt", "file_id", "filename", "sha256", "size_bytes"}
                or metadata["schema"] != 1 or metadata["owner_id"] != request.owner_id
                or metadata["request_id"] != request.request_id or metadata["attempt"] != attempt
                or metadata["file_id"] != artifact or metadata["filename"] != "summary.csv"):
            raise ValueError("workspace binding mismatch")
        data = read_regular(target, "summary.csv", max_bytes=MAX_RESULT_BYTES)
        digest = hashlib.sha256(data).hexdigest()
        if (digest != metadata["sha256"] or len(data) != metadata["size_bytes"]
                or "artifact_" + hashlib.sha256((attempt + digest).encode()).hexdigest() != artifact):
            raise ValueError("workspace integrity failure")
        return {key: metadata[key] for key in ("file_id", "filename", "sha256", "size_bytes")}
