"""Gateway composition for remote CSV execution and scoped workspace return."""

import asyncio
from dataclasses import asdict

from langchain.tools import ToolRuntime, tool
from agentseek_files.models import FileScope

from .sandbox_authorization import SandboxRequestResolver, runtime_scope, scoped_owner
from .sandbox_composition import scoped_csv_bytes


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
        existing = self.store.snapshot(request)
        if existing is not None:
            return existing
        attempt = self.store.reserve(request)
        try:
            return self._receive(request, self.client.exchange(request, data=data))
        except Exception:
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


def remote_csv_tools(*, grant_for, file_store, runner, downloads=None):
    """Explicit opt-in; workspace files only, no channel media upload/send."""

    def workspace_result(runtime, outcome):
        result = asdict(outcome)
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
        """
        try:
            request = resolver(runtime)(runtime, input_ref, instruction)
            data = scoped_csv_bytes(file_store, runtime, runtime_scope(runtime), input_ref)
            outcome = await asyncio.to_thread(runner.execute, request, data)
            return await asyncio.to_thread(workspace_result, runtime, outcome)
        except Exception:
            return {"state": "unavailable_or_rejected", "retry_allowed": False}

    @tool
    async def get_sandbox_task_result(runtime: ToolRuntime) -> dict:
        """Read/reconcile your last task and renew its file link; never recreates."""
        try:
            outcome = await asyncio.to_thread(runner.recover, scoped_owner(runtime_scope(runtime)))
            return await asyncio.to_thread(workspace_result, runtime, outcome)
        except Exception:
            return {"state": "unavailable_or_rejected", "retry_allowed": False}

    @tool
    def read_sandbox_csv_result(artifact_ref: str, runtime: ToolRuntime) -> dict:
        """Read persisted CSV bytes for your own session; never reruns computation."""
        try:
            data = runner.store.read(scoped_owner(runtime_scope(runtime)), artifact_ref)
            return {"state": "available", "filename": "summary.csv", "csv": data.decode("utf-8")}
        except Exception:
            return {"state": "unavailable_or_rejected"}

    return [run_sandbox_task, get_sandbox_task_result, read_sandbox_csv_result]
