import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from enterprise_wecom_digital_employee.mcp_compat import (
    bind_kb_arguments,
    effective_schema,
    local_transport_overrides,
)
from enterprise_wecom_digital_employee.production_mcp import bind_oa_schema, prepare_production_arguments
from jsonschema import ValidationError


def runtime(kind="single"):
    from agentseek_enterprise.runtime import EnterpriseRuntimeSettings

    return SimpleNamespace(
        context=SimpleNamespace(enterprise={"conversation_type": kind, "user_key": EnterpriseRuntimeSettings.from_env().scoped_key("employee", "alice")}),
        state={"employee_context": {"oa_account": "alice"}},
    )


def kb_schema(code="e3n5qeo1ec2w"):
    return {"type": "object", "properties": {
        "query": {"type": "string", "minLength": 1},
        "kbCode": {"type": "string", "default": code},
        "limit": {"type": "integer", "default": 10, "minimum": 1},
        "chunkConfig": {"type": "object", "default": {"next": 1}},
    }, "required": ["query"]}


@pytest.mark.parametrize("server,code", [
    ("信息技术部知识库", "e3n5qeo1ec2w"), ("信息技术部知识库", "i4ucnjkj5b9y"),
    ("战略发展部知识库", "urzi680on449"), ("人力资源知识库", "z1sdxq9hme78"),
])
def test_kb_materializes_defaults_without_mutating_schema(server, code):
    schema = kb_schema(code)
    original = deepcopy(schema)
    result = bind_kb_arguments(server, schema, {"query": "test", "limit": None})
    assert result == {"query": "test", "kbCode": code, "limit": 10, "chunkConfig": {"next": 1}}
    result["chunkConfig"]["next"] = 9
    assert schema == original


@pytest.mark.parametrize("args", [{"query": "test", "kbCode": "other"}, {"query": "test", "limit": 0}, {"query": "test", "limit": True}, {}])
def test_kb_rejects_invalid_or_cross_binding(args):
    with pytest.raises((ValueError, ValidationError)):
        bind_kb_arguments("信息技术部知识库", kb_schema(), args)


def test_oa_dangling_required_and_null_default():
    schema = {"properties": {"oa_account": {"type": "string"}, "top_count": {"type": "integer", "default": 3}}, "required": ["login_name"]}
    assert effective_schema("znsh", "查询最近费用报销流程", schema)["required"] == ["oa_account"]
    assert schema["required"] == ["login_name"]
    assert bind_oa_schema("查询最近费用报销流程", {"top_count": None}, schema, runtime()) == {"oa_account": "alice", "top_count": 3}
    assert prepare_production_arguments("znsh", "查询最近费用报销流程", {"oa_account": "bob"}, runtime())[1]
    assert prepare_production_arguments("znsh", "查询最近费用报销流程", {}, runtime("group"))[1]
    assert effective_schema("unrelated", "查询最近费用报销流程", schema) == schema


def test_override_survives_platform_rewrite_without_disk_secret(tmp_path, monkeypatch):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"tavily": {"headers_from_env": {"Authorization": "TEST_MCP_AUTH"}}}))
    monkeypatch.setenv("AGENTSEEK_MCP_LOCAL_OVERRIDES", str(path))
    monkeypatch.setenv("TEST_MCP_AUTH", "test-only-secret")
    for old in ("placeholder", "rewritten-placeholder"):
        original = {"tavily": {"url": "https://example.invalid", "headers": {"Authorization": old}}}
        assert local_transport_overrides(original)["tavily"]["headers"]["Authorization"] == "test-only-secret"
        assert original["tavily"]["headers"]["Authorization"] == old
    assert "test-only-secret" not in path.read_text()
    monkeypatch.delenv("TEST_MCP_AUTH")
    with pytest.raises(ValueError, match="configuration unavailable"):
        local_transport_overrides(original)


def test_shared_call_path_defaults_and_backend_failure(monkeypatch):
    from enterprise_wecom_digital_employee import tools

    calls, audits = [], []

    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def list_tools(self):
            return [SimpleNamespace(name="search", inputSchema=kb_schema())]
        async def call_tool(self, name, arguments):
            calls.append(arguments)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="kbIndexCode不存在")])

    monkeypatch.setattr(tools, "Client", Client)
    monkeypatch.setattr(tools, "_read_mcp_servers", lambda: {"信息技术部知识库": {"url": "https://example.invalid"}})
    monkeypatch.setattr(tools, "_mcp_policy", lambda: SimpleNamespace(
        evaluate=lambda *a, **kw: SimpleNamespace(action="allow", risk="read", reason="test"),
        audit=lambda **kw: audits.append(kw),
    ))
    response = asyncio.run(tools.call_mcp_tool("信息技术部知识库", "search", {"query": "test"}))
    assert calls[0]["kbCode"] == "e3n5qeo1ec2w"
    assert "不表示没有" in response
    assert audits[-1]["action"] == "failed"
    asyncio.run(tools.call_mcp_tool("信息技术部知识库", "search", {"query": "test", "kbCode": "other"}))
    assert len(calls) == 1
