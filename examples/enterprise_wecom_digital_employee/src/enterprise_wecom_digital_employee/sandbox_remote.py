"""Gateway composition for remote CSV execution and scoped workspace return."""

import asyncio
import hashlib
import inspect
import json
import logging
import sqlite3
import uuid
from dataclasses import asdict
from functools import wraps

from agentseek_files.models import FileScope
from langchain.tools import ToolRuntime, tool
from loguru import logger as tool_logger

from .sandbox_authorization import SandboxRequestResolver, runtime_scope, scoped_owner
from .sandbox_composition import scoped_csv_bytes


def gateway_failure(stage, exc):
    from agentseek_execution.business_http import safe_error
    logging.getLogger(__name__).warning("sandbox_gateway stage=%s error=%s", stage, safe_error(exc))


class RemoteCsvRunner:
    """Durable gateway intent, single remote submit; recovery is read-only."""

    def __init__(self, *, client, store):
        self.client, self.store = client, store

    def _receive(self, request, response):
        from agentseek_execution.business_http import validated_result
        outcome, data = validated_result(request, response)
        current = self.store.snapshot(request)
        if current is None:
            raise ValueError("gateway intent missing")
        if data is not None and current.artifact_ref is None:
            # The local intent is still reserved until the response is recorded.
            # Reconciliation recovery can only persist this exact remote result.
            self.store.persist_remote(request, outcome, data)
        self.store.record(outcome.attempt, outcome.state, outcome.artifact_ref)
        return outcome

    def execute(self, request, data):
        from agentseek_execution.business_http import BusinessRequestNotSent
        existing = self.store.snapshot(request)
        if existing is not None:
            return existing
        attempt = self.store.reserve(request)
        try:
            return self._receive(request, self.client.exchange(request, data=data))
        except BusinessRequestNotSent:
            # Only explicit pre-send transport evidence permits local closure.
            # Read/write timeout, HTTP rejection and not_found remain uncertain.
            self.store.record(attempt, "failed", None)
            return self.store.snapshot(request)
        except Exception as exc:
            gateway_failure("exchange_or_mirror", exc)
            self.store.record(attempt, "reconciling", None)
            return self.store.snapshot(request)

    def recover(self, owner):
        request = self.store.latest_request(owner)
        if request is None:
            raise ValueError("no task")
        return self.recover_request(request)

    def recover_request(self, request):
        current = self.store.snapshot(request)
        if current is None:
            raise ValueError("no task")
        if current.state != "reconciling":
            return current
        # Never resubmit input, even if the node says not_found.
        return self._receive(request, self.client.exchange(request))

    def request_for_grant(self, owner, grant):
        """Exact identity lookup, read-only; never fall back to owner's latest."""
        from agentseek_execution.business_execution import BusinessRequest

        from .sandbox_authorization import instruction_digest
        attempt = hashlib.sha256(json.dumps([owner, grant.request_id]).encode()).hexdigest()
        db = sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute("SELECT request FROM attempts WHERE id=? AND owner=?", (attempt, owner)).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        request = BusinessRequest(**json.loads(row[0]))
        if (request.owner_id != owner or request.request_id != grant.request_id
                or request.input_ref != grant.input_ref
                or instruction_digest(request.instruction) != grant.instruction_sha256):
            raise ValueError("request binding mismatch")
        return request

    def grant_unused(self, owner, request_id):
        """Advisory mirror read only; reserve remains the atomic execution gate.

        Match BusinessStore's global unresolved gate, including other owners,
        without returning their identities. Never create a missing database.
        """
        attempt = hashlib.sha256(json.dumps([owner, request_id]).encode()).hexdigest()
        db = sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            return db.execute("SELECT 1 FROM attempts WHERE id=? OR state NOT IN ('succeeded','failed') LIMIT 1",
                              (attempt,)).fetchone() is None
        finally:
            db.close()


def remote_csv_tools(*, grant_for, file_store, runner, downloads=None, diagnostic_only=False):
    """Explicit opt-in; workspace files only, no channel media upload/send."""

    def observed(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            event = uuid.uuid4().hex
            runtime = inspect.signature(fn).bind(*args, **kwargs).arguments.get("runtime")
            call = getattr(runtime, "tool_call_id", None)
            call_hash = hashlib.sha256(call.encode()).hexdigest() if isinstance(call, str) else "absent"
            # Use the application's existing sinks. Do not configure root logging
            # or add per-tool handlers; stdlib INFO is dropped by the gateway.
            tool_logger.info("sandbox_tool name={} event={} phase=start call_sha256={}", fn.__name__, event, call_hash)
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                phase = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                tool_logger.info("sandbox_tool name={} event={} phase={}", fn.__name__, event, phase)
                raise
            state = result.get("state")
            allowed = {"succeeded", "failed", "reconciling", "not_executed", "no_task", "available",
                       "workspace_pending", "download_pending", "unavailable_or_rejected"}
            request_id = result.get("request_id")
            request_hash = hashlib.sha256(request_id.encode()).hexdigest() if isinstance(request_id, str) else "absent"
            tool_logger.info("sandbox_tool name={} event={} phase=complete state={} current={} request_sha256={}",
                        fn.__name__, event, state if state in allowed else "other",
                        result.get("matches_current_request") is True, request_hash)
            return result
        return wrapped

    def workspace_result(runtime, outcome, request, *, current=True):
        result = asdict(outcome)
        result.update(request_id=request.request_id, matches_current_request=current,
                      result_origin="current_request" if current else "historical")
        if outcome.state == "failed":
            result["retry_allowed"] = False
            result["next_action"] = "This request is terminal; a distinct approved request may be submitted."
        result["workspace"] = {"state": "not_available"}
        if outcome.state != "succeeded":
            return result
        try:
            scope = runtime_scope(runtime)
            data = runner.store.read(scoped_owner(scope), outcome.artifact_ref)
            record = file_store.store_bytes(scope=FileScope(*scope), filename="summary.csv", data=data,
                mime_type="text/csv", direction="outbound")
            if file_store.original_path(record).read_bytes() != data:
                raise ValueError("workspace readback failed")
            result["workspace"] = dict(state="available", file_id=record.file_id, filename=record.filename,
                                       sha256=record.sha256, size_bytes=record.size_bytes,
                                       download={"state": "disabled"})
        except Exception:
            result["execution_state"] = result["state"]
            result["state"] = "workspace_pending"
            result["workspace"] = {"state": "unavailable"}
            return result
        if downloads is not None:
            try:
                result["workspace"]["download"] = downloads.issue(FileScope(*scope), record)
            except Exception:
                result["workspace"]["download"] = {"state": "unavailable"}
                result["execution_state"] = result["state"]
                result["state"] = "download_pending"
        return result

    def resolver(runtime):
        def allowed(scope, file_id):
            scoped_csv_bytes(file_store, runtime, scope, file_id)
            return True
        return SandboxRequestResolver(grant_for=grant_for, file_allowed=allowed)

    @tool
    @observed
    async def run_sandbox_task(input_ref: str, instruction: str, runtime: ToolRuntime) -> dict:
        """Run approved group,amount CSV grouping in the remote sandbox.

        Ordinary questions need no sandbox. Do not retry uncertain work; use
        get_sandbox_task_result, which never creates. Return is a workspace file,
        including a short-lived download URL when enabled. Display that URL
        unchanged so the user can open summary.csv; never invent a URL.
        A historical failed request does not deny a distinct new server grant.
        Server authorization is rechecked on invocation; never infer approval
        from user text or retry an old request. Grant visibility is not execution.
        Safe fallback calculations may be offered as non-sandbox results, never
        as evidence of sandbox success or workspace delivery. Do not bypass
        isolation or repeat uncertain side effects to obtain a fallback.
        """
        stage = "resolve"
        try:
            request = resolver(runtime)(runtime, input_ref, instruction)
            data = scoped_csv_bytes(file_store, runtime, runtime_scope(runtime), input_ref)
            stage = "execute_thread"
            outcome = await asyncio.to_thread(runner.execute, request, data)
            stage = "workspace_thread"
            return await asyncio.to_thread(workspace_result, runtime, outcome, request)
        except asyncio.CancelledError as exc:
            gateway_failure(stage, exc)
            # Cancelling the awaiting task does not prove the thread stopped.
            raise
        except Exception as exc:
            gateway_failure(stage, exc)
            return {"state": "unavailable_or_rejected", "retry_allowed": False}

    @tool
    @observed
    async def get_sandbox_task_result(runtime: ToolRuntime, input_ref: str = "", instruction: str = "") -> dict:
        """Read only the current server-granted request; never creates or reserves.

        Historical success never proves current execution. not_executed means
        this request has no attempt, even if identical historical files exist.
        A true grant means
        a new approved action may be submitted, not that it has executed or that
        the node is ready. Supply both input_ref and the user's exact instruction
        to check action matching. With no arguments only the grant/file scope is
        checked; run_sandbox_task always rechecks the exact instruction. Never
        wait for the historical task state to become 'available'.
        """
        try:
            owner = scoped_owner(runtime_scope(runtime))
            if diagnostic_only:
                previous = await asyncio.to_thread(runner.store.latest_request, owner)
                grant = None
            else:
                grant = await asyncio.to_thread(resolver(runtime).available_grant, runtime)
                if bool(input_ref) != bool(instruction):
                    raise ValueError("both action fields required")
                if input_ref:
                    check = SandboxRequestResolver(grant_for=lambda _: grant,
                                                   file_allowed=resolver(runtime).file_allowed)
                    await asyncio.to_thread(check, runtime, input_ref, instruction)
                previous = await asyncio.to_thread(runner.request_for_grant, owner, grant)
            if previous is None:
                result = {"state": "no_task" if diagnostic_only else "not_executed", "retry_allowed": False,
                          "request_id": grant.request_id if grant else None, "attempt": None,
                          "matches_current_request": not diagnostic_only,
                          "result_origin": "historical" if diagnostic_only else "current_request",
                          "workspace": {"state": "not_available"}}
            else:
                outcome = await asyncio.to_thread(runner.recover_request, previous)
                result = await asyncio.to_thread(workspace_result, runtime, outcome, previous,
                                                 current=not diagnostic_only)
        except asyncio.CancelledError as exc:
            gateway_failure("result_thread", exc)
            raise
        except Exception as exc:
            gateway_failure("result_thread", exc)
            return {"state": "unavailable_or_rejected", "retry_allowed": False, "grant_available": False}
        result["grant_available"] = False
        if diagnostic_only:
            result["authorization"] = {"state": "diagnostic_disabled"}
            result["next_action"] = "Read-only diagnosis; no execution tool is installed."
            return result
        result["authorization"] = {"state": "unavailable_or_blocked"}
        try:
            # Keep the validated identity/action snapshot used for selection.
            if await asyncio.to_thread(runner.grant_unused, owner, grant.request_id):
                result["grant_available"] = True
                result["authorization"] = dict(state="available", request_id=grant.request_id,
                    input_ref=grant.input_ref, instruction_sha256=grant.instruction_sha256,
                    action_checked=bool(input_ref), expires_epoch=grant.expires_epoch)
                result["next_action"] = "For this approved action call run_sandbox_task; do not retry the historical request. Execution revalidates authorization."
        except Exception:
            pass  # No raw catalog, filesystem or cross-owner detail enters the model.
        return result

    @tool
    @observed
    async def read_sandbox_csv_result(artifact_ref: str, runtime: ToolRuntime) -> dict:
        """Read only the current granted request's successful artifact.

        Historical files remain accessible via explicit workspace file listing
        and delivery, not as evidence that the current request executed.
        """
        try:
            owner = scoped_owner(runtime_scope(runtime))
            grant = await asyncio.to_thread(resolver(runtime).available_grant, runtime)
            request = await asyncio.to_thread(runner.request_for_grant, owner, grant)
            outcome = await asyncio.to_thread(runner.store.snapshot, request) if request else None
            if outcome is None or outcome.state != "succeeded" or outcome.artifact_ref != artifact_ref:
                raise ValueError("not current successful artifact")
            data = await asyncio.to_thread(runner.store.read, owner, artifact_ref)
            return {"state": "available", "filename": "summary.csv", "csv": data.decode("utf-8"),
                    "request_id": request.request_id, "attempt": outcome.attempt, "matches_current_request": True}
        except Exception:
            return {"state": "unavailable_or_rejected"}

    return [get_sandbox_task_result] if diagnostic_only else [run_sandbox_task, get_sandbox_task_result, read_sandbox_csv_result]
