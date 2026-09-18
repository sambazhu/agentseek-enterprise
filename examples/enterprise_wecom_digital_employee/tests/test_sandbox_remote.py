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


def test_real_graph_remote_result_returns_workspace_file(remote):
    s = remote
    tools = remote_csv_tools(grant_for=lambda runtime: s.grant, file_store=s.files,
                             runner=s.runner)
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
    assert "delivery" not in outcome
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
