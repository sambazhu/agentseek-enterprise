"""Parent-side scalar diagnostics. Never capture worker stderr or payloads."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import logging
import os
import threading
import time
import uuid

from .models import ContractError

_scope = ContextVar("execution_diagnostic", default=None)
STAGES = frozenset({"provider_create", "provider_run", "provider_destroy", "lifecycle",
    "intent_save", "result_save", "launcher", "create_worker", "attach_worker",
    "session_reconstruct", "closeout", "worker_spawn", "worker_exchange", "worker_cleanup"})
EVENTS = frozenset({"started", "complete", "failed", "skipped", "spawned", "kill_sent",
                    "group_absent", "reaped"})


def error_kind(exc):
    if exc is None:
        return "none"
    if isinstance(exc, ContractError):
        return "contract"
    name = type(exc).__name__
    return name if name in {"TimeoutError", "TimeoutExpired", "PermissionError", "ProcessLookupError",
        "FileNotFoundError", "OSError", "ValueError", "RuntimeError", "CancelledError",
        "KeyboardInterrupt", "SystemExit"} else "other"


def emit(stage, event, *, started=None, exc=None, worker_id="", child_pid=0, returncode=None):
    # Diagnostics cannot replace the original outcome, including logging failures.
    try:
        if stage not in STAGES or event not in EVENTS:
            return
        scope = _scope.get() or ("unbound", "unbound")
        value = dict(scope_id=scope[0], request_sha256=scope[1], stage=stage, event=event,
            pid=os.getpid(), thread_id=threading.get_native_id(), epoch_ms=int(time.time()*1000),
            mono_ms=int(time.monotonic()*1000),
            elapsed_ms=0 if started is None else max(0, int((time.monotonic()-started)*1000)),
            error=error_kind(exc), worker_id=worker_id, child_pid=child_pid, returncode=returncode)
        if (worker_id and (type(worker_id) is not str or len(worker_id) != 32
                          or any(c not in "0123456789abcdef" for c in worker_id))):
            return
        if type(child_pid) is not int or (returncode is not None and type(returncode) is not int):
            return
        logging.getLogger(__name__).warning("execution_stage %s", json.dumps(value, sort_keys=True))
    except Exception:
        pass


@contextmanager
def phase(stage, request=None):
    token = None
    if _scope.get() is None:
        digest = hashlib.sha256(json.dumps(list(request)).encode()).hexdigest() if request else "unbound"
        token = _scope.set((uuid.uuid4().hex, digest))
    started = time.monotonic()
    try:
        emit(stage, "started", started=started)
        try:
            yield
        except BaseException as exc:
            emit(stage, "failed", started=started, exc=exc)
            raise
        else:
            emit(stage, "complete", started=started)
    finally:
        if token is not None:
            _scope.reset(token)


def diagnosed(stage):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            request = getattr(args[0], "_diagnostic_request", None) if args else None
            with phase(stage, request):
                return function(*args, **kwargs)
        return call
    return decorate
