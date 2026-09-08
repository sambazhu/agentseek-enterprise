"""Bounded local SDK worker I/O. Not a sandbox for untrusted host code.

Command and environment come from trusted Broker composition only. Requests go
over stdin, never argv. Timeout/exit/output failure has unknown remote outcome.
Remote create/execute/delete is NEVER retried here.
"""

from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress

from .models import Code, ContractError, require

MAX_REQUEST = 65536
MAX_OUTPUT = 1024 * 1024


def run_worker(command: Sequence[str], request: bytes, *, budget: float, environment: Mapping[str, str]) -> bytes:
    """POSIX trusted-worker call. No shell, inherited secrets, or stderr capture.

    A single deadline covers nonblocking stdin/stdout and exit waiting after
    process launch. Cleanup may take an additional second. OS process creation
    itself is not forcibly interruptible by this function.
    """
    require(type(request) is bytes and len(request) <= MAX_REQUEST)
    require(math.isfinite(budget) and 0 < budget <= 600)
    require(bool(command) and all(isinstance(part, str) and part for part in command))
    deadline = time.monotonic() + budget
    process = subprocess.Popen(  # noqa: S603 -- command is trusted service composition, never a request field
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(environment),
        start_new_session=True,
        close_fds=True,
    )
    try:
        return _exchange(process, request, deadline)
    except Exception:
        raise ContractError(Code.UNKNOWN) from None
    finally:
        # Kill the entire local worker process group, including children holding
        # pipe handles after the worker exited. Does not prove a remote VM stopped.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            raise ContractError(Code.UNKNOWN) from None
        finally:
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()


def _exchange(process: subprocess.Popen, request: bytes, deadline: float) -> bytes:
    if process.stdin is None or process.stdout is None:
        raise ContractError(Code.UNKNOWN)
    output = bytearray()
    offset = 0
    os.set_blocking(process.stdin.fileno(), False)
    os.set_blocking(process.stdout.fileno(), False)
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if request:
            selector.register(process.stdin, selectors.EVENT_WRITE)
        else:
            process.stdin.close()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            require(remaining > 0, Code.UNKNOWN)
            for key, _events in selector.select(timeout=remaining):
                if key.fileobj is process.stdin:
                    try:
                        sent = os.write(key.fd, request[offset : offset + 4096])
                    except BlockingIOError:
                        continue
                    offset += sent
                    if offset == len(request):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                else:
                    try:
                        chunk = os.read(key.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(process.stdout)
                    else:
                        output.extend(chunk)
                        require(len(output) <= MAX_OUTPUT, Code.UNKNOWN)
        remaining = deadline - time.monotonic()
        require(remaining > 0, Code.UNKNOWN)
        require(process.wait(timeout=remaining) == 0, Code.UNKNOWN)
    return bytes(output)
