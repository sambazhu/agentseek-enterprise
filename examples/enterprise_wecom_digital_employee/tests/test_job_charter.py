from __future__ import annotations

from pathlib import Path

import enterprise_wecom_digital_employee.agent as agent_module
import pytest
from agentseek_langchain.spec import InvocationContext
from enterprise_wecom_digital_employee.capability_catalog import RuntimeCapabilityAvailability
from enterprise_wecom_digital_employee.job_charter import (
    JobCharterIntent,
    match_job_charter_intent,
    render_job_charter_response,
)
from enterprise_wecom_digital_employee.pack_loader import PackLoadError, RestrictedPackLoader

PROJECT_ROOT = Path(__file__).parents[1]
PACK_ROOT = PROJECT_ROOT / "digital_employees" / "industry-report"
ASSET_REF = "trusted-asset://strategic-report-docx/1.0.0"


def load_profile():
    def resolve_asset(artifact_ref: str) -> Path:
        if artifact_ref != ASSET_REF:
            raise PackLoadError("unknown trusted asset")
        return PACK_ROOT / "assets" / "neutral-industry-report-v1.docx"

    return RestrictedPackLoader(
        pack_root=PACK_ROOT,
        allowed_entrypoint_package="enterprise_wecom_digital_employee",
        asset_resolver=resolve_asset,
    ).load().profile


@pytest.mark.parametrize(
    ("message", "intent"),
    [
        ("你是谁？", JobCharterIntent.IDENTITY),
        ("你 能 做 什 么", JobCharterIntent.CAPABILITIES),
        ("怎么使用你。", JobCharterIntent.USAGE),
        (
            "from_userid=opaque|channel=$wecom|chat_id=opaque\n"
            "---Date: 2026-07-22---\n你有哪些服务",
            JobCharterIntent.CAPABILITIES,
        ),
    ],
)
def test_job_charter_intents_are_exact_and_accept_authenticated_wecom_envelope(
    message: str,
    intent: JobCharterIntent,
) -> None:
    assert match_job_charter_intent(message) is intent


@pytest.mark.parametrize(
    "message",
    [
        "我是谁",
        "你是谁以及帮我写报告",
        "请问你能做天气查询吗",
        "---Date: 2026-07-22---\n你是谁",
    ],
)
def test_job_charter_does_not_capture_employee_identity_or_ambiguous_requests(message: str) -> None:
    assert match_job_charter_intent(message) is None


def test_job_charter_responses_are_profile_backed_and_explain_the_formal_workflow() -> None:
    profile = load_profile()

    identity = render_job_charter_response(profile, JobCharterIntent.IDENTITY)
    capabilities = render_job_charter_response(profile, JobCharterIntent.CAPABILITIES)
    usage = render_job_charter_response(profile, JobCharterIntent.USAGE)

    assert "战略发展部数字员工" in identity
    assert "DE-SD-001" in identity
    assert profile.mission in identity
    assert "证券行业正式报告" in capabilities
    assert "需求澄清与 ReportBrief 确认" in capabilities
    assert "DOCX 渲染、发布与交付" in capabilities
    assert "分析你授权的文件" in capabilities
    assert "检索已配置的部门知识" in capabilities
    assert "明确同意后使用已配置的外部数据或公开搜索" in capabilities
    assert "analyze_file" not in capabilities
    assert "department-knowledge" not in capabilities
    assert "关键版本需要你明确确认" in capabilities
    assert "正式服务\n" in capabilities
    assert "\n\n协助能力\n" in capabilities
    assert "\n\n执行边界\n" in capabilities
    assert "不会静默启动任务" in usage
    assert profile.service_catalog[0].example_requests[0] in usage


def test_job_charter_capabilities_do_not_claim_services_missing_from_runtime() -> None:
    profile = load_profile()
    availability = RuntimeCapabilityAvailability(
        file_analysis=True,
        department_knowledge=False,
        licensed_external_data=False,
        public_search=True,
    )

    response = render_job_charter_response(
        profile,
        JobCharterIntent.CAPABILITIES,
        capabilities=availability,
    )

    assert "分析你授权的文件" in response
    assert "已配置的公开搜索" in response
    assert "部门知识" not in response
    assert "外部数据" not in response


def test_agent_direct_response_requires_loaded_employee_profile_and_emits_safe_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = load_profile()
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "agentseek_enterprise.observability.emit_enterprise_event",
        lambda name, **payload: events.append((name, payload)),
    )
    denied = InvocationContext(
        prompt="你是谁",
        session_id="wecom:test",
        state={"digital_employee_status": "requester_forbidden"},
        workspace=tmp_path,
        agents_md=None,
    )
    allowed = InvocationContext(
        prompt="你是谁",
        session_id="wecom:test",
        state={"digital_employee_status": "found", "latest_user_message": "你是谁"},
        workspace=tmp_path,
        agents_md=None,
    )

    assert agent_module._job_charter_direct_response(denied, profile) is None
    response = agent_module._job_charter_direct_response(allowed, profile)
    assert response is not None and "战略发展部数字员工" in response
    assert events == [
        (
            "digital_employee_service_discovery",
            {
                "status": "succeeded",
                "session_id": "wecom:test",
                "digital_employee_id": "industry-report",
                    "profile_version": "1.12.0",
                "intent": "identity",
            },
        )
    ]
