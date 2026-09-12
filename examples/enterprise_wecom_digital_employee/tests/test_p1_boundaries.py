import asyncio
from types import SimpleNamespace

import pytest
from enterprise_wecom_digital_employee.identity_response import identity_response
from enterprise_wecom_digital_employee.production_mcp import bind_oa_schema, prepare_production_arguments
from langchain_core.tools import StructuredTool


def runtime(kind="single", account="alice"):
    from agentseek_enterprise.runtime import EnterpriseRuntimeSettings

    return SimpleNamespace(context=SimpleNamespace(
        enterprise={"conversation_type": kind, "user_key": EnterpriseRuntimeSettings.from_env().scoped_key("employee", account)},
    ), state={"employee_context": {"oa_account": account}})


def test_identity_is_runtime_backed_without_work_or_model():
    assert identity_response("我是谁", {"employee_context": {"name": "测试员工", "oa_account": "private", "dept_name": "测试部"}}) == "你是测试员工，所属组织：测试部。"
    assert "未能完整识别" in identity_response("我是谁", {})
    assert identity_response("我是谁，帮我写报告", {}) is None


def test_personal_mcp_fails_closed_before_call_for_group_missing_identity_and_other_user():
    for rt, args in ((runtime("group"), {}), (runtime("unknown"), {}), (runtime(account=""), {}), (runtime(), {"login_name": "bob"})):
        prepared, refusal = prepare_production_arguments("OA流程助手", "查询员工会议日程", args, rt)
        assert refusal and prepared == {}


def test_oa_schema_binds_authenticated_identity_and_does_not_invent_field_names():
    args, refusal = prepare_production_arguments("OA流程助手", "查询员工会议日程", {}, runtime())
    assert refusal is None
    schema = {"properties": {"login_name": {"type": "string"}, "top_count": {"type": "integer"}}, "required": ["login_name", "top_count"]}
    assert bind_oa_schema("查询员工会议日程", args, schema, runtime()) == {"login_name": "alice", "top_count": 3}


def test_reimbursement_id_alone_does_not_authorize_details():
    _, refusal = prepare_production_arguments("OA流程助手", "查询费用报销流程审批轨迹", {"flow_id": "other-flow"}, runtime())
    assert refusal


def test_state_account_cannot_impersonate_the_authenticated_runtime_user():
    rt = runtime()
    rt.state["employee_context"]["oa_account"] = "bob"
    _, refusal = prepare_production_arguments("OA流程助手", "查询员工会议日程", {}, rt)
    assert refusal


def test_runtime_identity_is_not_a_model_supplied_tool_argument():
    from enterprise_wecom_digital_employee.tools import call_mcp_tool

    tool = StructuredTool.from_function(coroutine=call_mcp_tool)
    assert set(tool.tool_call_schema.model_json_schema()["properties"]) == {
        "server_name", "tool_name", "arguments", "confirmed",
    }


def test_real_graph_preserves_identity_and_injects_tool_runtime():
    from enterprise_wecom_digital_employee.agent import EnterpriseAgentRuntimeContext, EnterpriseAgentState
    from enterprise_wecom_digital_employee.production_mcp import _authenticated_account
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph
    from langgraph.prebuilt import ToolNode, ToolRuntime

    @tool
    def identity_probe(runtime: ToolRuntime[EnterpriseAgentRuntimeContext]) -> str:
        """Read the authenticated test identity."""
        return _authenticated_account(runtime)
    graph = StateGraph(EnterpriseAgentState, context_schema=EnterpriseAgentRuntimeContext)
    graph.add_node("tools", ToolNode([identity_probe]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    rt = runtime()
    result = graph.compile().invoke({
        **rt.state,
        "messages": [AIMessage(content="", tool_calls=[{"id": "test-call", "name": "identity_probe", "args": {}}])],
    }, context=EnterpriseAgentRuntimeContext(enterprise=rt.context.enterprise))
    assert result["messages"][-1].content == "alice"


def test_cancel_and_automatic_delegation_reject_questions_quotes_and_negation():
    from enterprise_wecom_digital_employee.work_commands import (
        explicitly_cancels_current_work,
        requests_automatic_draft,
    )

    for command in ("取消当前任务？", "不要取消当前任务", "他说取消当前任务", "“取消当前任务”"):
        assert not explicitly_cancels_current_work(command)
    for command in ("按已确认需求自动研究并生成初稿？", "不要按已确认需求自动研究并生成初稿", "确认 ReportBrief v1 并自动研究生成初稿？"):
        assert not requests_automatic_draft(command)
    assert requests_automatic_draft("确认 ReportBrief v1 并自动研究生成初稿")


def test_oa_binding_rejects_invalid_schema_values():
    schema = {"properties": {"login_name": {"type": "string"}, "top_count": {"type": "integer"}}}
    for value in ("all", -1, 21, True):
        with pytest.raises(ValueError):
            bind_oa_schema("查询员工会议日程", {"top_count": value}, schema, runtime())
    with pytest.raises(ValueError, match="requester binding"):
        bind_oa_schema("查询员工会议日程", {}, {"properties": {}}, runtime())


def test_oa_personal_refusals_make_zero_remote_calls(monkeypatch):
    from enterprise_wecom_digital_employee import tools

    audits = []
    monkeypatch.setattr(tools, "_read_mcp_servers", lambda: {"OA流程助手": {"url": "https://unused.invalid"}})
    monkeypatch.setattr(tools, "_mcp_policy", lambda: SimpleNamespace(audit=lambda **kw: audits.append(kw)))
    def forbidden(*args, **kwargs):
        pytest.fail("refused requests must not connect to MCP")
    monkeypatch.setattr(tools, "Client", forbidden)
    for rt, args in ((runtime("group"), {}), (runtime(account=""), {}), (runtime(), {"login_name": "bob"})):
        result = asyncio.run(tools.call_mcp_tool("OA流程助手", "查询员工会议日程", args, runtime=rt))
        assert result
    assert len(audits) == 3
    assert all(item["arguments"] == {} for item in audits)


def test_oa_remote_schema_and_audit_do_not_leak_personal_payload(monkeypatch):
    from enterprise_wecom_digital_employee import tools

    audits, calls = [], []
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def list_tools(self):
            return [SimpleNamespace(name="查询员工会议日程", inputSchema={"properties": {"login_name": {"type": "string"}}})]
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="personal-meeting-result")])
    monkeypatch.setattr(tools, "Client", Client)
    monkeypatch.setattr(tools, "_read_mcp_servers", lambda: {"OA流程助手": {"url": "https://unused.invalid"}})
    monkeypatch.setattr(tools, "_mcp_policy", lambda: SimpleNamespace(
        evaluate=lambda *a, **kw: SimpleNamespace(action="allow", risk="read", reason="test"),
        audit=lambda **kw: audits.append(kw),
    ))
    response = asyncio.run(tools.call_mcp_tool("OA流程助手", "查询员工会议日程", {}, runtime=runtime()))
    assert "personal-meeting-result" in response
    assert calls == [("查询员工会议日程", {"login_name": "alice"})]
    assert "alice" not in str(audits)
    assert "personal-meeting-result" not in str(audits)


def test_work_disabled_discovery_does_not_advertise_an_enabled_formal_service(monkeypatch):
    from pathlib import Path

    from enterprise_wecom_digital_employee import identity_response as module

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        work_enabled=False, production_mcp_enabled=False,
        resolved_mcp_config_path=lambda: Path("/nonexistent-test-config"),
    ))
    for command in ("你是谁", "你能做什么", "怎么使用你"):
        response = module.identity_response(command, {})
        assert "正式报告工作流未启用" in response
        assert "当前正式服务" not in response
        assert "现在可以：" in response
        assert "按配置提供：" in response
        assert "尚未启用：" in response


def test_business_tools_have_injected_runtime_and_fixed_knowledge_collection(monkeypatch):
    from pathlib import Path

    from enterprise_wecom_digital_employee import capability_catalog, production_mcp, settings, tools

    monkeypatch.setattr(settings, "get_settings", lambda: SimpleNamespace(
        production_mcp_enabled=True, it_policy_kb_code="configured-policy",
        it_regulation_kb_code="configured-regulation", resolved_mcp_config_path=lambda: Path("/unused"),
    ))
    monkeypatch.setattr(capability_catalog, "configured_mcp_server_names", lambda path: {"OA流程助手", "信息技术部知识库"})
    calls = []
    async def invoke(*args):
        calls.append(args)
        return "verified test source"
    monkeypatch.setattr(tools, "call_mcp_tool", invoke)
    business_tools = {item.name: item for item in production_mcp.production_service_tools()}
    assert set(business_tools) == {"describe_oa_query", "query_my_oa", "search_it_policy"}
    for item in business_tools.values():
        assert "runtime" not in item.tool_call_schema.model_json_schema()["properties"]
    rt = runtime()
    # An unrelated active task does not prevent an ordinary direct knowledge question.
    rt.state["current_work"] = {"work_id": "test"}
    search = business_tools["search_it_policy"].coroutine
    assert asyncio.run(search("测试问题", "制度", rt)) == "verified test source"
    assert calls[0][2] == {"query": "测试问题", "kbCode": "configured-policy"}
    rt.state["playbook_route"] = {"route_status": "selected"}
    assert "工作流检索入口" in asyncio.run(search("测试问题", "制度", rt))
    assert len(calls) == 1
