"""Linux trusted single-thread host composition; no operator/config entry point.

Forks the already constructed creating worker so its real admission callback
runs inside the deadline too. Not suitable for threaded Gateway processes.
The child inherits trusted memory (including credentials), not a security sandbox.
No retries, remote cleanup, or claim that killing a process stops a guest.
"""

from __future__ import annotations

import math
import os
import selectors
import signal
import sys
import threading
import time
from contextlib import suppress
from dataclasses import asdict, replace

from .m3_create_receipt import CreateBinding
from .m3_create_worker import CreateWorker
from .m3_probe_process import _decode
from .models import Code, ContractError, canonical, require

MAX_CREATE_SECONDS = 10


def create_isolated(worker: CreateWorker, request: bytes, *, deadline: float) -> CreateBinding:  # noqa: C901 -- one process lifecycle with mandatory cleanup
    """Bound complete create to at most 10 seconds plus up to 1s child reaping.

    Trusted parent supplies same-host monotonic deadline, not user request JSON.
    All post-fork uncertainty stays UNKNOWN. Reservation semantics remain in
    CreateWorker: a child killed before reserve may not have claimed a slot;
    an existing slot is never removed, even when the result cannot be recovered.
    """
    require(sys.platform == "linux" and threading.active_count() == 1, Code.DENIED)
    require(type(worker) is CreateWorker and type(request) is bytes and len(request) <= 65536, Code.DENIED)
    require(type(deadline) in {int, float} and math.isfinite(deadline), Code.DENIED)
    remaining = deadline - time.monotonic()
    require(remaining >= 11, Code.DENIED)
    stop = min(deadline - 1, time.monotonic() + MAX_CREATE_SECONDS)
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if pid == 0:
        _child(worker, request, read_fd, write_fd)
        os._exit(2)
    os.close(write_fd)
    reaped = False
    try:
        raw = bytearray()
        os.set_blocking(read_fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(read_fd, selectors.EVENT_READ)
            while selector.get_map():
                require(stop > time.monotonic(), Code.UNKNOWN)
                for key, _ in selector.select(max(0, stop - time.monotonic())):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(read_fd)
                    else:
                        require(len(raw) + len(chunk) <= 8192, Code.UNKNOWN)
                        raw.extend(chunk)
        # Pipe EOF does not mean a clean process exit.
        while True:
            require(stop > time.monotonic(), Code.UNKNOWN)
            observed, status = os.waitpid(pid, os.WNOHANG)
            if observed:
                reaped = True
                require(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, Code.UNKNOWN)
                break
            time.sleep(0.005)
        binding = CreateBinding(**_decode(bytes(raw)))
        require(replace(binding, intent_sha256=worker._binding.intent_sha256) == worker._binding, Code.UNKNOWN)
    except Exception:
        raise ContractError(Code.UNKNOWN) from None
    else:
        return binding
    finally:
        os.close(read_fd)
        # Child setsid before any creating work. PID kill also handles a child
        # interrupted before setsid; group kill covers descendants after it.
        with suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        if not reaped:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            cleanup = time.monotonic() + 1
            while os.waitpid(pid, os.WNOHANG)[0] == 0:
                require(time.monotonic() < cleanup, Code.UNKNOWN)
                time.sleep(0.005)


def _child(worker: CreateWorker, request: bytes, read_fd: int, write_fd: int) -> None:
    try:
        os.setsid()
        os.close(read_fd)
        # Do not let trusted callbacks/libraries print tokens to inherited UI.
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        if null > 2 and null != write_fd:
            os.close(null)
        os.closerange(3, write_fd)
        os.closerange(write_fd + 1, os.sysconf("SC_OPEN_MAX"))
        raw = canonical(asdict(worker.create(request))).encode()
        require(len(raw) <= 8192, Code.UNKNOWN)
        offset = 0
        while offset < len(raw):
            count = os.write(write_fd, raw[offset:])
            require(count > 0, Code.UNKNOWN)
            offset += count
        os.close(write_fd)
        os._exit(0)
    except BaseException:
        os._exit(2)
