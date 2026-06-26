from __future__ import annotations

from typing import Any

from agentseek_enterprise.identity import EmployeeContext
from agentseek_enterprise.plugin import (
    EMPLOYEE_CONTEXT_STATE_KEY,
    EMPLOYEE_IDENTITY_STATE_KEY,
    EnterprisePlugin,
    extract_oa_account,
    format_employee_context_for_prompt,
)
from agentseek_enterprise.memory import SHORT_TERM_MEMORY_STATE_KEY


class FakeIdentityProvider:
    def __init__(self, context: EmployeeContext | None) -> None:
        self.context = context
        self.queries: list[str] = []

    def get_employee_context(self, oa_account: str) -> EmployeeContext | None:
        self.queries.append(oa_account)
        return self.context


def _employee_context() -> EmployeeContext:
    return EmployeeContext(
        user_id="person-1",
        oa_account="chenkang2",
        name="陈康",
        dept_id="dept-1",
        dept_name="财富管理研发团队",
        primary_org_id="company",
        primary_org_name="公司总部",
        org_path=[
            {"id": "company", "no": "HQ", "name": "公司总部", "parent_id": "root", "org_type": "2"},
            {"id": "info-tech", "no": "DEPT-IT", "name": "信息技术部", "parent_id": "company", "org_type": "2"},
            {"id": "dept-1", "no": "DEPT-RD", "name": "财富管理研发团队", "parent_id": "info-tech", "org_type": "2"},
        ],
        org_path_label="公司总部 / 信息技术部 / 财富管理研发团队",
        post="软件开发岗",
        belong_to="1",
        belong_to_label="公司总部",
        role="1",
        role_label="总部员工",
    )


def test_extract_oa_account_reads_flat_and_nested_envelopes() -> None:
    assert extract_oa_account({"from_userid": " chenkang2 "}) == "chenkang2"
    assert extract_oa_account({"context": {"userid": "zhangsan"}}) == "zhangsan"
    assert extract_oa_account({"metadata": {"oa_account": "lisi"}}) == "lisi"


def test_load_state_injects_employee_context(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_IDENTITY_PROVIDER", "dm")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "false")
    plugin = EnterprisePlugin()
    provider = FakeIdentityProvider(_employee_context())
    plugin._provider = provider
    plugin._provider_initialized = True

    state = plugin.load_state({"from_userid": "chenkang2"}, "wecom:chenkang2")

    assert provider.queries == ["chenkang2"]
    assert state[EMPLOYEE_CONTEXT_STATE_KEY]["oa_account"] == "chenkang2"
    assert state[EMPLOYEE_CONTEXT_STATE_KEY]["belong_to_label"] == "公司总部"
    assert state[EMPLOYEE_CONTEXT_STATE_KEY]["org_path_label"] == "公司总部 / 信息技术部 / 财富管理研发团队"
    assert state[EMPLOYEE_IDENTITY_STATE_KEY]["status"] == "found"


def test_load_state_skips_when_identity_disabled(monkeypatch: Any) -> None:
    monkeypatch.delenv("AGENTSEEK_IDENTITY_PROVIDER", raising=False)
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "false")
    plugin = EnterprisePlugin()

    assert plugin.load_state({"from_userid": "chenkang2"}, "s1") == {}


def test_load_state_marks_missing_employee(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_IDENTITY_PROVIDER", "dm")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "false")
    plugin = EnterprisePlugin()
    plugin._provider = FakeIdentityProvider(None)
    plugin._provider_initialized = True

    state = plugin.load_state({"from_userid": "missing"}, "s1")

    assert EMPLOYEE_CONTEXT_STATE_KEY not in state
    assert state[EMPLOYEE_IDENTITY_STATE_KEY]["status"] == "not_found"


def test_format_employee_context_for_prompt() -> None:
    prompt = format_employee_context_for_prompt(_employee_context().to_dict())

    assert "[EmployeeContext]" in prompt
    assert "姓名: 陈康" in prompt
    assert "组织主体: 公司总部" in prompt
    assert "组织路径: 公司总部 / 信息技术部 / 财富管理研发团队" in prompt


def test_short_term_memory_persists_recent_messages(monkeypatch: Any, tmp_path: Any) -> None:
    memory_path = tmp_path / "short-term-memory.sqlite3"
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_SQLITE_PATH", str(memory_path))
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_RECENT_TURNS", "2")

    plugin = EnterprisePlugin()
    plugin.save_state(
        "wecom:chenkang2",
        {},
        {"content": "帮我记一下，我明天去深圳出差"},
        "好的，我记住了。",
    )

    state = plugin.load_state({"content": "我刚才说我要去哪里？"}, "wecom:chenkang2")

    memory = state[SHORT_TERM_MEMORY_STATE_KEY]
    assert memory["session_id"] == "wecom:chenkang2"
    assert [item["role"] for item in memory["recent_messages"]] == ["user", "assistant"]
    assert memory["recent_messages"][0]["content"] == "帮我记一下，我明天去深圳出差"
    assert memory["recent_messages"][1]["content"] == "好的，我记住了。"
    assert plugin.load_state({"content": "hi"}, "wecom:other") == {}


def test_system_prompt_can_include_short_term_memory(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_SYSTEM_PROMPT", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_SYSTEM_PROMPT", "true")
    plugin = EnterprisePlugin()

    prompt = plugin.system_prompt(
        "",
        {
            SHORT_TERM_MEMORY_STATE_KEY: {
                "session_id": "wecom:chenkang2",
                "recent_messages": [
                    {"role": "user", "content": "帮我记一下，我明天去深圳出差"},
                    {"role": "assistant", "content": "好的，我记住了。"},
                ],
            }
        },
    )

    assert prompt is not None
    assert "[ShortTermMemory]" in prompt
    assert "用户: 帮我记一下，我明天去深圳出差" in prompt
