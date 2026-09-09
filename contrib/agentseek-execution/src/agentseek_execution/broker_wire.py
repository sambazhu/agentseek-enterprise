"""Bounded local JSON framing plus the legacy read-only discovery dispatcher.

No listener/daemon starts at import. broker_daemon supplies the separate
Lifecycle dispatcher; Dispatcher itself remains a read-only library interface.
"""

from __future__ import annotations

import json
import math
import os
import socket
import stat
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event

from .broker_access import AccessGate, Operation
from .models import Code, ContractError, canonical, require

MAX_REQUEST = 65536
MAX_RESPONSE = 65536


def serve_unix(
    path: Path,
    dispatch: Callable[[dict[str, object]], dict[str, object]],
    stop: Event,
    *,
    request_budget: float = 2.0,
    tick: Callable[[], None] | None = None,
    on_bound: Callable[[int, int], None] | None = None,
) -> None:
    """Single local discovery listener; refuses existing socket or unsafe directory.

    No stale socket removal on startup. The deployment operator must reconcile
    stale runtime paths. Parent must be provisioned before starting the service.
    """
    parent = path.parent
    require(path.is_absolute() and parent.resolve() == parent, Code.DENIED)
    info = parent.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), Code.DENIED)
    require(stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
    require(not path.exists() and not path.is_symlink(), Code.CONFLICT)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity = None
    try:
        listener.bind(str(path))
        created = path.lstat()
        identity = (created.st_dev, created.st_ino)
        path.chmod(0o600)
        if on_bound is not None:
            on_bound(*identity)
        listener.listen(8)
        listener.settimeout(0.1)
        next_tick = 0.0
        while not stop.is_set():
            if tick is not None and time.monotonic() >= next_tick:
                tick()
                next_tick = time.monotonic() + 1
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                serve_connection(connection, dispatch, budget=request_budget)
    finally:
        listener.close()
        if identity is not None:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == identity and stat.S_ISSOCK(current.st_mode):
                    path.unlink()
            except FileNotFoundError:
                pass


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


class Dispatcher:
    def __init__(self, access: AccessGate):
        self.access = access

    def __call__(self, request: dict[str, object]) -> dict[str, object]:
        require(type(request) is dict)
        key = request.get("key")
        if not isinstance(key, str):
            raise ContractError(Code.DENIED)
        # Authenticate even unknown methods; no unauthenticated discovery surface.
        self.access.credentials.authenticate(key)
        method = request.get("method")
        if method == "list":
            require(set(request) == {"key", "method"})
            return {"resource_ids": self.access.list_ids(key)}
        if method == "inspect":
            require(set(request) == {"key", "method", "resource_id"})
            identifier = request.get("resource_id")
            if not isinstance(identifier, str):
                raise ContractError(Code.INVALID)
            resource = self.access.resource(key, identifier, Operation.READ)
            return {"resource_id": resource.resource_id}
        raise ContractError(Code.DENIED)


def serve_connection(
    connection: socket.socket, dispatch: Callable[[dict[str, object]], dict[str, object]], *, budget: float = 2.0
) -> None:
    """One request per connection; caller owns/ closes connection afterwards.

    Total budget bounds socket I/O, NOT arbitrary handler runtime. Real Cube
    commands must run through a separate bounded worker, not this inline hook.
    """
    require(math.isfinite(budget) and budget > 0)
    deadline = time.monotonic() + budget
    data = bytearray()
    try:
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            require(remaining > 0, Code.UNKNOWN)
            connection.settimeout(remaining)
            block = connection.recv(min(4096, MAX_REQUEST + 1 - len(data)))
            require(bool(block))
            data.extend(block)
            require(len(data) <= MAX_REQUEST)
        line, tail = data.split(b"\n", 1)
        require(not tail)
        request = json.loads(line, object_pairs_hook=_object)
        require(type(request) is dict)
        result = dispatch(request)
        response = (canonical({"ok": True, "result": result}) + "\n").encode()
        require(len(response) <= MAX_RESPONSE)
    except Exception:
        # Do not serialize repr, input values, tokens, provider exceptions or traces.
        response = b'{"ok":false,"error":"request_rejected"}\n'
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            connection.settimeout(remaining)
            connection.sendall(response)
        except OSError:
            pass
