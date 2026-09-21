"""Real graph and two stores; workspace return, no live VM or channel send."""

import asyncio
import base64
from dataclasses import asdict
import hashlib
import json
from types import SimpleNamespace
import time

import pytest
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from agentseek_execution.business_broker import BusinessBroker, BusinessPermit
from agentseek_execution.business_execution import BusinessRequest
from agentseek_execution.csv_business import BusinessStore
from agentseek_execution.business_workspace import CsvWorkspace
from agentseek_files.models import FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore

from enterprise_wecom_digital_employee.sandbox_authorization import (
    ApprovedGrantCatalog, SandboxGrant, instruction_digest, scoped_owner,
)
from enterprise_wecom_digital_employee.sandbox_remote import RemoteCsvRunner, remote_csv_tools
from test_sandbox_deepagent_loop import Context, State, ScriptedModel


@pytest.fixture
def remote(tmp_path):
    scope = ("tenant", "user", "session")
    owner = scoped_owner(scope)
    for name in ("node", "gateway", "workspace"):
        (tmp_path / name).mkdir(mode=0o700)
    node, gateway = (BusinessStore((tmp_path / n).resolve()) for n in ("node", "gateway"))
    workspace = CsvWorkspace((tmp_path / "workspace").resolve())
    files = LocalFileStore(FilesSettings(root_dir=tmp_path / "files"))
    data, output = b"group,amount\nA,3\n", b"group,total\nA,3\n"
    record = files.store_bytes(scope=FileScope(*scope), filename="input.csv", data=data)
    instruction = "sum amount by group"
    grant = SandboxGrant("approved-request", *scope, record.file_id, instruction_digest(instruction), time.time()+120)
    request = BusinessRequest(grant.request_id, owner, record.file_id, instruction)
    token, events, calls = "synthetic-secret-" + "x"*32, [], []
    permit = BusinessPermit(request, hashlib.sha256(data).hexdigest(), hashlib.sha256(token.encode()).hexdigest(), grant.expires_epoch)
    class Provider:
        def validate_request(self, r, d): assert r == request and d == data
        def create(self, attempt): events.append("create")
        def run(self, *args, **kwargs):
            events.append("execute")
            return json.dumps({"csv": base64.b64encode(output).decode(), "groups": 1})
        def destroy(self, attempt):
            assert node.snapshot(request).artifact_ref
            assert workspace.reference(request, attempt, node.snapshot(request).artifact_ref)["filename"] == "summary.csv"
            events.append("destroy")
            return True
    broker = BusinessBroker(permits=[permit], store=node, workspace=workspace, provider_for=lambda p: Provider())
    class Client:
        lose = False
        def exchange(self, req, *, data=None):
            calls.append("result" if data is None else "execute")
            result = broker.dispatch("Bearer " + token, dict(operation=calls[-1], request=asdict(req),
                input="" if data is None else base64.b64encode(data).decode()))
            if self.lose and data is not None:
                raise TimeoutError("synthetic lost response")
            return result
    client = Client()
    runner = RemoteCsvRunner(client=client, store=gateway)
    context = Context(dict(zip(("tenant_key", "user_key", "session_key"), scope)))
    runtime = SimpleNamespace(context=context, state={"current_files": [record.to_dict()]})
    return SimpleNamespace(node=node, gateway=gateway, files=files, data=data, output=output,
        request=request, grant=grant, record=record, events=events, calls=calls, client=client,
        runner=runner, scope=scope, owner=owner, context=context, runtime=runtime)


def test_lost_response_recovered_without_second_submit(remote):
    s = remote
    s.client.lose = True
    first = s.runner.execute(s.request, s.data)
    assert first.state == "reconciling" and not first.cleanup_confirmed
    assert s.runner.execute(s.request, s.data) == first
    assert s.calls == ["execute"]
    recovered = s.runner.recover(s.owner)
    assert recovered.state == "succeeded" and s.calls == ["execute", "result"]
    assert s.gateway.read(s.owner, recovered.artifact_ref) == s.output
    assert s.events == ["create", "execute", "destroy"]
    with pytest.raises(ValueError): s.runner.recover("other")


def test_proven_pre_send_failure_closes_without_retry(remote, monkeypatch):
    from dataclasses import replace
    from agentseek_execution.business_http import BusinessRequestNotSent
    calls = []
    def fail(*args, **kwargs):
        calls.append("connect")
        raise BusinessRequestNotSent()
    monkeypatch.setattr(remote.client, "exchange", fail)
    result = remote.runner.execute(remote.request, remote.data)
    assert result.state == "failed" and result.cleanup_confirmed
    assert remote.runner.execute(remote.request, remote.data) == result
    assert remote.runner.recover(remote.owner) == result
    assert calls == ["connect"] and remote.events == []
    # A different approved request is not blocked by this terminal reservation.
    remote.gateway.reserve(replace(remote.request, request_id="new-approved-request"))


def test_not_found_is_not_proof_of_no_submission(remote, monkeypatch):
    from dataclasses import replace
    remote.gateway.reserve(remote.request)
    monkeypatch.setattr(remote.client, "exchange", lambda *a, **k: {"state": "not_found"})
    with pytest.raises(ValueError):
        remote.runner.recover(remote.owner)
    assert remote.gateway.snapshot(remote.request).state == "reconciling"
    with pytest.raises(ValueError):
        remote.gateway.reserve(replace(remote.request, request_id="new-request"))


def test_real_graph_remote_result_returns_workspace_file(remote, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from urllib.parse import urlsplit
    from agentseek_files.workspace_download import WorkspaceDownloads, WorkspaceDownloadSettings
    from agentseek_wecom.workspace_routes import register_workspace_routes

    s = remote
    grants = tmp_path / "downloads"
    grants.mkdir(mode=0o700)
    downloads = WorkspaceDownloads(store=s.files, settings=WorkspaceDownloadSettings(
        "https://files.example.test/ai-server/workspace-files", grants))
    tools = remote_csv_tools(grant_for=lambda runtime: s.grant, file_store=s.files,
                             runner=s.runner, downloads=downloads)
    assert set(tools[0].tool_call_schema.model_json_schema()["properties"]) == {"input_ref", "instruction"}
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[dict(name="run_sandbox_task",
        id="csv-call", type="tool_call", args=dict(input_ref=s.record.file_id, instruction=s.request.instruction))]),
        AIMessage(content="已完成汇总，文件已保存到工作区。")])
    graph = create_deep_agent(model=model, tools=tools, context_schema=Context, state_schema=State)
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content=s.request.instruction)],
        "current_files": [s.record.to_dict()]}, context=s.context))
    outcome = json.loads(next(m.content for m in result["messages"] if isinstance(m, ToolMessage)))
    assert outcome["state"] == "succeeded" and outcome["workspace"]["state"] == "available"
    assert outcome["workspace"]["sha256"] == hashlib.sha256(s.output).hexdigest()
    parsed = urlsplit(outcome["workspace"]["download"]["url"])
    app = FastAPI()
    register_workspace_routes(app, downloads)
    response = TestClient(app).post(parsed.path + "/redeem", content=parsed.fragment)
    assert response.status_code == 200 and response.content == s.output
    assert "delivery" not in outcome
    assert s.calls == ["execute"] and s.events == ["create", "execute", "destroy"]


def test_link_failure_and_renewal_never_resubmit(remote, tmp_path, monkeypatch):
    from agentseek_files.workspace_download import WorkspaceDownloads, WorkspaceDownloadSettings

    s = remote
    directory = tmp_path / "downloads"
    directory.mkdir(mode=0o700)
    downloads = WorkspaceDownloads(store=s.files, settings=WorkspaceDownloadSettings(
        "https://files.example.test/ai-server/workspace-files", directory))
    issue = downloads.issue
    monkeypatch.setattr(downloads, "issue", lambda *args: (_ for _ in ()).throw(OSError("synthetic")))
    tools = remote_csv_tools(grant_for=lambda runtime: s.grant, file_store=s.files,
                             runner=s.runner, downloads=downloads)
    failed = asyncio.run(tools[0].coroutine(s.record.file_id, s.request.instruction, s.runtime))
    assert failed["state"] == "download_pending" and failed["cleanup_confirmed"]
    assert failed["workspace"]["state"] == "available"
    monkeypatch.setattr(downloads, "issue", issue)
    recovered = asyncio.run(tools[1].coroutine(s.runtime))
    assert recovered["state"] == "succeeded"
    assert recovered["workspace"]["download"]["state"] == "available"
    assert s.calls == ["execute"] and s.events == ["create", "execute", "destroy"]


def test_workspace_mirror_failure_recovers_without_rerun(remote, monkeypatch):
    s = remote
    tools = remote_csv_tools(grant_for=lambda runtime: s.grant, file_store=s.files, runner=s.runner)
    original = s.files.store_bytes
    def fail(**kwargs): raise OSError("disk unavailable")
    monkeypatch.setattr(s.files, "store_bytes", fail)
    result = asyncio.run(tools[0].coroutine(s.record.file_id, s.request.instruction, s.runtime))
    assert result["state"] == "workspace_pending" and result["cleanup_confirmed"]
    monkeypatch.setattr(s.files, "store_bytes", original)
    recovered = asyncio.run(tools[1].coroutine(s.runtime))
    assert recovered["workspace"]["state"] == "available"
    assert s.calls == ["execute"] and s.events == ["create", "execute", "destroy"]


def test_pinned_gateway_catalog_rejects_false_or_ambiguous_grants(remote, tmp_path):
    s = remote
    path = tmp_path / "grants.json"
    def catalog(approved, grants):
        path.write_text(json.dumps(dict(schema=1, approved=approved, grants=grants))); path.chmod(0o600)
        return ApprovedGrantCatalog(path.resolve(), hashlib.sha256(path.read_bytes()).hexdigest())
    assert catalog(True, [asdict(s.grant)])(s.runtime) == s.grant
    with pytest.raises(ValueError): catalog(False, [asdict(s.grant)])(s.runtime)
    with pytest.raises(ValueError): catalog(True, [asdict(s.grant), asdict(s.grant)])(s.runtime)


@pytest.mark.parametrize("history", ["none", "failed", "succeeded"])
def test_grant_visible_separately_from_history_without_writes(remote, history):
    from dataclasses import replace
    s = remote
    if history != "none":
        old = replace(s.request, request_id="old")
        attempt = s.gateway.reserve(old)
        s.gateway.record(attempt, history, None)
    tools = remote_csv_tools(grant_for=lambda _: s.grant, file_store=s.files, runner=s.runner)
    before = s.gateway.path.read_bytes()
    result = asyncio.run(tools[1].coroutine(s.runtime))
    assert result["state"] == ("no_task" if history == "none" else
                               "workspace_pending" if history == "succeeded" else history)
    assert result["grant_available"] is True
    assert result["authorization"]["request_id"] == s.grant.request_id
    assert result["authorization"]["action_checked"] is False
    assert s.gateway.path.read_bytes() == before
    assert s.calls == s.events == []


@pytest.mark.parametrize("case", ["expired", "scope", "no_file", "wrong_action", "half_action",
                                   "used", "older_used", "unresolved", "other_owner", "catalog_error", "tampered"])
def test_grant_visibility_fails_closed(remote, case):
    from dataclasses import replace
    s = remote
    grant = s.grant
    kwargs = dict(input_ref=s.record.file_id, instruction=s.request.instruction)
    if case == "expired": grant = replace(grant, expires_epoch=time.time()-1)
    if case == "scope": grant = replace(grant, session_key="other")
    if case == "no_file": s.runtime.state["current_files"] = []
    if case == "wrong_action": kwargs["instruction"] = "different action"
    if case == "half_action": kwargs.pop("instruction")
    if case == "tampered": s.files.original_path(s.record).write_bytes(b"tampered")
    if case in {"used", "older_used", "unresolved", "other_owner"}:
        request = replace(s.request, owner_id="other") if case == "other_owner" else s.request
        attempt = s.gateway.reserve(request)
        if case in {"used", "older_used"}: s.gateway.record(attempt, "failed", None)
        if case == "older_used":
            newer = s.gateway.reserve(replace(s.request, request_id="newer"))
            s.gateway.record(newer, "failed", None)
    def catalog(_):
        if case == "catalog_error": raise ValueError("secret-path-and-token")
        return grant
    tools = remote_csv_tools(grant_for=catalog, file_store=s.files, runner=s.runner)
    before = s.gateway.path.read_bytes()
    result = asyncio.run(tools[1].coroutine(s.runtime, **kwargs))
    assert result["grant_available"] is False
    assert "secret-path-and-token" not in json.dumps(result)
    assert s.gateway.path.read_bytes() == before
    assert "execute" not in s.calls and s.events == []


def test_grant_check_does_not_reserve_and_execution_rechecks_expiry(remote):
    from dataclasses import replace
    s = remote
    tools = remote_csv_tools(grant_for=lambda _: s.grant, file_store=s.files, runner=s.runner)
    result = asyncio.run(tools[1].coroutine(s.runtime, s.record.file_id, s.request.instruction))
    assert result["grant_available"] and result["authorization"]["action_checked"]
    s.grant = replace(s.grant, expires_epoch=time.time()-1)
    denied = asyncio.run(tools[0].coroutine(s.record.file_id, s.request.instruction, s.runtime))
    assert denied["state"] == "unavailable_or_rejected"
    assert s.gateway.latest_request(s.owner) is None and s.calls == s.events == []


def test_graph_reads_new_grant_then_executes_once_despite_old_failure(remote):
    from dataclasses import replace
    s = remote
    old = s.gateway.reserve(replace(s.request, request_id="old-failed"))
    s.gateway.record(old, "failed", None)
    tools = remote_csv_tools(grant_for=lambda _: s.grant, file_store=s.files, runner=s.runner)
    args = dict(input_ref=s.record.file_id, instruction=s.request.instruction)
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[dict(name="get_sandbox_task_result", id="check", type="tool_call", args=args)]),
        AIMessage(content="", tool_calls=[dict(name="run_sandbox_task", id="run", type="tool_call", args=args)]),
        AIMessage(content="done")])
    graph = create_deep_agent(model=model, tools=tools, context_schema=Context, state_schema=State)
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content=s.request.instruction)],
        "current_files": [s.record.to_dict()]}, context=s.context))
    outputs = [json.loads(m.content) for m in result["messages"] if isinstance(m, ToolMessage)]
    assert outputs[0]["state"] == "failed" and outputs[0]["grant_available"]
    assert outputs[1]["workspace"]["state"] == "available"
    assert s.calls == ["execute"] and s.events == ["create", "execute", "destroy"]
    again = asyncio.run(tools[1].coroutine(s.runtime))
    assert again["grant_available"] is False and s.calls == ["execute"]
