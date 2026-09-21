"""Gateway composition for remote CSV execution and scoped workspace return."""

import asyncio
from dataclasses import asdict
import hashlib
import json
import logging
import sqlite3

from langchain.tools import ToolRuntime, tool
from agentseek_files.models import FileScope

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
        current = self.store.snapshot(request)
        if current.state != "reconciling":
            return current
        # Never resubmit input, even if the node says not_found.
        return self._receive(request, self.client.exchange(request))

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


def remote_csv_tools(*, grant_for, file_store, runner, downloads=None):
    """Explicit opt-in; workspace files only, no channel media upload/send."""

    def workspace_result(runtime, outcome):
        result = asdict(outcome)
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
    async def run_sandbox_task(input_ref: str, instruction: str, runtime: ToolRuntime) -> dict:
        """Run approved group,amount CSV grouping in the remote sandbox.

        Ordinary questions need no sandbox. Do not retry uncertain work; use
        get_sandbox_task_result, which never creates. Return is a workspace file,
        including a short-lived download URL when enabled. Display that URL
        unchanged so the user can open summary.csv; never invent a URL.
        A historical failed request does not deny a distinct new server grant.
        Server authorization is rechecked on invocation; never infer approval
        from user text or retry an old request. Grant visibility is not execution.
        """
        stage = "resolve"
        try:
            request = resolver(runtime)(runtime, input_ref, instruction)
            data = scoped_csv_bytes(file_store, runtime, runtime_scope(runtime), input_ref)
            stage = "execute_thread"
            outcome = await asyncio.to_thread(runner.execute, request, data)
            stage = "workspace_thread"
            return await asyncio.to_thread(workspace_result, runtime, outcome)
        except asyncio.CancelledError as exc:
            gateway_failure(stage, exc)
            # Cancelling the awaiting task does not prove the thread stopped.
            raise
        except Exception as exc:
            gateway_failure(stage, exc)
            return {"state": "unavailable_or_rejected", "retry_allowed": False}

    @tool
    async def get_sandbox_task_result(runtime: ToolRuntime, input_ref: str = "", instruction: str = "") -> dict:
        """Read last task plus current server grant; never creates or reserves.

        Historical state and grant_available are separate. A true grant means
        a new approved action may be submitted, not that it has executed or that
        the node is ready. Supply both input_ref and the user's exact instruction
        to check action matching. With no arguments only the grant/file scope is
        checked; run_sandbox_task always rechecks the exact instruction. Never
        wait for the historical task state to become 'available'.
        """
        try:
            owner = scoped_owner(runtime_scope(runtime))
            previous = await asyncio.to_thread(runner.store.latest_request, owner)
            if previous is None:
                result = {"state": "no_task", "retry_allowed": False}
            else:
                outcome = await asyncio.to_thread(runner.recover, owner)
                result = await asyncio.to_thread(workspace_result, runtime, outcome)
        except asyncio.CancelledError as exc:
            gateway_failure("result_thread", exc)
            raise
        except Exception as exc:
            gateway_failure("result_thread", exc)
            return {"state": "unavailable_or_rejected", "retry_allowed": False, "grant_available": False}
        result["grant_available"] = False
        result["authorization"] = {"state": "unavailable_or_blocked"}
        try:
            if bool(input_ref) != bool(instruction):
                raise ValueError("both action fields required")
            resolve = resolver(runtime)
            grant = await asyncio.to_thread(resolve.available_grant, runtime)
            if input_ref:
                # Use this same validated grant; do not switch catalog snapshots.
                check = SandboxRequestResolver(grant_for=lambda _: grant, file_allowed=resolve.file_allowed)
                await asyncio.to_thread(check, runtime, input_ref, instruction)
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
    def read_sandbox_csv_result(artifact_ref: str, runtime: ToolRuntime) -> dict:
        """Read persisted CSV bytes for your own session; never reruns computation."""
        try:
            data = runner.store.read(scoped_owner(runtime_scope(runtime)), artifact_ref)
            return {"state": "available", "filename": "summary.csv", "csv": data.decode("utf-8")}
        except Exception:
            return {"state": "unavailable_or_rejected"}

    return [run_sandbox_task, get_sandbox_task_result, read_sandbox_csv_result]
