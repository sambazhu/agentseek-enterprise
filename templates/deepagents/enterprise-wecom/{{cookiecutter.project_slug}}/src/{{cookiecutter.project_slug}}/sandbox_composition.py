"""Explicit CSV pilot composition; no production environment auto-enablement.

Requires server-issued grant and an approved same-host provider. A remote gateway
must use an authenticated broker, not transplant .172 credentials into .171.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib

from agentseek_files.models import FileRecord
from langchain.tools import ToolRuntime, tool

from .sandbox_authorization import SandboxRequestResolver, runtime_scope, scoped_owner
from .sandbox_tools import sandbox_business_tools


def scoped_csv_bytes(file_store, runtime, scope, file_id):
    from agentseek_execution.csv_business import MAX_CSV_BYTES
    state = runtime.state if isinstance(runtime.state, Mapping) else {}
    matches = []
    for item in state.get("current_files", []):
        if isinstance(item, FileRecord):
            item = item.to_dict()
        if isinstance(item, dict) and item.get("file_id") == file_id:
            matches.append(FileRecord.from_dict(item))
    if len(matches) != 1:
        raise ValueError("file not uniquely available in this turn")
    expected = matches[0]
    record = file_store.load_record(expected.relative_dir)
    if (record.file_id != file_id or record.relative_dir != expected.relative_dir
            or (record.tenant_key, record.employee_key, record.session_key) != scope
            or record.direction != "inbound" or not record.filename.lower().endswith(".csv")
            or not 0 < record.size_bytes <= MAX_CSV_BYTES):
        raise ValueError("file scope or format mismatch")
    if record.expires_at and datetime.fromisoformat(record.expires_at) <= datetime.now(timezone.utc):
        raise ValueError("file expired")
    with file_store.original_path(record).open("rb") as stream:
        data = stream.read(MAX_CSV_BYTES + 1)
    if len(data) != record.size_bytes or hashlib.sha256(data).hexdigest() != record.sha256:
        raise ValueError("file content mismatch")
    return data


def csv_pilot_tools(*, grant_for, file_store, business_store, provider_for):
    """Build real tools with trusted grant/file/provider adapters supplied explicitly."""
    from agentseek_execution.csv_business import CsvBusinessBackend

    def file_bytes(runtime, scope, file_id):
        return scoped_csv_bytes(file_store, runtime, scope, file_id)

    def resolver_for(runtime):
        def allowed(scope, file_id):
            file_bytes(runtime, scope, file_id)
            return True
        return SandboxRequestResolver(grant_for=grant_for, file_allowed=allowed)

    def resolve(runtime, file_id, instruction):
        return resolver_for(runtime)(runtime, file_id, instruction)

    def backend_for(request, runtime):
        grant = grant_for(runtime)
        scope = (grant.tenant_key, grant.user_key, grant.session_key)
        def authorize(current):
            return resolve(runtime, current.input_ref, current.instruction) == current
        return CsvBusinessBackend(request=request, store=business_store,
            provider=provider_for(request), authorize=authorize,
            load_input=lambda current: file_bytes(runtime, scope, current.input_ref))

    @tool
    def read_sandbox_csv_result(artifact_ref: str, runtime: ToolRuntime) -> dict:
        """Read the persisted CSV result of your approved sandbox task after cleanup.

        This reads durable storage only, never recreates a sandbox or reruns work.
        """
        try:
            data = business_store.read(scoped_owner(runtime_scope(runtime)), artifact_ref)
            return {"state": "available", "filename": "summary.csv", "csv": data.decode("utf-8")}
        except Exception:
            return {"state": "unavailable_or_rejected"}

    return [*sandbox_business_tools(resolve=resolve, backend_for=backend_for), read_sandbox_csv_result]
