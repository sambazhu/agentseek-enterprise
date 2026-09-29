"""Current-request attribution; no platform, live model or real file sending."""
# ruff: noqa: F811

import asyncio
import json
from dataclasses import asdict, replace
import hashlib

import pytest
from agentseek_langchain.spec import InvocationContext, RunnableSpec
from enterprise_wecom_digital_employee.sandbox_remote import remote_csv_tools
from enterprise_wecom_digital_employee.sandbox_result_guard import guarded_spec
from langchain_core.messages import AIMessage, HumanMessage
from test_sandbox_remote import remote  # noqa: F401


def tools_for(s):
    return remote_csv_tools(grant_for=lambda _: s.grant, file_store=s.files, runner=s.runner)


def history(s):
    outcome = s.runner.execute(s.request, s.data)
    s.grant = replace(s.grant, request_id="new-current-request")
    return outcome


def test_identical_old_success_is_neither_returned_nor_republished(remote, monkeypatch):
    s = remote
    old = history(s)
    monkeypatch.setattr(s.files, "store_bytes", lambda **kw: pytest.fail("historical republish"))
    result = asyncio.run(tools_for(s)[1].coroutine(s.runtime))
    assert result["state"] == "not_executed" and result["grant_available"]
    assert result["request_id"] == s.grant.request_id and result["matches_current_request"]
    assert old.artifact_ref not in json.dumps(result)
    assert s.calls == ["execute"]
    denied = asyncio.run(tools_for(s)[2].coroutine(old.artifact_ref, s.runtime))
    assert denied["state"] == "unavailable_or_rejected"


@pytest.mark.parametrize("state", ["failed", "reconciling", "succeeded"])
def test_current_state_not_latest_owner_row(remote, state):
    s = remote
    # Current identity is older than the owner's latest terminal request.
    result = s.runner.execute(s.request, s.data)
    if state != "succeeded":
        with s.gateway._connect() as db:
            db.execute("UPDATE attempts SET state=? WHERE id=?", (state, result.attempt))
    if state == "reconciling":
        # No network: preserve current unknown state without resolving it.
        s.runner.recover_request = lambda request: s.gateway.snapshot(request)
    else:
        other = s.gateway.reserve(replace(s.request, request_id="newer-history"))
        s.gateway.record(other, "failed", None)
    out = asyncio.run(tools_for(s)[1].coroutine(s.runtime))
    assert out["state"] == state
    assert out["request_id"] == s.request.request_id and out["attempt"] == result.attempt


def test_current_artifact_read_attribution(remote):
    s = remote
    outcome = s.runner.execute(s.request, s.data)
    result = asyncio.run(tools_for(s)[2].coroutine(outcome.artifact_ref, s.runtime))
    assert result["state"] == "available" and result["matches_current_request"]
    assert result["request_id"] == s.request.request_id
    assert result["csv"].encode() == s.output


def test_tampered_stored_binding_rejected(remote):
    s = remote
    outcome = s.runner.execute(s.request, s.data)
    with s.gateway._connect() as db:
        db.execute("UPDATE attempts SET request=? WHERE id=?",
                   (json.dumps({**s.request.__dict__, "instruction": "SECRET"}), outcome.attempt))
    out = asyncio.run(tools_for(s)[1].coroutine(s.runtime))
    assert out["state"] == "unavailable_or_rejected"
    assert "SECRET" not in json.dumps(out)


def make_spec(s, tmp_path, text=None):
    class FakeGraph:
        async def ainvoke(self, value, **kwargs):
            return "FAKE succeeded cleanup_confirmed=true OLD FILE"
    spec = RunnableSpec(FakeGraph(), lambda ctx: {"messages": [
        AIMessage(content="W17 succeeded"), HumanMessage(content=ctx.prompt)]}, str)
    wrapped = guarded_spec(spec, grant_for=lambda _: s.grant, runner=s.runner)
    ctx = InvocationContext(prompt=text or s.request.instruction, session_id="s", state={},
        workspace=tmp_path, agents_md=None, runtime_context={"enterprise": s.context.enterprise})
    return wrapped, ctx


@pytest.mark.parametrize("mode", ["invoke", "stream"])
def test_historical_memory_claim_cannot_escape_output_boundary(remote, tmp_path, mode):
    s = remote
    history(s)
    spec, ctx = make_spec(s, tmp_path)
    async def run():
        if mode == "invoke":
            return await spec.invoke(ctx)
        return "".join([part async for part in spec.stream(ctx)])
    text = asyncio.run(run())
    assert "尚未执行" in text and s.grant.request_id in text
    assert "FAKE" not in text and "W17" not in text
    assert s.calls == ["execute"]  # boundary never creates or queries the node


@pytest.mark.parametrize("state", ["succeeded", "failed", "reconciling"])
def test_guard_uses_durable_current_status(remote, tmp_path, state):
    s = remote
    outcome = s.runner.execute(s.request, s.data)
    with s.gateway._connect() as db:
        db.execute("UPDATE attempts SET state=? WHERE id=?", (state, outcome.attempt))
    spec, ctx = make_spec(s, tmp_path)
    text = asyncio.run(spec.invoke(ctx))
    expected = {"succeeded": "结果已持久化", "failed": "任务失败", "reconciling": "状态未确定"}
    assert expected[state] in text and "FAKE" not in text
    assert outcome.attempt in text


def test_explicit_historical_file_request_keeps_normal_delivery_path(remote, tmp_path):
    spec, ctx = make_spec(remote, tmp_path, "把 summary.csv 发给我")
    assert asyncio.run(spec.invoke(ctx)).startswith("FAKE")


def test_guard_does_not_trust_model_state_or_direct_response(remote, tmp_path):
    s = remote
    class Graph:
        async def ainvoke(self, value, **kwargs):
            return {"state": "succeeded", "request_id": s.grant.request_id, "messages": [AIMessage(content="FAKE")]}
    base = RunnableSpec(Graph(), lambda ctx: {}, str, direct_response=lambda ctx: "FAKE shortcut")
    spec = guarded_spec(base, grant_for=lambda _: s.grant, runner=s.runner)
    ctx = InvocationContext(prompt=s.request.instruction, session_id="s", state={}, workspace=tmp_path,
                            agents_md=None, runtime_context={"enterprise": s.context.enterprise})
    text = asyncio.run(spec.invoke(ctx))
    assert "尚未执行" in text and "FAKE" not in text and s.calls == []


def test_guard_store_error_does_not_release_model_claim(remote, tmp_path, monkeypatch):
    spec, ctx = make_spec(remote, tmp_path)
    def fail(*args):
        raise ValueError("SECRET path credential")
    monkeypatch.setattr(remote.runner, "request_for_grant", fail)
    text = asyncio.run(spec.invoke(ctx))
    assert "暂不可核验" in text and "SECRET" not in text and "FAKE" not in text


def test_audit_start_end_correlated_and_no_body(remote, sandbox_tool_logs):
    s = remote
    asyncio.run(tools_for(s)[1].coroutine(s.runtime))
    events = [r["message"] for r in sandbox_tool_logs if r["message"].startswith("sandbox_tool")]
    assert len(events) == 2
    assert "phase=start" in events[0] and "phase=complete state=not_executed current=True" in events[1]
    assert events[0].split("event=")[1].split()[0] == events[1].split("event=")[1].split()[0]
    for value in (s.request.instruction, s.request.input_ref, s.token, s.owner):
        assert value not in " ".join(events)


def test_audit_cancel_is_terminal_observation(remote, monkeypatch, sandbox_tool_logs):
    def cancelled(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(remote.runner, "request_for_grant", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tools_for(remote)[1].coroutine(remote.runtime))
    assert any("phase=cancelled" in r["message"] for r in sandbox_tool_logs)


@pytest.mark.parametrize("execute", [False, True])
def test_production_spec_real_graph_current_request_boundary(remote, tmp_path, monkeypatch, execute):
    from deepagents import create_deep_agent
    from agentseek_langchain.profiles import messages_spec
    from enterprise_wecom_digital_employee import agent, sandbox_spec
    import agentseek_execution.business_http as http
    import agentseek_files.store as files
    import agentseek_files.workspace_download as downloads
    from test_sandbox_deepagent_loop import Context, State, ScriptedModel

    s = remote
    old = s.gateway.reserve(replace(s.request, request_id="W17"))
    artifact = s.gateway.persist(old, s.owner, s.output)
    s.gateway.record(old, "succeeded", artifact)
    token = tmp_path / "token"
    token.write_text(s.token); token.chmod(0o600)
    grants = tmp_path / "grants"
    grants.write_text(json.dumps({"schema": 1, "approved": True, "grants": [asdict(s.grant)]}))
    grants.chmod(0o600)
    config = {"schema": 1, "approved": True, "endpoint": "https://unused.invalid",
              "ca_file": str(token), "broker_token_file": str(token), "grants_file": str(grants),
              "grants_sha256": hashlib.sha256(grants.read_bytes()).hexdigest(),
              "mirror_directory": str(s.gateway.path.parent)}
    path = tmp_path / "config"
    path.write_text(json.dumps(config)); path.chmod(0o600)
    monkeypatch.setenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG", str(path))
    monkeypatch.setenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(http, "BusinessHttpClient", lambda **kw: s.client)
    monkeypatch.setattr(files, "LocalFileStore", lambda _: s.files)
    monkeypatch.setattr(downloads, "configured_workspace_downloads", lambda _: None)
    responses = [AIMessage(content="FAKE W17 succeeded and file delivered")]
    if execute:
        responses = [AIMessage(content="", tool_calls=[{"name": "get_sandbox_task_result",
                     "id": "current-query", "type": "tool_call", "args": {}}]),
                     AIMessage(content="", tool_calls=[{"name": "run_sandbox_task", "id": "current-run",
                     "type": "tool_call", "args": {"input_ref": s.record.file_id, "instruction": s.request.instruction}}]),
                     *responses]
    def build_agent_spec(*, sandbox_tools):
        return messages_spec(create_deep_agent(model=ScriptedModel(responses=responses),
            tools=sandbox_tools, context_schema=Context, state_schema=State))
    monkeypatch.setattr(agent, "build_spec", build_agent_spec)
    spec = sandbox_spec.build_spec()
    ctx = InvocationContext(prompt=s.request.instruction, session_id="s", workspace=tmp_path,
        agents_md=None, state={"current_files": [s.record.to_dict()]},
        runtime_context={"enterprise": s.context.enterprise})
    result = asyncio.run(spec.invoke(ctx))
    assert "FAKE" not in result
    if execute:
        assert "结果已持久化" in result and s.events == ["create", "execute", "destroy"]
        assert s.gateway.snapshot(s.request).state == "succeeded"
    else:
        assert "尚未执行" in result and not s.calls and not s.events
