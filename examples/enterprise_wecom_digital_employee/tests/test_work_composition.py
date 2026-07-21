from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import enterprise_wecom_digital_employee.work_composition as work_composition_module
import pytest
from agentseek_enterprise.runtime import EnterpriseIdentityContext
from agentseek_langchain.ag_ui import application_state_from_state, runtime_context_from_state
from agentseek_wecom.outbound import ArtifactDownloadNotFound, resolve_artifact_download
from agentseek_work import ActiveWorkConflictError, SQLAlchemyWorkRepository, WorkStatus, apply_migrations
from enterprise_wecom_digital_employee.agent import (
    EnterpriseAgentRuntimeContext,
    EnterpriseAgentState,
    _work_observability_config,
)
from enterprise_wecom_digital_employee.pack_loader import (
    FilesystemPackSnapshotStore,
    RestrictedPackLoader,
    build_pack_snapshot,
)
from enterprise_wecom_digital_employee.report_brief import ReportBrief, ResearchScope
from enterprise_wecom_digital_employee.settings import ProjectSettings
from enterprise_wecom_digital_employee.work_composition import (
    IndustryReportWorkComposition,
    WorkCompositionError,
)
from sqlalchemy import create_engine

PROJECT_ROOT = Path(__file__).parents[1]
PACK_ROOT = PROJECT_ROOT / "digital_employees" / "industry-report"
ASSET_REF = "trusted-asset://strategic-report-docx/1.0.0"
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def build_composition(tmp_path: Path) -> IndustryReportWorkComposition:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    apply_migrations(engine)
    repository = SQLAlchemyWorkRepository(engine)

    def resolve_asset(artifact_ref: str) -> Path:
        assert artifact_ref == ASSET_REF
        return PACK_ROOT / "assets" / "neutral-industry-report-v1.docx"

    loaded = RestrictedPackLoader(
        pack_root=PACK_ROOT,
        allowed_entrypoint_package="enterprise_wecom_digital_employee",
        asset_resolver=resolve_asset,
    ).load()
    snapshot_store = FilesystemPackSnapshotStore(tmp_path / "snapshots")
    snapshot = build_pack_snapshot(
        loaded,
        store=snapshot_store,
        created_at=NOW,
    )
    repository.put_pack_snapshot(snapshot)
    return IndustryReportWorkComposition(
        repository=repository,
        loaded_pack=loaded,
        pack_snapshot_id=snapshot.pack_snapshot_id,
        runtime_release="enterprise-wecom-v0.1.0-m1",
        pack_artifact_root=snapshot_store.resolve(snapshot.content_artifact_id),
        clock=lambda: NOW,
        id_factory=lambda: "work_live_001",
    )


def authorized_state() -> dict:
    return {
        "employee_context": {
            "name": "测试员工",
            "oa_account": "not-published",
            "dept_name": "战略发展部",
            "org_path_label": "总部/战略发展部",
        },
        "_langgraph_runtime_context": {
            "enterprise": {
                "version": "v1",
                "tenant_id": "tenant-test",
                "tenant_key": f"hmac-{'1' * 64}",
                "user_key": f"hmac-{'2' * 64}",
                "session_key": f"hmac-{'3' * 64}",
            }
        },
        "current_files": [{"file_id": "file_001"}, {"file_id": "file_001"}],
    }


def message(msgid: str = "message-001") -> dict:
    return {
        "content": "请创建2025年中国证券行业发展研究报告任务",
        "context": {"wecom": {"raw": {"msgid": msgid}}},
    }


def test_enrichment_publishes_safe_profile_and_preserves_enterprise_context(tmp_path: Path) -> None:
    composition = build_composition(tmp_path)
    state = authorized_state()

    composition.enrich_state(message(), "wecom:test", state)

    assert state["digital_employee_status"] == "found"
    profile = state["digital_employee_profile"]
    assert profile["digital_employee_id"] == "industry-report"
    assert profile["owning_org"] == "战略发展部"
    assert "tool_grants" not in profile
    assert "data_scopes" not in profile
    runtime = state["_langgraph_runtime_context"]
    assert runtime["enterprise"]["tenant_id"] == "tenant-test"
    assert runtime["digital_employee"]["pack_snapshot_id"] == composition.pack_snapshot_id
    assert state["work_request_key"].startswith("request_sha256_")
    assert "not-published" not in str(runtime)


def test_enrichment_fails_closed_for_wrong_department_or_missing_identity(tmp_path: Path) -> None:
    composition = build_composition(tmp_path)
    wrong_department = authorized_state()
    wrong_department["employee_context"]["dept_name"] = "信息技术部"
    wrong_department["employee_context"]["org_path_label"] = "总部/信息技术部"

    composition.enrich_state(message(), "wecom:test", wrong_department)
    missing_identity = {"employee_context": {"dept_name": "战略发展部"}}
    composition.enrich_state(message(), "wecom:test", missing_identity)

    assert wrong_department["digital_employee_status"] == "requester_forbidden"
    assert "digital_employee_profile" not in wrong_department
    assert missing_identity["digital_employee_status"] == "requester_forbidden"


def test_factory_creates_idempotent_profile_bound_work_and_publishes_current_state(tmp_path: Path) -> None:
    composition = build_composition(tmp_path)
    state = authorized_state()
    state.update(composition.load_message_state(message(), "wecom:test"))
    composition.enrich_state(message(), "wecom:test", state)

    first = composition.create_report_work(state)
    replay = composition.create_report_work(state)

    assert first.created is True
    assert replay.created is False
    assert replay.item.work_id == first.item.work_id == "work_live_001"
    assert first.item.status is WorkStatus.DRAFT
    assert first.item.pack_snapshot_id == composition.pack_snapshot_id
    assert first.item.skill_set_version == "1.8.3"
    assert first.item.digital_employee_profile_version == "1.8.3"
    assert first.item.pack_version == "1.9.3"
    assert composition.research_template_path.is_relative_to(tmp_path / "snapshots")
    assert first.item.digital_employee_permissions_digest == composition.permissions_digest
    assert first.item.skill_digests
    assert first.item.input_file_ids == ("file_001",)
    assert (
        len({
            first.item.requester_id,
            first.item.reviewer_id,
            first.item.approver_id,
            first.item.data_owner_id,
            first.item.beneficiary_id,
        })
        == 1
    )
    assert first.item.brief == {}
    assert state["current_work"]["work_id"] == first.item.work_id
    assert state["_langgraph_runtime_context"]["work"]["permissions_digest"].startswith("sha256:")


def test_cold_boot_registers_download_resolver_before_any_model_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ProjectSettings(
        _env_file=None,  # ty: ignore[unknown-argument]
        work_enabled=True,
        work_sqlalchemy_url=f"sqlite+pysqlite:///{tmp_path / 'work.sqlite3'}",
        work_auto_migrate=True,
        work_snapshot_path=str(tmp_path / "snapshots"),
        work_template_asset_path=str(PACK_ROOT / "assets" / "neutral-industry-report-v1.docx"),
        work_artifact_path=str(tmp_path / "artifacts"),
        work_artifact_delivery_mode="signed_link",
        work_artifact_public_base_url="https://reports.example.test/artifacts",
        work_runtime_release="enterprise-wecom-v0.1.0-rc",
    )
    monkeypatch.setattr(work_composition_module, "get_settings", lambda: settings)
    work_composition_module.get_work_composition.cache_clear()
    try:
        first = work_composition_module.get_work_composition()
        with pytest.raises(ArtifactDownloadNotFound):
            resolve_artifact_download("delivery_missing", "token")

        work_composition_module.get_work_composition.cache_clear()
        restarted = work_composition_module.get_work_composition()
        with pytest.raises(ArtifactDownloadNotFound):
            resolve_artifact_download("delivery_missing", "token")

        assert restarted is not first
        assert restarted.pack_snapshot_id == first.pack_snapshot_id
    finally:
        work_composition_module.get_work_composition.cache_clear()


def test_current_work_is_scoped_and_loaded_on_follow_up_turn(tmp_path: Path) -> None:
    composition = build_composition(tmp_path)
    first_turn = authorized_state()
    composition.enrich_state(message(), "wecom:test", first_turn)
    composition.create_report_work(first_turn)

    follow_up = authorized_state()
    composition.enrich_state(message("message-002"), "wecom:test", follow_up)
    other_employee = authorized_state()
    other_employee["_langgraph_runtime_context"]["enterprise"]["user_key"] = f"hmac-{'4' * 64}"
    composition.enrich_state(message("message-003"), "wecom:other", other_employee)

    assert follow_up["current_work"]["work_id"] == "work_live_001"
    assert "[CurrentWork]" in follow_up["current_work_context"]
    assert "current_work" not in other_employee


def test_current_work_summary_publishes_current_report_brief_as_distinct_ledger_field(
    tmp_path: Path,
) -> None:
    composition = build_composition(tmp_path)
    state = authorized_state()
    composition.enrich_state(message(), "wecom:test", state)
    composition.create_report_work(state)
    brief = composition.save_report_brief(
        state,
        None,
        ReportBrief(title="证券行业数字化转型报告", target_audience=("公司管理层",)),
    )
    composition.confirm_report_brief(
        state,
        None,
        expected_version=brief.contract_version,
        latest_user_message=f"确认 ReportBrief v{brief.contract_version}。",
    )

    follow_up = authorized_state()
    composition.enrich_state(message("message-002"), "wecom:test", follow_up)

    assert follow_up["current_work"]["report_brief"] == {
        "contract_version": 1,
        "status": "confirmed",
    }
    assert "current_report_brief: v1 status=confirmed" in follow_up["current_work_context"]


def test_report_brief_scope_is_enforced_at_save_and_confirm(tmp_path: Path) -> None:
    composition = build_composition(tmp_path)
    state = authorized_state()
    composition.enrich_state(message(), "wecom:test", state)
    composition.create_report_work(state)

    with pytest.raises(WorkCompositionError, match="SCOPE_MISMATCH"):
        composition.save_report_brief(
            state,
            None,
            ReportBrief(title="新能源汽车行业研究报告", target_audience=("公司管理层",)),
        )

    saved = composition.save_report_brief(
        state,
        None,
        ReportBrief(
            title="新能源汽车对证券行业的影响",
            research_scope=ResearchScope.EXTERNAL_FACTOR_ON_SECURITIES,
            target_audience=("公司管理层",),
        ),
    )
    assert saved.payload["research_scope"] == "external_factor_on_securities"
    assert composition.confirm_report_brief(
        state,
        None,
        expected_version=saved.contract_version,
        latest_user_message=f"确认 ReportBrief v{saved.contract_version}。",
    ).status.value == "confirmed"


def test_distinct_wecom_message_ids_do_not_collapse_identical_content(tmp_path: Path) -> None:
    composition = build_composition(tmp_path)
    first = authorized_state()
    second = authorized_state()
    first.update(composition.load_message_state(message("message-001"), "wecom:test"))
    second.update(composition.load_message_state(message("message-002"), "wecom:test"))
    composition.enrich_state(message(), "wecom:test", first)
    composition.enrich_state(message(), "wecom:test", second)

    assert first["work_request_key"] != second["work_request_key"]


def test_distinct_request_is_rejected_when_same_report_playbook_is_active(tmp_path: Path) -> None:
    composition = build_composition(tmp_path)
    first = authorized_state()
    second = authorized_state()
    composition.enrich_state(message("message-001"), "wecom:test", first)
    composition.create_report_work(first)
    composition.enrich_state(message("message-002"), "wecom:test", second)

    with pytest.raises(ActiveWorkConflictError) as raised:
        composition.create_report_work(second)

    assert raised.value.existing.work_id == "work_live_001"
    assert second["current_work"]["work_id"] == "work_live_001"


def test_graph_boundary_preserves_tool_state_and_passes_private_runtime_as_context(tmp_path: Path) -> None:
    composition = build_composition(tmp_path)
    aggregate_state = authorized_state()
    aggregate_state.update(composition.load_message_state(message(), "wecom:test"))
    composition.enrich_state(message(), "wecom:test", aggregate_state)

    graph_state = application_state_from_state(aggregate_state)
    runtime_mapping = runtime_context_from_state(aggregate_state)

    assert "_langgraph_runtime_context" not in graph_state
    assert graph_state["digital_employee_status"] == "found"
    assert str(graph_state["work_request_key"]).startswith("request_sha256_")
    assert {
        "digital_employee_status",
        "latest_user_message",
        "work_request_key",
    } <= EnterpriseAgentState.__annotations__.keys()
    assert runtime_mapping is not None
    runtime_context = EnterpriseAgentRuntimeContext(
        enterprise=cast("EnterpriseIdentityContext", runtime_mapping["enterprise"]),
        digital_employee=cast("Mapping[str, object] | None", runtime_mapping.get("digital_employee")),
        work=cast("Mapping[str, object] | None", runtime_mapping.get("work")),
    )

    created = composition.create_report_work(graph_state, runtime_context)
    current = composition.current_work(graph_state, runtime_context)

    assert created.created is True
    assert current is not None
    assert current.work_id == created.item.work_id


def test_work_observability_uses_metadata_and_tags_without_top_level_work_id() -> None:
    config = _work_observability_config(
        {"run_name": "agentseek", "tags": ["agentseek"], "metadata": {"session_id": "opaque"}},
        {
            "current_work": {
                "work_id": "work_opaque_001",
                "current_phase": "intake",
                "pack_snapshot_id": "pack_snapshot_sha256_abc",
                "runtime_release": "enterprise-wecom-v0.1.0-m1",
            }
        },
    )

    assert config is not None
    metadata = config["metadata"]
    tags = config["tags"]
    assert isinstance(metadata, dict)
    assert isinstance(tags, list)
    metadata_by_name = {str(key): value for key, value in metadata.items()}
    assert metadata_by_name["work_id"] == "work_opaque_001"
    assert "work:work_opaque_001" in tags
    assert "phase:intake" in tags
    assert "work_id" not in {key for key in config if key != "metadata"}
