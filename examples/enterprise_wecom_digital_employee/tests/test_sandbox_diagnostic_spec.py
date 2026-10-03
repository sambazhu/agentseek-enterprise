"""Public diagnostic spec, real DeepAgent + local HTTPS, zero create tools."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from enterprise_wecom_digital_employee import agent, sandbox_spec
from test_sandbox_transport import tls_graph
from test_sandbox_remote import remote
from test_sandbox_deepagent_loop import ScriptedModel


@pytest.fixture
def configured_diagnostic(tls_graph, tmp_path, monkeypatch):
    import agentseek_execution.business_http as transport
    import agentseek_files.store as files
    import agentseek_files.workspace_download as downloads
    s = tls_graph
    token = tmp_path / "token"
    token.write_text(s.token); token.chmod(0o600)
    grants = tmp_path / "grants.json"
    grants.write_text("{}"); grants.chmod(0o600)  # result query does not require a live grant
    config = dict(schema=1, approved=True, endpoint=s.http.endpoint, ca_file=str(tmp_path / "test-cert.pem"),
        broker_token_file=str(token), grants_file=str(grants), grants_sha256=hashlib.sha256(grants.read_bytes()).hexdigest(),
        mirror_directory=str(s.gateway.path.parent))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config)); path.chmod(0o600)
    monkeypatch.setenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG", str(path))
    monkeypatch.setenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(files, "LocalFileStore", lambda _: s.files)
    monkeypatch.setattr(transport, "BusinessHttpClient", lambda **kwargs: s.http)
    monkeypatch.setattr(downloads, "configured_workspace_downloads",
                        lambda _: pytest.fail("diagnostic must not configure downloads"))
    s.bindings = []
    class RecordingModel(ScriptedModel):
        def bind_tools(self, tools, **kwargs):
            s.bindings.append([t.name for t in tools])
            return self
    def configure_model(tool_name):
        args = ({"file_path": "/blocked.txt", "content": "blocked"} if tool_name == "write_file" else
                {"description": "blocked", "subagent_type": "general-purpose"} if tool_name == "task" else
                {"command": "echo blocked"} if tool_name == "execute" else {})
        model = RecordingModel(responses=[AIMessage(content="", tool_calls=[dict(name=tool_name,
            id="diagnose", type="tool_call", args=args)]), AIMessage(content="done")])
        monkeypatch.setattr(agent, "get_settings", lambda: SimpleNamespace(
            work_enabled=False, build_model=lambda: model, openai_request_timeout_s=30))
    s.configure_model = configure_model
    return s


@pytest.mark.parametrize("tool_name", ["get_sandbox_task_result", "run_sandbox_task", "write_file", "call_mcp_tool", "task", "execute"])
def test_public_diagnostic_spec_visible_tools_and_dispatch(configured_diagnostic, tool_name):
    s = configured_diagnostic
    s.gateway.reserve(s.request)  # synthetic existing unresolved request, no node attempt
    s.configure_model(tool_name)
    spec = sandbox_spec.build_diagnostic_spec()
    registered = spec.runnable.nodes["tools"].bound.tools_by_name
    assert "get_sandbox_task_result" in registered
    assert not {"run_sandbox_task", "read_sandbox_csv_result", "call_mcp_tool"} & set(registered)
    before = s.gateway.path.read_bytes()
    result = asyncio.run(spec.runnable.ainvoke({"messages": [HumanMessage(content="仅查询既有任务")]}, context=s.context))
    assert s.bindings and all(names == ["get_sandbox_task_result"] for names in s.bindings)
    assert s.events == [] and s.node.latest_request(s.owner) is None
    assert s.gateway.path.read_bytes() == before
    messages = [m.content for m in result["messages"] if isinstance(m, ToolMessage)]
    assert messages
    if tool_name == "get_sandbox_task_result":
        assert s.reached.is_set()  # real HTTPS result lookup reached synthetic broker
        assert json.loads(messages[0])["state"] == "unavailable_or_rejected"
    else:
        assert not s.reached.is_set()
        if tool_name in {"write_file", "task", "execute"}:
            assert messages[0] == "diagnostic_tool_denied"


def test_diagnostic_refuses_work_and_wrong_tool_set(monkeypatch):
    monkeypatch.setattr(agent, "get_settings", lambda: SimpleNamespace(work_enabled=True))
    with pytest.raises(ValueError, match="WORK disabled"):
        agent.build_spec(diagnostic_only=True)
    with pytest.raises(ValueError, match="exactly"):
        agent._build_diagnostic_agent([SimpleNamespace(name="run_sandbox_task")])


def test_diagnostic_requires_pinned_configuration(monkeypatch):
    monkeypatch.delenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG", raising=False)
    with pytest.raises(KeyError): sandbox_spec.build_diagnostic_spec()


def test_diagnostic_missing_mirror_does_not_create_one(configured_diagnostic):
    s = configured_diagnostic
    s.gateway.path.unlink()  # synthetic test DB only
    with pytest.raises(ValueError, match="existing mirror"):
        sandbox_spec.build_diagnostic_spec()
    assert not s.gateway.path.exists()


def test_diagnostic_no_task_does_not_advertise_or_query_grants(configured_diagnostic):
    s = configured_diagnostic
    s.configure_model("get_sandbox_task_result")
    spec = sandbox_spec.build_diagnostic_spec()
    result = asyncio.run(spec.runnable.ainvoke({"messages": [HumanMessage(content="查询")]}, context=s.context))
    output = json.loads(next(m.content for m in result["messages"] if isinstance(m, ToolMessage)))
    assert output["state"] == "no_task" and output["grant_available"] is False
    assert output["authorization"]["state"] == "diagnostic_disabled"
    assert not s.reached.is_set() and not s.events
