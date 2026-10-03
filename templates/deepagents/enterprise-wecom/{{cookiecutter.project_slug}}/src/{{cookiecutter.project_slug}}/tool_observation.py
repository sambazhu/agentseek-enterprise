"""Bounded, pseudonymous tool observations; never an authorization or receipt."""

import asyncio
import hashlib
import inspect
import json
import uuid
from functools import wraps

from loguru import logger

from .sandbox_authorization import runtime_scope

_NAMES = {
    "run_sandbox_task": "sandbox_tool",
    "get_sandbox_task_result": "sandbox_tool",
    "read_sandbox_csv_result": "sandbox_tool",
    "deliver_workspace_file": "workspace_delivery_tool",
}
_STATES = frozenset({
    "succeeded", "failed", "reconciling", "not_executed", "no_task", "available",
    "workspace_pending", "download_pending", "unavailable_or_rejected",
    "api_accepted", "upload_failed", "uncertain", "denied",
})


def identifier_hash(value):
    """Compatibility: request/call hashing is SHA256 of the original UTF-8 ID."""
    if type(value) is not str or not value or len(value) > 4096:
        return "absent"
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeError:
        return "absent"


def scope_hash(runtime):
    try:
        scope = runtime_scope(runtime)
        if any(len(part) > 4096 for part in scope):
            return "absent"
        encoded = json.dumps(scope, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(("scope-v1:" + encoded).encode("utf-8")).hexdigest()
    except Exception:
        return "absent"  # Never inspect mutable graph state for identity.


def _fields(result, arguments, delivery):
    result = result if type(result) is dict else {}
    state = result.get("status" if delivery else "state")
    fields = {
        "state": state if type(state) is str and state in _STATES else "other",
        "current": result.get("matches_current_request") is True,
        "request_sha256": "absent", "attempt_sha256": "absent",
        "artifact_sha256": "absent", "file_ref_sha256": "absent",
        "delivery_sha256": "absent", "reused": "absent",
    }
    if delivery:
        # Only a validated delivery result identifies a file. Denied proposals
        # must not be recorded as a verified association or another user's ID.
        delivery_id = result.get("delivery_id")
        if type(state) is str and state in {"api_accepted", "upload_failed", "uncertain"} and type(delivery_id) is str:
            fields["delivery_sha256"] = identifier_hash(delivery_id)
            fields["file_ref_sha256"] = identifier_hash(arguments.get("file_ref"))
            if type(result.get("reused_receipt")) is bool:
                fields["reused"] = result["reused_receipt"]
        return fields
    for key in ("request", "attempt", "artifact"):
        source = {"request": "request_id", "attempt": "attempt", "artifact": "artifact_ref"}[key]
        fields[key + "_sha256"] = identifier_hash(result.get(source))
    workspace = result.get("workspace")
    link = workspace.get("association") if type(workspace) is dict else None
    if (type(link) is dict and link.get("state") == "verified"
            and all(link.get(k) == result.get(k) and type(result.get(k)) is str
                    for k in ("request_id", "attempt", "artifact_ref"))):
        fields["file_ref_sha256"] = identifier_hash(link.get("file_ref"))
    return fields


def _emit(prefix, name, event, phase, context, fields):
    try:
        # Values are fixed tokens, bools or hashes. No exception objects, bodies,
        # filenames, paths, recipients, credentials or arbitrary result values.
        values = {"schema": 1, "name": name, "event": event, "phase": phase, **fields, **context}
        logger.info("{} {}", prefix, " ".join(f"{key}={value}" for key, value in values.items()))
    except Exception:  # noqa: S110 - never recurse into a broken logger or expose its exception
        pass  # Logging failure must not turn a sent message into a retryable failure.


def observed(fn):
    prefix = _NAMES[fn.__name__]
    signature = inspect.signature(fn)

    @wraps(fn)
    async def wrapped(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs).arguments
        runtime = arguments.get("runtime")
        context = {"call_sha256": identifier_hash(getattr(runtime, "tool_call_id", None)),
                   "scope_sha256": scope_hash(runtime)}
        event = uuid.uuid4().hex
        _emit(prefix, fn.__name__, event, "start", context, {})
        try:
            result = await fn(*args, **kwargs)
        except BaseException as exc:
            phase = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            _emit(prefix, fn.__name__, event, phase, context, {})
            raise
        try:
            fields = _fields(result, arguments, prefix == "workspace_delivery_tool")
        except Exception:
            fields = {"state": "other"}  # Observation failure is not a business failure.
        _emit(prefix, fn.__name__, event, "complete", context, fields)
        return result
    return wrapped
