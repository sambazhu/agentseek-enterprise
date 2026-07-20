from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from agentseek_enterprise.runtime import EnterpriseRuntimeSettings
from agentseek_work import (
    LATEST_SCHEMA_VERSION,
    ArtifactRecord,
    CreateWorkResult,
    InteractionRoute,
    PublicationRecord,
    PublicationStatus,
    RouteRequest,
    SideEffect,
    SourceRecord,
    SourceType,
    SQLAlchemyWorkRepository,
    ToolContract,
    ToolContractRegistry,
    WorkBudget,
    WorkContractConflictError,
    WorkContractSnapshot,
    WorkContractStatus,
    WorkItem,
    WorkMode,
    WorkNotFoundError,
    apply_migrations,
    current_schema_version,
    decide_interaction_route,
)
from bub.envelope import content_of, field_of
from bub.types import Envelope, State
from sqlalchemy import Engine, create_engine

from enterprise_wecom_digital_employee.pack_loader import (
    DigitalEmployeeProfile,
    FilesystemPackSnapshotStore,
    LoadedPackManifest,
    RestrictedPackLoader,
    build_pack_snapshot,
)
from enterprise_wecom_digital_employee.report_approval import (
    REPORT_APPROVAL_CONTRACT_TYPE,
    ReportApproval,
    approval_message_digest,
    approval_state,
    explicitly_approves_report_draft,
    explicitly_requests_report_approval,
)
from enterprise_wecom_digital_employee.report_artifact import (
    REPORT_ARTIFACT_FORMAT_DOCX,
    REPORT_ARTIFACT_MEDIA_TYPE_DOCX,
    REPORT_ARTIFACT_TYPE,
    ContentAddressedArtifactStore,
    ReportArtifactError,
    artifact_id,
    contract_digest,
    explicitly_requests_report_artifact,
    render_report_docx,
)
from enterprise_wecom_digital_employee.report_brief import (
    REPORT_BRIEF_CONTRACT_TYPE,
    ReportBrief,
    explicitly_confirms_report_brief,
    validate_report_brief_scope,
)
from enterprise_wecom_digital_employee.report_draft import (
    REPORT_DRAFT_CONTRACT_TYPE,
    ReportDraft,
    claim_set_digest,
    evidence_set_digest,
    explicitly_confirms_report_draft,
    report_draft_digest,
)
from enterprise_wecom_digital_employee.report_outline import (
    REPORT_OUTLINE_CONTRACT_TYPE,
    ReportOutline,
    explicitly_confirms_report_outline,
    source_set_digest,
)
from enterprise_wecom_digital_employee.report_publication import (
    explicitly_requests_report_publication,
    publication_id,
)
from enterprise_wecom_digital_employee.research_gap_decision import (
    RESEARCH_GAP_DECISION_CONTRACT_TYPE,
    ResearchGapDecision,
    explicitly_selects_gap_action,
)
from enterprise_wecom_digital_employee.settings import PROJECT_ROOT, ProjectSettings, get_settings

_SCOPED_KEY_RE = re.compile(r"^(?:hmac|sha256)-[a-f0-9]{64}$")
_PACK_ROOT = PROJECT_ROOT / "digital_employees" / "industry-report"
_ASSET_REF = "trusted-asset://strategic-report-docx/1.0.0"
_CREATE_REPORT_TOOL = "create_industry_report_work"
_DIGITAL_EMPLOYEE_STATUS_KEY = "digital_employee_status"
_WORK_REQUEST_KEY = "work_request_key"
_BUDGET_ID = "industry-report-budget-v1"
_DEFAULT_BUDGET = WorkBudget(
    max_model_calls=30,
    max_input_tokens=300_000,
    max_output_tokens=80_000,
    max_external_queries=100,
    max_phase_duration_seconds=900,
    max_work_duration_seconds=3600,
    max_retry_count=2,
)


class WorkCompositionError(RuntimeError):
    """Raised when the isolated digital-employee composition fails closed."""


@dataclass(frozen=True, slots=True)
class WorkItemFactory:
    loaded_pack: LoadedPackManifest
    pack_snapshot_id: str
    budget_id: str
    runtime_release: str
    permissions_digest: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    id_factory: Callable[[], str] = lambda: f"work_{uuid4().hex}"

    def create(
        self,
        *,
        tenant_id: str,
        requester_key: str,
        idempotency_key: str,
        input_file_ids: tuple[str, ...],
    ) -> WorkItem:
        profile = self.loaded_pack.profile
        playbook_id, playbook_version = _single_playbook_ref(profile)
        now = self.clock()
        return WorkItem(
            work_id=self.id_factory(),
            tenant_id=tenant_id,
            digital_employee_id=profile.digital_employee_id,
            digital_employee_profile_version=profile.profile_version,
            digital_employee_permissions_digest=self.permissions_digest,
            pack_id=self.loaded_pack.pack_id,
            pack_version=self.loaded_pack.pack_version,
            pack_snapshot_id=self.pack_snapshot_id,
            runtime_release=self.runtime_release,
            requester_id=requester_key,
            reviewer_id=requester_key,
            approver_id=requester_key,
            data_owner_id=requester_key,
            beneficiary_id=requester_key,
            playbook_id=playbook_id,
            playbook_version=playbook_version,
            budget_id=self.budget_id,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
            skill_set_version=profile.profile_version,
            skill_digests=self.loaded_pack.skill_digests,
            input_file_ids=input_file_ids,
        )


class IndustryReportWorkComposition:
    def __init__(
        self,
        *,
        repository: SQLAlchemyWorkRepository,
        loaded_pack: LoadedPackManifest,
        pack_snapshot_id: str,
        runtime_release: str,
        pack_artifact_root: Path | None = None,
        artifact_store_root: Path | None = None,
        template_asset_path: Path | None = None,
        budget_id: str = _BUDGET_ID,
        budget: WorkBudget = _DEFAULT_BUDGET,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: f"work_{uuid4().hex}",
    ) -> None:
        self.repository = repository
        self.loaded_pack = loaded_pack
        self.profile = loaded_pack.profile
        self.playbook_id, playbook_version = _single_playbook_ref(self.profile)
        self.playbook = next(
            playbook
            for playbook in loaded_pack.playbooks
            if playbook.playbook_id == self.playbook_id and playbook.version == playbook_version
        )
        self.pack_artifact_root = pack_artifact_root or loaded_pack.pack_root
        self.artifact_store = ContentAddressedArtifactStore(
            artifact_store_root or loaded_pack.pack_root / "runtime" / "work-artifacts"
        )
        self.template_asset_path = (
            template_asset_path
            or loaded_pack.pack_root / "assets" / "neutral-industry-report-v1.docx"
        )
        self.research_template_path = self.pack_artifact_root / self.playbook.research_template_path
        self.pack_snapshot_id = pack_snapshot_id
        self.permissions_digest = _permissions_digest(self.profile)
        self.skill_set_digest = _skill_set_digest(loaded_pack)
        self._contracts = ToolContractRegistry((
            ToolContract(
                name=_CREATE_REPORT_TOOL,
                work_mode=WorkMode.REQUIRED,
                side_effect=SideEffect.WRITE,
                supports_idempotency=True,
            ),
        ))
        self._factory = WorkItemFactory(
            loaded_pack=loaded_pack,
            pack_snapshot_id=pack_snapshot_id,
            budget_id=budget_id,
            runtime_release=runtime_release,
            permissions_digest=self.permissions_digest,
            clock=clock,
            id_factory=id_factory,
        )
        repository.put_budget(budget_id, budget)

    def load_message_state(self, message: Envelope, session_id: str) -> State:
        del session_id
        return {"_work_message_key": _message_scope_key(message)}

    def enrich_state(self, message: Envelope, session_id: str, state: State) -> None:
        del session_id
        status = self._authorization_status(state)
        state[_DIGITAL_EMPLOYEE_STATUS_KEY] = status
        if status != "found":
            return

        enterprise = _enterprise_context(state)
        if enterprise is None:  # guarded by _authorization_status
            state[_DIGITAL_EMPLOYEE_STATUS_KEY] = "requester_forbidden"
            return
        requester_key = str(enterprise["user_key"])
        tenant_id = str(enterprise["tenant_id"])
        state["digital_employee_profile"] = _profile_summary(self.profile)
        state["_work_binding_digest"] = f"sha256:{sha256(self.pack_snapshot_id.encode()).hexdigest()}"
        state["_work_permissions_digest"] = self.permissions_digest
        state["_work_skill_set_digest"] = self.skill_set_digest
        state[_WORK_REQUEST_KEY] = _request_key(message, enterprise, state)
        _merge_runtime_context(
            state,
            "digital_employee",
            {
                "version": "v1",
                "digital_employee_id": self.profile.digital_employee_id,
                "profile_version": self.profile.profile_version,
                "permissions_digest": self.permissions_digest,
                "pack_snapshot_id": self.pack_snapshot_id,
            },
        )

        current = self.repository.find_active_work(
            tenant_id=tenant_id,
            requester_id=requester_key,
            digital_employee_id=self.profile.digital_employee_id,
            playbook_id=self.playbook_id,
        )
        if current is not None:
            self._publish_current_work(state, current)

    def create_report_work(
        self,
        state: Mapping[str, object],
        runtime_context: object | None = None,
    ) -> CreateWorkResult:
        if state.get(_DIGITAL_EMPLOYEE_STATUS_KEY) != "found":
            raise WorkCompositionError("当前员工未获授权使用行业报告数字员工。")
        enterprise = _enterprise_context(runtime_context if runtime_context is not None else state)
        if enterprise is None:
            raise WorkCompositionError("企业身份上下文不可用，未创建任务。")
        request_key = _clean(state.get(_WORK_REQUEST_KEY))
        if not request_key:
            raise WorkCompositionError("当前消息缺少幂等请求标识，未创建任务。")

        contract = self._contracts.resolve(_CREATE_REPORT_TOOL)
        decision = decide_interaction_route(
            RouteRequest(
                tool_contract=contract,
                playbook_work_mode=WorkMode.REQUIRED,
                produces_formal_artifact=True,
                spans_turns_systems_or_phases=True,
                requester_requires_tracking=True,
                idempotency_enabled=True,
            )
        )
        if decision.route is not InteractionRoute.WORK_ITEM:
            raise WorkCompositionError("正式报告路由合同拒绝 DirectTurn。")

        item = self._factory.create(
            tenant_id=str(enterprise["tenant_id"]),
            requester_key=str(enterprise["user_key"]),
            idempotency_key=request_key,
            input_file_ids=_current_file_ids(state),
        )
        result = self.repository.create_work(item)
        if isinstance(state, dict):
            self._publish_current_work(cast("dict[str, Any]", state), result.item)
        return result

    def current_work(
        self,
        state: Mapping[str, object],
        runtime_context: object | None = None,
    ) -> WorkItem | None:
        enterprise = _enterprise_context(runtime_context if runtime_context is not None else state)
        if enterprise is None or state.get(_DIGITAL_EMPLOYEE_STATUS_KEY) != "found":
            return None
        return self.repository.find_active_work(
            tenant_id=str(enterprise["tenant_id"]),
            requester_id=str(enterprise["user_key"]),
            digital_employee_id=self.profile.digital_employee_id,
            playbook_id=self.playbook_id,
        )

    def current_work_summary(
        self,
        state: Mapping[str, object],
        runtime_context: object | None = None,
    ) -> dict[str, object] | None:
        """Return the current WorkItem with independently named contract versions."""

        item = self.current_work(state, runtime_context)
        return self._ledger_summary(item) if item is not None else None

    def save_report_brief(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
        brief: ReportBrief,
    ) -> WorkContractSnapshot:
        item = self.current_work(state, runtime_context)
        if item is None:
            raise WorkCompositionError("当前员工没有可绑定 ReportBrief 的进行中报告任务。")
        self._validate_report_brief_scope(brief)
        current = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_BRIEF_CONTRACT_TYPE,
        )
        if current is not None and dict(current.payload) == brief.to_payload():
            saved = current
        else:
            version = 1 if current is None else current.contract_version + 1
            candidate = brief.to_contract(
                work_id=item.work_id,
                tenant_id=item.tenant_id,
                contract_version=version,
                created_by=item.requester_id,
                created_at=self._factory.clock(),
            )
            saved = (
                self.repository.create_work_contract(candidate)
                if current is None
                else self.repository.revise_work_contract(candidate)
            )
        if isinstance(state, dict):
            self._publish_current_work(cast("dict[str, Any]", state), item)
        return saved

    def confirm_report_brief(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
        *,
        expected_version: int,
        latest_user_message: str,
    ) -> WorkContractSnapshot:
        item = self.current_work(state, runtime_context)
        if item is None:
            raise WorkCompositionError("当前员工没有可确认 ReportBrief 的进行中报告任务。")
        current = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_BRIEF_CONTRACT_TYPE,
        )
        if current is None:
            raise WorkCompositionError("当前任务尚未形成 ReportBrief。")
        brief = ReportBrief.from_contract(current)
        self._validate_report_brief_scope(brief)
        if not brief.is_confirmable:
            raise WorkCompositionError("ReportBrief 仍缺少目标受众，不能确认。")
        if current.contract_version != expected_version:
            raise WorkCompositionError("ReportBrief 版本不匹配，请重新展示当前版本后再确认。")
        if current.status is WorkContractStatus.CONFIRMED:
            confirmed = current
        else:
            if not explicitly_confirms_report_brief(latest_user_message, expected_version=expected_version):
                raise WorkCompositionError(
                    f"员工最新消息未显式确认 ReportBrief v{expected_version}，不能确认或启动正式研究。"
                )
            confirmed = self.repository.confirm_work_contract(
                tenant_id=item.tenant_id,
                work_id=item.work_id,
                contract_type=REPORT_BRIEF_CONTRACT_TYPE,
                expected_contract_version=expected_version,
                confirmed_by=item.requester_id,
                confirmed_at=self._factory.clock(),
            )
        if isinstance(state, dict):
            self._publish_current_work(cast("dict[str, Any]", state), item)
        return confirmed

    def _validate_report_brief_scope(self, brief: ReportBrief) -> None:
        try:
            validate_report_brief_scope(
                brief,
                allowed_scopes=self.playbook.allowed_research_scopes,
                topic_anchor_terms=self.playbook.topic_anchor_terms,
            )
        except ValueError as exc:
            raise WorkCompositionError(str(exc)) from exc

    def save_report_outline(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
        outline: ReportOutline,
    ) -> WorkContractSnapshot:
        """Persist one deterministic, source-backed outline as a provisional contract."""

        item = self.current_work(state, runtime_context)
        if item is None:
            raise WorkCompositionError("当前员工没有可绑定 ReportOutline 的进行中报告任务。")
        self._require_outline_brief_binding(item, outline)
        current = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_OUTLINE_CONTRACT_TYPE,
        )
        if current is not None and dict(current.payload) == outline.to_payload():
            saved = current
        else:
            version = 1 if current is None else current.contract_version + 1
            candidate = outline.to_contract(
                work_id=item.work_id,
                tenant_id=item.tenant_id,
                contract_version=version,
                created_by=item.requester_id,
                created_at=self._factory.clock(),
            )
            saved = (
                self.repository.create_work_contract(candidate)
                if current is None
                else self.repository.revise_work_contract(candidate)
            )
        if isinstance(state, dict):
            self._publish_current_work(cast("dict[str, Any]", state), item)
        return saved

    def confirm_report_outline(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
        *,
        expected_version: int,
        latest_user_message: str,
    ) -> WorkContractSnapshot:
        """Confirm an outline only while its Brief, evidence set, and decision remain current."""

        item = self.current_work(state, runtime_context)
        if item is None:
            raise WorkCompositionError("当前员工没有可确认 ReportOutline 的进行中报告任务。")
        current = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_OUTLINE_CONTRACT_TYPE,
        )
        if current is None:
            raise WorkCompositionError("当前任务尚未形成 ReportOutline。")
        if current.contract_version != expected_version:
            raise WorkCompositionError("ReportOutline 版本不匹配，请重新展示当前版本后再确认。")
        try:
            outline = ReportOutline.from_contract(current)
        except (TypeError, ValueError) as exc:
            raise WorkCompositionError("当前 ReportOutline 合同无效，不能确认。") from exc
        self._require_outline_brief_binding(item, outline)
        self._require_outline_evidence_current(item, outline)
        if current.status is WorkContractStatus.CONFIRMED:
            confirmed = current
        else:
            if not explicitly_confirms_report_outline(latest_user_message, expected_version=expected_version):
                raise WorkCompositionError(
                    f"员工最新消息未显式确认 ReportOutline v{expected_version}，不能进入初稿阶段。"
                )
            confirmed = self.repository.confirm_work_contract(
                tenant_id=item.tenant_id,
                work_id=item.work_id,
                contract_type=REPORT_OUTLINE_CONTRACT_TYPE,
                expected_contract_version=expected_version,
                confirmed_by=item.requester_id,
                confirmed_at=self._factory.clock(),
            )
        if isinstance(state, dict):
            self._publish_current_work(cast("dict[str, Any]", state), item)
        return confirmed

    def current_confirmed_report_outline(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
    ) -> tuple[WorkItem, WorkContractSnapshot, ReportOutline]:
        """Return the current confirmed outline after revalidating all frozen bindings."""

        item = self.current_work(state, runtime_context)
        if item is None:
            raise WorkCompositionError("当前员工没有可用于初稿的进行中报告任务。")
        contract = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_OUTLINE_CONTRACT_TYPE,
        )
        if contract is None or contract.status is not WorkContractStatus.CONFIRMED:
            raise WorkCompositionError("当前任务没有已确认的 ReportOutline，不能准备或保存初稿。")
        try:
            outline = ReportOutline.from_contract(contract)
        except (TypeError, ValueError) as exc:
            raise WorkCompositionError("当前 ReportOutline 合同无效，不能进入初稿阶段。") from exc
        self._require_outline_brief_binding(item, outline)
        self._require_outline_evidence_current(item, outline)
        return item, contract, outline

    def save_report_draft(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
        draft: ReportDraft,
    ) -> WorkContractSnapshot:
        """Persist one ledger-backed Markdown draft as a provisional contract."""

        item, outline_contract, outline = self.current_confirmed_report_outline(state, runtime_context)
        if (
            draft.report_outline_version != outline_contract.contract_version
            or draft.report_brief_version != outline.report_brief_version
            or draft.report_title != outline.report_title
            or draft.source_set_digest != outline.source_set_digest
        ):
            raise WorkCompositionError("ReportDraft 与当前已确认的 ReportOutline 绑定不一致。")
        self._require_draft_bindings(item, outline_contract, outline, draft)
        current = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_DRAFT_CONTRACT_TYPE,
        )
        if current is not None and dict(current.payload) == draft.to_payload():
            saved = current
        else:
            version = 1 if current is None else current.contract_version + 1
            candidate = draft.to_contract(
                work_id=item.work_id,
                tenant_id=item.tenant_id,
                contract_version=version,
                created_by=item.requester_id,
                created_at=self._factory.clock(),
            )
            saved = (
                self.repository.create_work_contract(candidate)
                if current is None
                else self.repository.revise_work_contract(candidate)
            )
        if isinstance(state, dict):
            self._publish_current_work(cast("dict[str, Any]", state), item)
        return saved

    def confirm_report_draft(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
        *,
        expected_version: int,
        latest_user_message: str,
    ) -> WorkContractSnapshot:
        """Confirm an exact draft version without treating confirmation as approval."""

        item, outline_contract, outline = self.current_confirmed_report_outline(state, runtime_context)
        current = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_DRAFT_CONTRACT_TYPE,
        )
        if current is None:
            raise WorkCompositionError("当前任务尚未形成 ReportDraft。")
        if current.contract_version != expected_version:
            raise WorkCompositionError("ReportDraft 版本不匹配，请重新展示当前版本后再确认。")
        try:
            draft = ReportDraft.from_contract(current)
        except (TypeError, ValueError) as exc:
            raise WorkCompositionError("当前 ReportDraft 合同无效，不能确认。") from exc
        self._require_draft_bindings(item, outline_contract, outline, draft)
        if current.status is WorkContractStatus.CONFIRMED:
            confirmed = current
        else:
            if not explicitly_confirms_report_draft(latest_user_message, expected_version=expected_version):
                raise WorkCompositionError(
                    f"员工最新消息未显式确认 ReportDraft v{expected_version}，不能确认初稿。"
                )
            confirmed = self.repository.confirm_work_contract(
                tenant_id=item.tenant_id,
                work_id=item.work_id,
                contract_type=REPORT_DRAFT_CONTRACT_TYPE,
                expected_contract_version=expected_version,
                confirmed_by=item.requester_id,
                confirmed_at=self._factory.clock(),
            )
        if isinstance(state, dict):
            self._publish_current_work(cast("dict[str, Any]", state), item)
        return confirmed

    def current_confirmed_report_draft(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
    ) -> tuple[WorkItem, WorkContractSnapshot, ReportDraft]:
        """Return the current confirmed draft after revalidating its frozen bindings."""

        item, outline_contract, outline = self.current_confirmed_report_outline(state, runtime_context)
        contract = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_DRAFT_CONTRACT_TYPE,
        )
        if contract is None or contract.status is not WorkContractStatus.CONFIRMED:
            raise WorkCompositionError("当前任务没有已确认的 ReportDraft，不能提交或完成审批。")
        try:
            draft = ReportDraft.from_contract(contract)
        except (TypeError, ValueError) as exc:
            raise WorkCompositionError("当前 ReportDraft 合同无效，不能提交或完成审批。") from exc
        if (
            draft.report_outline_version != outline_contract.contract_version
            or draft.report_brief_version != outline.report_brief_version
            or draft.report_title != outline.report_title
            or draft.source_set_digest != outline.source_set_digest
        ):
            raise WorkCompositionError("当前 ReportDraft 与已确认的 ReportOutline 绑定不一致。")
        self._require_draft_bindings(item, outline_contract, outline, draft)
        return item, contract, draft

    def request_report_approval(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
        *,
        expected_version: int,
        latest_user_message: str,
    ) -> WorkContractSnapshot:
        """Open a versioned approval request for one exact confirmed draft."""

        item, draft_contract, draft = self.current_confirmed_report_draft(state, runtime_context)
        if draft_contract.contract_version != expected_version:
            raise WorkCompositionError("ReportDraft 版本不匹配，请重新展示当前版本后再提交审批。")
        if not explicitly_requests_report_approval(latest_user_message, expected_version=expected_version):
            raise WorkCompositionError(
                f"员工最新消息未显式提交 ReportDraft v{expected_version} 审批，不能建立审批合同。"
            )
        approval = ReportApproval(
            report_draft_version=draft_contract.contract_version,
            report_draft_digest=report_draft_digest(draft),
            request_message_digest=approval_message_digest(latest_user_message),
        )
        current = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_APPROVAL_CONTRACT_TYPE,
        )
        if current is not None:
            try:
                current_approval = ReportApproval.from_contract(current)
            except (TypeError, ValueError):
                current_approval = None
            if (
                current_approval is not None
                and current_approval.report_draft_version == approval.report_draft_version
                and current_approval.report_draft_digest == approval.report_draft_digest
                and current_approval.policy_id == approval.policy_id
            ):
                saved = current
            else:
                saved = self.repository.revise_work_contract(
                    approval.to_contract(
                        work_id=item.work_id,
                        tenant_id=item.tenant_id,
                        contract_version=current.contract_version + 1,
                        created_by=item.requester_id,
                        created_at=self._factory.clock(),
                    )
                )
        else:
            saved = self.repository.create_work_contract(
                approval.to_contract(
                    work_id=item.work_id,
                    tenant_id=item.tenant_id,
                    contract_version=1,
                    created_by=item.requester_id,
                    created_at=self._factory.clock(),
                )
            )
        if isinstance(state, dict):
            self._publish_current_work(cast("dict[str, Any]", state), item)
        return saved

    def approve_report_draft(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
        *,
        expected_version: int,
        latest_user_message: str,
    ) -> WorkContractSnapshot:
        """Approve one exact pending draft without publishing or rendering it."""

        item, draft_contract, draft = self.current_confirmed_report_draft(state, runtime_context)
        if draft_contract.contract_version != expected_version:
            raise WorkCompositionError("ReportDraft 版本不匹配，请重新展示当前版本后再审批。")
        if not explicitly_approves_report_draft(latest_user_message, expected_version=expected_version):
            raise WorkCompositionError(
                f"员工最新消息未显式批准 ReportDraft v{expected_version}，不能完成审批。"
            )
        enterprise = _enterprise_context(runtime_context if runtime_context is not None else state)
        actor = _clean(enterprise.get("user_key")) if enterprise is not None else ""
        if not actor or actor != item.approver_id:
            raise WorkCompositionError("当前企业身份不是该任务的审批人，不能批准。")
        current = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_APPROVAL_CONTRACT_TYPE,
        )
        if current is None:
            raise WorkCompositionError("当前 ReportDraft 尚未显式提交审批。")
        try:
            approval = ReportApproval.from_contract(current)
        except (TypeError, ValueError) as exc:
            raise WorkCompositionError("当前 ReportApproval 合同无效，不能审批。") from exc
        if (
            approval.report_draft_version != draft_contract.contract_version
            or approval.report_draft_digest != report_draft_digest(draft)
        ):
            raise WorkCompositionError("当前审批请求未绑定最新已确认 ReportDraft，请重新提交审批。")
        if current.status is WorkContractStatus.CONFIRMED:
            approved = current
        else:
            approved = self.repository.confirm_work_contract(
                tenant_id=item.tenant_id,
                work_id=item.work_id,
                contract_type=REPORT_APPROVAL_CONTRACT_TYPE,
                expected_contract_version=current.contract_version,
                confirmed_by=actor,
                confirmed_at=self._factory.clock(),
            )
        if isinstance(state, dict):
            self._publish_current_work(cast("dict[str, Any]", state), item)
        return approved

    def render_report_artifact(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
        *,
        expected_version: int,
        artifact_format: str,
        latest_user_message: str,
    ) -> ArtifactRecord:
        """Render one current approved draft into an immutable DOCX artifact."""

        if not explicitly_requests_report_artifact(
            latest_user_message,
            expected_version=expected_version,
            artifact_format=artifact_format,
        ):
            raise WorkCompositionError(
                f"员工最新消息未显式请求生成 ReportDraft v{expected_version} DOCX，不能渲染文件。"
            )
        if artifact_format != REPORT_ARTIFACT_FORMAT_DOCX:
            raise WorkCompositionError("当前仅支持 DOCX Artifact；PDF 尚未启用。")
        item, draft_contract, draft = self.current_confirmed_report_draft(state, runtime_context)
        if draft_contract.contract_version != expected_version:
            raise WorkCompositionError("ReportDraft 版本不匹配，请重新展示当前版本后再渲染。")
        approval_contract = self._require_current_report_approval(
            item,
            draft_contract=draft_contract,
            draft=draft,
        )
        template_ref, template_digest, template_bytes = self._read_verified_docx_template()
        try:
            rendered = render_report_docx(markdown=draft.markdown, template_bytes=template_bytes)
            blob = self.artifact_store.put(
                tenant_id=item.tenant_id,
                work_id=item.work_id,
                data=rendered,
                suffix=artifact_format,
            )
        except ReportArtifactError as exc:
            raise WorkCompositionError(f"DOCX Artifact 渲染失败：{exc}") from exc
        draft_digest = report_draft_digest(draft)
        approval_digest = contract_digest(approval_contract)
        record = ArtifactRecord(
            artifact_id=artifact_id(
                tenant_id=item.tenant_id,
                work_id=item.work_id,
                content_sha256=blob.content_sha256,
                source_digest=draft_digest,
                approval_digest=approval_digest,
                template_digest=template_digest,
            ),
            work_id=item.work_id,
            tenant_id=item.tenant_id,
            artifact_type=REPORT_ARTIFACT_TYPE,
            artifact_format=artifact_format,
            media_type=REPORT_ARTIFACT_MEDIA_TYPE_DOCX,
            content_sha256=blob.content_sha256,
            size_bytes=blob.size_bytes,
            storage_key=blob.storage_key,
            filename=f"industry-report-draft-v{expected_version}.docx",
            source_contract_type=REPORT_DRAFT_CONTRACT_TYPE,
            source_contract_version=expected_version,
            source_digest=draft_digest,
            approval_contract_version=approval_contract.contract_version,
            approval_digest=approval_digest,
            template_ref=template_ref,
            template_digest=template_digest,
            created_by=item.requester_id,
            created_at=self._factory.clock(),
            metadata={
                "report_outline_version": draft.report_outline_version,
                "report_brief_version": draft.report_brief_version,
                "pack_snapshot_id": item.pack_snapshot_id,
                "publication_status": "not_published",
                "delivery_status": "not_delivered",
            },
        )
        saved = self.repository.put_artifact_record(record)
        if isinstance(state, dict):
            current = self.repository.get_work(tenant_id=item.tenant_id, work_id=item.work_id)
            self._publish_current_work(cast("dict[str, Any]", state), current)
        return saved

    def publish_report_artifact(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
        *,
        expected_version: int,
        latest_user_message: str,
    ) -> PublicationRecord:
        """Publish one exact current Artifact without delivering it."""

        if not explicitly_requests_report_publication(
            latest_user_message,
            expected_version=expected_version,
        ):
            raise WorkCompositionError(
                f"员工最新消息未精确请求发布 ReportArtifact v{expected_version}，不能登记发布。"
            )
        item, draft_contract, draft = self.current_confirmed_report_draft(state, runtime_context)
        if draft_contract.contract_version != expected_version:
            raise WorkCompositionError("ReportArtifact 版本不匹配，请重新展示当前版本后再发布。")
        approval_contract = self._require_current_report_approval(
            item,
            draft_contract=draft_contract,
            draft=draft,
        )
        approval = ReportApproval.from_contract(approval_contract)
        draft_digest = report_draft_digest(draft)
        approval_digest = contract_digest(approval_contract)
        candidates = tuple(
            artifact
            for artifact in self.repository.list_artifact_records(
                tenant_id=item.tenant_id,
                work_id=item.work_id,
            )
            if artifact.artifact_format == REPORT_ARTIFACT_FORMAT_DOCX
            and artifact.source_contract_version == expected_version
            and artifact.source_digest == draft_digest
            and artifact.approval_contract_version == approval_contract.contract_version
            and artifact.approval_digest == approval_digest
        )
        if len(candidates) != 1:
            raise WorkCompositionError("当前没有唯一且绑定最新审批的 ReportArtifact，不能发布。")
        artifact = candidates[0]
        try:
            artifact_bytes = self.artifact_store.resolve(artifact.storage_key).read_bytes()
        except (OSError, ReportArtifactError) as exc:
            raise WorkCompositionError("当前 ReportArtifact 物理文件不可用，不能发布。") from exc
        if f"sha256:{sha256(artifact_bytes).hexdigest()}" != artifact.content_sha256:
            raise WorkCompositionError("当前 ReportArtifact 内容哈希校验失败，不能发布。")
        enterprise = _enterprise_context(runtime_context if runtime_context is not None else state)
        actor = _clean(enterprise.get("user_key")) if enterprise is not None else ""
        if not actor or actor != item.requester_id:
            raise WorkCompositionError("当前企业身份不是该任务的委派人，不能发布。")
        publications = self.repository.list_publication_records(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
        )
        existing = next(
            (publication for publication in publications if publication.artifact_id == artifact.artifact_id),
            None,
        )
        if existing is not None:
            saved = existing
        else:
            saved = self.repository.put_publication_record(PublicationRecord(
                publication_id=publication_id(
                    tenant_id=item.tenant_id,
                    work_id=item.work_id,
                    artifact_id=artifact.artifact_id,
                    content_sha256=artifact.content_sha256,
                ),
                publication_version=max(
                    (publication.publication_version for publication in publications),
                    default=0,
                )
                + 1,
                work_id=item.work_id,
                tenant_id=item.tenant_id,
                artifact_id=artifact.artifact_id,
                content_sha256=artifact.content_sha256,
                source_contract_version=artifact.source_contract_version,
                approval_contract_version=artifact.approval_contract_version,
                template_digest=artifact.template_digest,
                policy_id=approval.policy_id,
                status=PublicationStatus.PUBLISHED,
                published_by=actor,
                published_at=self._factory.clock(),
                metadata={
                    "pack_snapshot_id": item.pack_snapshot_id,
                    "delivery_status": "not_delivered",
                },
            ))
        if isinstance(state, dict):
            current = self.repository.get_work(tenant_id=item.tenant_id, work_id=item.work_id)
            self._publish_current_work(cast("dict[str, Any]", state), current)
        return saved

    def _require_current_report_approval(
        self,
        item: WorkItem,
        *,
        draft_contract: WorkContractSnapshot,
        draft: ReportDraft,
    ) -> WorkContractSnapshot:
        approval_contract = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_APPROVAL_CONTRACT_TYPE,
        )
        if approval_contract is None or approval_contract.status is not WorkContractStatus.CONFIRMED:
            raise WorkCompositionError("当前 ReportDraft 尚未完成内容审批，不能渲染 Artifact。")
        try:
            approval = ReportApproval.from_contract(approval_contract)
        except (TypeError, ValueError) as exc:
            raise WorkCompositionError("当前 ReportApproval 合同无效，不能渲染 Artifact。") from exc
        outline_contract = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_OUTLINE_CONTRACT_TYPE,
        )
        try:
            outline = ReportOutline.from_contract(outline_contract) if outline_contract else None
        except (TypeError, ValueError):
            outline = None
        if not self._approval_matches_current_draft(
            item,
            approval,
            draft_contract=draft_contract,
            draft=draft,
            outline_contract=outline_contract,
            outline=outline,
        ):
            raise WorkCompositionError("当前 ReportApproval 已失效或未绑定最新 ReportDraft，不能渲染 Artifact。")
        return approval_contract

    def _read_verified_docx_template(self) -> tuple[str, str, bytes]:
        asset = next(
            (candidate for candidate in self.loaded_pack.assets if candidate.artifact_ref == _ASSET_REF),
            None,
        )
        if asset is None:
            raise WorkCompositionError("当前 PackSnapshot 未声明批准的 DOCX 模板资产。")
        try:
            template_bytes = self.template_asset_path.read_bytes()
        except OSError as exc:
            raise WorkCompositionError("无法读取批准的 DOCX 模板资产。") from exc
        template_digest = f"sha256:{sha256(template_bytes).hexdigest()}"
        if template_digest != f"sha256:{asset.sha256}":
            raise WorkCompositionError("DOCX 模板资产摘要与 PackSnapshot 声明不一致。")
        return asset.artifact_ref, template_digest, template_bytes

    def _require_draft_bindings(
        self,
        item: WorkItem,
        outline_contract: WorkContractSnapshot,
        outline: ReportOutline,
        draft: ReportDraft,
    ) -> None:
        source_ids = set(outline.source_ids)
        evidence = tuple(
            record
            for record in self.repository.list_evidence_records(
                tenant_id=item.tenant_id,
                work_id=item.work_id,
            )
            if record.metadata.get("report_outline_version") == outline_contract.contract_version
            and record.source_id in source_ids
        )
        if evidence_set_digest(evidence) != draft.evidence_set_digest:
            raise WorkCompositionError("ReportDraft 绑定的 Evidence 集合已变化，请重新准备初稿上下文。")
        claims_by_id = {
            claim.claim_id: claim
            for claim in self.repository.list_claim_records(tenant_id=item.tenant_id, work_id=item.work_id)
        }
        try:
            claims = tuple(claims_by_id[claim_id] for claim_id in draft.claim_ids)
        except KeyError as exc:
            raise WorkCompositionError("ReportDraft 引用了未登记的 ClaimRecord。") from exc
        if claim_set_digest(claims) != draft.claim_set_digest:
            raise WorkCompositionError("ReportDraft 绑定的 Claim 集合已变化，请重新生成初稿。")

    def _require_outline_brief_binding(self, item: WorkItem, outline: ReportOutline) -> None:
        brief_contract = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_BRIEF_CONTRACT_TYPE,
        )
        if brief_contract is None or brief_contract.status is not WorkContractStatus.CONFIRMED:
            raise WorkCompositionError("当前任务没有已确认的 ReportBrief，不能形成或确认提纲。")
        if brief_contract.contract_version != outline.report_brief_version:
            raise WorkCompositionError(
                f"ReportOutline 绑定的 ReportBrief v{outline.report_brief_version} 已失效；"
                f"当前已确认版本为 ReportBrief v{brief_contract.contract_version}。"
            )
        brief = ReportBrief.from_contract(brief_contract)
        if brief.title != outline.report_title or brief.research_scope.value != outline.research_scope:
            raise WorkCompositionError("ReportOutline 与当前 ReportBrief 的主题或研究范围不一致。")

    def _require_outline_evidence_current(self, item: WorkItem, outline: ReportOutline) -> None:
        if outline.gap_decision_contract_version is not None:
            decision = self.repository.get_current_work_contract(
                tenant_id=item.tenant_id,
                work_id=item.work_id,
                contract_type=RESEARCH_GAP_DECISION_CONTRACT_TYPE,
            )
            if (
                decision is None
                or decision.status is not WorkContractStatus.CONFIRMED
                or decision.contract_version != outline.gap_decision_contract_version
            ):
                raise WorkCompositionError("ReportOutline 绑定的研究缺口决策已失效，请重新生成提纲。")
        sources = _outline_sources(self.repository.list_source_records(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
        ), outline)
        if source_set_digest(sources) != outline.source_set_digest:
            raise WorkCompositionError("ReportOutline 绑定的来源集合已变化，请重新生成提纲。")

    def confirm_research_gap_decision(
        self,
        state: Mapping[str, object],
        runtime_context: object | None,
        *,
        decision: ResearchGapDecision,
        latest_user_message: str,
    ) -> WorkContractSnapshot:
        """Persist one version-bound, explicit gap-resolution choice and confirm it."""

        item = self.current_work(state, runtime_context)
        if item is None:
            raise WorkCompositionError("当前员工没有可登记研究缺口决策的进行中报告任务。")
        self._require_current_confirmed_brief(item, decision.report_brief_version)
        if not explicitly_selects_gap_action(
            latest_user_message,
            expected_version=decision.report_brief_version,
            expected_action=decision.action,
        ):
            raise WorkCompositionError(
                f"员工最新消息未对 ReportBrief v{decision.report_brief_version} "
                f"明确选择 {decision.action.value}，不执行研究升级。"
            )

        current = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=RESEARCH_GAP_DECISION_CONTRACT_TYPE,
        )
        if current is not None and _same_gap_decision(current, decision):
            candidate = current
        else:
            version = 1 if current is None else current.contract_version + 1
            candidate = decision.to_contract(
                work_id=item.work_id,
                tenant_id=item.tenant_id,
                contract_version=version,
                created_by=item.requester_id,
                created_at=self._factory.clock(),
            )
            try:
                candidate = (
                    self.repository.create_work_contract(candidate)
                    if current is None
                    else self.repository.revise_work_contract(candidate)
                )
            except WorkContractConflictError:
                candidate = self.repository.get_current_work_contract(
                    tenant_id=item.tenant_id,
                    work_id=item.work_id,
                    contract_type=RESEARCH_GAP_DECISION_CONTRACT_TYPE,
                )
                if candidate is None or not _same_gap_decision(candidate, decision):
                    raise
        if candidate.status is WorkContractStatus.CONFIRMED:
            return candidate
        try:
            return self.repository.confirm_work_contract(
                tenant_id=item.tenant_id,
                work_id=item.work_id,
                contract_type=RESEARCH_GAP_DECISION_CONTRACT_TYPE,
                expected_contract_version=candidate.contract_version,
                confirmed_by=item.requester_id,
                confirmed_at=self._factory.clock(),
            )
        except WorkContractConflictError:
            latest = self.repository.get_current_work_contract(
                tenant_id=item.tenant_id,
                work_id=item.work_id,
                contract_type=RESEARCH_GAP_DECISION_CONTRACT_TYPE,
            )
            if (
                latest is not None
                and latest.status is WorkContractStatus.CONFIRMED
                and _same_gap_decision(latest, decision)
            ):
                return latest
            raise

    def _require_current_confirmed_brief(self, item: WorkItem, expected_version: int) -> None:
        current = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_BRIEF_CONTRACT_TYPE,
        )
        if current is None or current.status is not WorkContractStatus.CONFIRMED:
            raise WorkCompositionError("当前任务没有已确认的 ReportBrief，不执行研究缺口决策。")
        if current.contract_version != expected_version:
            raise WorkCompositionError(
                f"研究缺口决策绑定的 ReportBrief v{expected_version} 已失效；"
                f"当前已确认版本为 ReportBrief v{current.contract_version}，请重新展示当前缺口选项。"
            )

    def _authorization_status(self, state: Mapping[str, object]) -> str:
        if self.profile.service_status != "enabled":
            return "disabled"
        if _enterprise_context(state) is None:
            return "requester_forbidden"
        employee = state.get("employee_context")
        employee_mapping = (
            {str(key): value for key, value in employee.items()} if isinstance(employee, Mapping) else None
        )
        if employee_mapping is None or not _requester_allowed(self.profile, employee_mapping):
            return "requester_forbidden"
        return "found"

    def _publish_current_work(self, state: dict[str, Any], item: WorkItem) -> None:
        summary = self._ledger_summary(item)
        state["current_work"] = summary
        state["current_work_context"] = _current_work_context(summary)
        _merge_runtime_context(
            state,
            "work",
            {
                "version": "v1",
                "work_id": item.work_id,
                "playbook_id": item.playbook_id,
                "phase": item.current_phase,
                "requester_key": item.requester_id,
                "permissions_digest": self.permissions_digest,
                "pack_snapshot_id": item.pack_snapshot_id,
                "runtime_release": item.runtime_release,
            },
        )

    def _ledger_summary(self, item: WorkItem) -> dict[str, object]:  # noqa: C901
        summary = _work_summary(item)
        brief = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_BRIEF_CONTRACT_TYPE,
        )
        if brief is not None:
            summary["report_brief"] = {
                "contract_version": brief.contract_version,
                "status": brief.status.value,
            }
        gap_contract = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=RESEARCH_GAP_DECISION_CONTRACT_TYPE,
        )
        if gap_contract is not None:
            try:
                decision = ResearchGapDecision.from_contract(gap_contract)
            except (TypeError, ValueError):
                pass
            else:
                summary["research_gap_decision"] = {
                    "contract_version": gap_contract.contract_version,
                    "status": gap_contract.status.value,
                    "report_brief_version": decision.report_brief_version,
                    "action": decision.action.value,
                }
        current_outline: ReportOutline | None = None
        outline_contract = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_OUTLINE_CONTRACT_TYPE,
        )
        if outline_contract is not None:
            try:
                outline = ReportOutline.from_contract(outline_contract)
            except (TypeError, ValueError):
                pass
            else:
                current_outline = outline
                summary["report_outline"] = {
                    "contract_version": outline_contract.contract_version,
                    "status": outline_contract.status.value,
                    "report_brief_version": outline.report_brief_version,
                    "source_set_digest": outline.source_set_digest,
                    "unresolved_question_count": len(outline.unresolved_question_ids),
                }
        current_draft: ReportDraft | None = None
        draft_contract = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_DRAFT_CONTRACT_TYPE,
        )
        if draft_contract is not None:
            try:
                draft = ReportDraft.from_contract(draft_contract)
            except (TypeError, ValueError):
                pass
            else:
                current_draft = draft
                summary["report_draft"] = {
                    "contract_version": draft_contract.contract_version,
                    "status": draft_contract.status.value,
                    "report_outline_version": draft.report_outline_version,
                    "quality_status": draft.quality_status.value,
                    "claim_count": len(draft.claim_ids),
                }
        approval_is_current = False
        approval_contract = self.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_APPROVAL_CONTRACT_TYPE,
        )
        if approval_contract is not None:
            try:
                approval = ReportApproval.from_contract(approval_contract)
            except (TypeError, ValueError):
                pass
            else:
                approval_is_current = self._approval_matches_current_draft(
                    item,
                    approval,
                    draft_contract=draft_contract,
                    draft=current_draft,
                    outline_contract=outline_contract,
                    outline=current_outline,
                )
                summary["report_approval"] = {
                    "contract_version": approval_contract.contract_version,
                    "status": approval_state(approval_contract),
                    "report_draft_version": approval.report_draft_version,
                    "policy_id": approval.policy_id,
                    "current": approval_is_current,
                }
        artifacts = self.repository.list_artifact_records(tenant_id=item.tenant_id, work_id=item.work_id)
        publications = self.repository.list_publication_records(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
        )
        if artifacts:
            current_approval_digest = contract_digest(approval_contract) if approval_contract is not None else ""
            current_draft_digest = report_draft_digest(current_draft) if current_draft is not None else ""
            artifact_current = {
                artifact.artifact_id: (
                    approval_is_current
                    and approval_contract is not None
                    and approval_contract.status is WorkContractStatus.CONFIRMED
                    and artifact.source_digest == current_draft_digest
                    and artifact.approval_digest == current_approval_digest
                )
                for artifact in artifacts
            }
            published_artifact_ids = {publication.artifact_id for publication in publications}
            summary["report_artifacts"] = [
                {
                    "artifact_id": artifact.artifact_id,
                    "format": artifact.artifact_format,
                    "filename": artifact.filename,
                    "content_sha256": artifact.content_sha256,
                    "size_bytes": artifact.size_bytes,
                    "report_draft_version": artifact.source_contract_version,
                    "approval_contract_version": artifact.approval_contract_version,
                    "current": artifact_current[artifact.artifact_id],
                    "publication_status": (
                        "published"
                        if artifact.artifact_id in published_artifact_ids
                        else artifact.metadata.get("publication_status")
                    ),
                    "delivery_status": artifact.metadata.get("delivery_status"),
                }
                for artifact in artifacts
            ]
            if publications:
                summary["report_publications"] = [
                    {
                        "publication_id": publication.publication_id,
                        "publication_version": publication.publication_version,
                        "status": publication.status.value,
                        "artifact_id": publication.artifact_id,
                        "content_sha256": publication.content_sha256,
                        "report_draft_version": publication.source_contract_version,
                        "approval_contract_version": publication.approval_contract_version,
                        "policy_id": publication.policy_id,
                        "current": artifact_current.get(publication.artifact_id, False),
                        "delivery_status": publication.metadata.get("delivery_status"),
                    }
                    for publication in publications
                ]
        return summary

    def _approval_matches_current_draft(
        self,
        item: WorkItem,
        approval: ReportApproval,
        *,
        draft_contract: WorkContractSnapshot | None,
        draft: ReportDraft | None,
        outline_contract: WorkContractSnapshot | None,
        outline: ReportOutline | None,
    ) -> bool:
        if (
            draft_contract is None
            or draft_contract.status is not WorkContractStatus.CONFIRMED
            or draft is None
            or outline_contract is None
            or outline_contract.status is not WorkContractStatus.CONFIRMED
            or outline is None
            or approval.report_draft_version != draft_contract.contract_version
            or approval.report_draft_digest != report_draft_digest(draft)
            or draft.report_outline_version != outline_contract.contract_version
            or draft.report_brief_version != outline.report_brief_version
            or draft.report_title != outline.report_title
            or draft.source_set_digest != outline.source_set_digest
        ):
            return False
        try:
            self._require_outline_brief_binding(item, outline)
            self._require_outline_evidence_current(item, outline)
            self._require_draft_bindings(item, outline_contract, outline, draft)
        except WorkCompositionError:
            return False
        return True


def build_work_binding() -> IndustryReportWorkComposition:
    """Bub entrypoint named by AGENTSEEK_WORK_BINDING."""

    return get_work_composition()


@lru_cache(maxsize=1)
def get_work_composition() -> IndustryReportWorkComposition:
    settings = get_settings()
    if not settings.work_enabled:
        raise WorkCompositionError("AGENTSEEK_WORK_ENABLED is false")
    engine = create_engine(settings.require_work_sqlalchemy_url(), pool_pre_ping=True)
    _prepare_schema(engine, settings)
    repository = SQLAlchemyWorkRepository(engine)
    loaded = RestrictedPackLoader(
        pack_root=_PACK_ROOT,
        allowed_entrypoint_package="enterprise_wecom_digital_employee",
        asset_resolver=_resolve_asset,
    ).load()
    snapshot_store = FilesystemPackSnapshotStore(settings.resolved_work_snapshot_path())
    candidate = build_pack_snapshot(
        loaded,
        store=snapshot_store,
        created_at=datetime.now(UTC),
        source_repository=_optional(settings.work_source_repository),
        source_commit=_optional(settings.work_source_commit),
    )
    try:
        snapshot = repository.get_pack_snapshot(pack_snapshot_id=candidate.pack_snapshot_id)
    except WorkNotFoundError:
        snapshot = repository.put_pack_snapshot(candidate)
    else:
        _verify_registered_snapshot(candidate, snapshot)
    return IndustryReportWorkComposition(
        repository=repository,
        loaded_pack=loaded,
        pack_snapshot_id=snapshot.pack_snapshot_id,
        runtime_release=settings.require_work_runtime_release(),
        pack_artifact_root=snapshot_store.resolve(snapshot.content_artifact_id),
        artifact_store_root=settings.resolved_work_artifact_path(),
        template_asset_path=settings.resolved_work_template_asset_path(),
    )


def _prepare_schema(engine: Engine, settings: ProjectSettings) -> None:
    if settings.work_auto_migrate:
        apply_migrations(engine)
        return
    version = current_schema_version(engine)
    if version != LATEST_SCHEMA_VERSION:
        raise WorkCompositionError(
            f"work ledger schema revision {version} is not ready; expected {LATEST_SCHEMA_VERSION}"
        )


def _resolve_asset(artifact_ref: str) -> Path:
    if artifact_ref != _ASSET_REF:
        raise WorkCompositionError("角色包引用了未批准的模板资产。")
    return get_settings().resolved_work_template_asset_path()


def _verify_registered_snapshot(candidate, stored) -> None:
    fields = (
        "pack_id",
        "pack_version",
        "manifest_digest",
        "content_artifact_id",
        "asset_version_refs",
    )
    if any(getattr(candidate, field) != getattr(stored, field) for field in fields):
        raise WorkCompositionError("已登记 PackSnapshot 与当前角色包内容不一致。")


def _same_gap_decision(contract: WorkContractSnapshot, decision: ResearchGapDecision) -> bool:
    try:
        stored = ResearchGapDecision.from_contract(contract)
    except (TypeError, ValueError):
        return False
    return (
        stored.report_brief_version == decision.report_brief_version
        and stored.research_plan_digest == decision.research_plan_digest
        and stored.gap_digest == decision.gap_digest
        and stored.gap_question_ids == decision.gap_question_ids
        and stored.action is decision.action
    )


def _outline_sources(
    sources: Sequence[SourceRecord],
    outline: ReportOutline,
) -> tuple[SourceRecord, ...]:
    return tuple(
        source
        for source in sources
        if source.metadata.get("report_brief_version") == outline.report_brief_version
        and source.metadata.get("research_plan_digest") == outline.research_plan_digest
        and (
            source.source_type is SourceType.DEPARTMENT_KNOWLEDGE
            or (
                outline.gap_decision_contract_version is not None
                and source.metadata.get("gap_decision_contract_version")
                == outline.gap_decision_contract_version
            )
        )
    )


def _enterprise_context(source: object) -> Mapping[str, object] | None:
    if isinstance(source, Mapping):
        private_runtime = source.get("_langgraph_runtime_context")
        runtime = private_runtime if isinstance(private_runtime, Mapping) else source
    else:
        runtime = source
    enterprise = runtime.get("enterprise") if isinstance(runtime, Mapping) else getattr(runtime, "enterprise", None)
    if not isinstance(enterprise, Mapping):
        return None
    if _clean(enterprise.get("version")) != "v1":
        return None
    for key in ("tenant_key", "user_key", "session_key"):
        if not _SCOPED_KEY_RE.fullmatch(_clean(enterprise.get(key))):
            return None
    if not _clean(enterprise.get("tenant_id")):
        return None
    return {str(key): value for key, value in enterprise.items()}


def _requester_allowed(profile: DigitalEmployeeProfile, employee: Mapping[str, object]) -> bool:
    organization_text = " | ".join(
        _clean(employee.get(key)) for key in ("dept_name", "primary_org_name", "org_path_label", "belong_to_label")
    )
    for scope in profile.requester_scope:
        if scope == "strategic-development-employee" and profile.owning_org in organization_text:
            continue
        return False
    return True


def _message_scope_key(message: Envelope) -> str:
    context = field_of(message, "context", {})
    message_id = _nested_message_id(context)
    stable_message = message_id or f"content-sha256:{sha256(content_of(message).encode()).hexdigest()}"
    return EnterpriseRuntimeSettings.from_env().scoped_key("message", stable_message)


def _request_key(
    message: Envelope,
    enterprise: Mapping[str, object],
    state: Mapping[str, object],
) -> str:
    message_key = _clean(state.get("_work_message_key")) or _message_scope_key(message)
    payload = f"{enterprise['session_key']}:{message_key}".encode()
    return f"request_sha256_{sha256(payload).hexdigest()}"


def _nested_message_id(context: object) -> str:
    if not isinstance(context, Mapping):
        return ""
    wecom = context.get("wecom")
    raw = wecom.get("raw") if isinstance(wecom, Mapping) else None
    if isinstance(raw, Mapping):
        return _clean(raw.get("msgid"))
    return _clean(context.get("msgid"))


def _merge_runtime_context(state: dict[str, Any], key: str, value: Mapping[str, object]) -> None:
    existing = state.get("_langgraph_runtime_context")
    runtime = dict(existing) if isinstance(existing, Mapping) else {}
    runtime[key] = dict(value)
    state["_langgraph_runtime_context"] = runtime


def _profile_summary(profile: DigitalEmployeeProfile) -> dict[str, object]:
    return {
        "digital_employee_id": profile.digital_employee_id,
        "name": profile.name,
        "owning_org": profile.owning_org,
        "job_role": profile.job_role,
        "responsibilities": list(profile.responsibilities),
        "pack_id": profile.pack_id,
        "pack_version": profile.pack_version,
        "supported_playbooks": list(profile.supported_playbooks),
        "skill_refs": list(profile.skill_refs),
        "asset_refs": list(profile.asset_refs),
        "knowledge_refs": [
            {
                "id": reference.knowledge_id,
                "server": reference.server,
                "collection": reference.collection,
                "owning_org": reference.owning_org,
                "default_mode": reference.default_mode,
                "tools": list(reference.tools),
            }
            for reference in profile.knowledge_refs
        ],
        "profile_version": profile.profile_version,
    }


def _permissions_digest(profile: DigitalEmployeeProfile) -> str:
    payload = {
        "tool_grants": profile.tool_grants,
        "data_scopes": profile.data_scopes,
        "knowledge_refs": [
            {
                "id": reference.knowledge_id,
                "provider": reference.provider,
                "server": reference.server,
                "collection": reference.collection,
                "owning_org": reference.owning_org,
                "contract_version": reference.contract_version,
                "retrieval_modes": reference.retrieval_modes,
                "default_mode": reference.default_mode,
                "tools": reference.tools,
            }
            for reference in profile.knowledge_refs
        ],
        "requester_scope": profile.requester_scope,
        "escalation_policy": dict(profile.escalation_policy),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{sha256(encoded).hexdigest()}"


def _skill_set_digest(loaded: LoadedPackManifest) -> str:
    encoded = json.dumps(loaded.skill_digests, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{sha256(encoded).hexdigest()}"


def _single_playbook_ref(profile: DigitalEmployeeProfile) -> tuple[str, str]:
    if len(profile.supported_playbooks) != 1:
        raise WorkCompositionError("v0.1.0 requires exactly one enabled Playbook per Profile.")
    playbook_id, separator, version = profile.supported_playbooks[0].partition("@")
    if separator != "@" or not playbook_id or not version:
        raise WorkCompositionError("Profile Playbook reference is invalid.")
    return playbook_id, version


def _current_file_ids(state: Mapping[str, object]) -> tuple[str, ...]:
    current_files = state.get("current_files")
    if not isinstance(current_files, Sequence) or isinstance(current_files, (str, bytes)):
        return ()
    values: list[str] = []
    for record in current_files:
        value = record.get("file_id") if isinstance(record, Mapping) else getattr(record, "file_id", None)
        file_id = _clean(value)
        if file_id and file_id not in values:
            values.append(file_id)
    return tuple(values)


def _work_summary(item: WorkItem) -> dict[str, object]:
    return {
        "work_id": item.work_id,
        "digital_employee_id": item.digital_employee_id,
        "status": item.status.value,
        "current_phase": item.current_phase,
        "playbook_id": item.playbook_id,
        "playbook_version": item.playbook_version,
        "pack_snapshot_id": item.pack_snapshot_id,
        "runtime_release": item.runtime_release,
        "input_file_ids": list(item.input_file_ids),
        "artifact_ids": list(item.artifact_ids),
        "updated_at": item.updated_at.isoformat(),
        "allowed_next_actions": ["provide_input", "cancel", "query_status"],
    }


def _current_work_context(summary: Mapping[str, object]) -> str:
    raw_file_ids = summary.get("input_file_ids")
    file_ids = [str(value) for value in raw_file_ids] if isinstance(raw_file_ids, list) else []
    lines = [
        "[CurrentWork]",
        f"work_id: {summary['work_id']}",
        f"status: {summary['status']}",
        f"phase: {summary['current_phase']}",
        f"playbook: {summary['playbook_id']}@{summary['playbook_version']}",
        f"input_file_ids: {', '.join(file_ids) or '<none>'}",
    ]
    brief = summary.get("report_brief")
    if isinstance(brief, Mapping):
        lines.append(
            "current_report_brief: "
            f"v{brief.get('contract_version')} status={brief.get('status')}"
        )
    decision = summary.get("research_gap_decision")
    if isinstance(decision, Mapping):
        lines.append(
            "latest_gap_decision: "
            f"contract_v{decision.get('contract_version')} status={decision.get('status')} "
            f"bound_report_brief_v{decision.get('report_brief_version')} "
            f"action={decision.get('action')}"
        )
        lines.append(
            "gap-decision 的 contract_version 和 bound_report_brief_version 不是当前 ReportBrief 版本；"
            "当前版本只能以 current_report_brief 为准。"
        )
    outline = summary.get("report_outline")
    if isinstance(outline, Mapping):
        lines.append(
            "current_report_outline: "
            f"v{outline.get('contract_version')} status={outline.get('status')} "
            f"bound_report_brief_v{outline.get('report_brief_version')} "
            f"unresolved={outline.get('unresolved_question_count')}"
        )
    draft = summary.get("report_draft")
    if isinstance(draft, Mapping):
        lines.append(
            "current_report_draft: "
            f"v{draft.get('contract_version')} status={draft.get('status')} "
            f"bound_report_outline_v{draft.get('report_outline_version')} "
            f"quality={draft.get('quality_status')} claims={draft.get('claim_count')}"
        )
    approval = summary.get("report_approval")
    if isinstance(approval, Mapping):
        lines.append(
            "current_report_approval: "
            f"contract_v{approval.get('contract_version')} status={approval.get('status')} "
            f"bound_report_draft_v{approval.get('report_draft_version')} "
            f"current={approval.get('current')} policy={approval.get('policy_id')}"
        )
    artifacts = summary.get("report_artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                continue
            lines.append(
                "report_artifact: "
                f"id={artifact.get('artifact_id')} format={artifact.get('format')} "
                f"bound_report_draft_v{artifact.get('report_draft_version')} "
                f"current={artifact.get('current')} publication={artifact.get('publication_status')} "
                f"delivery={artifact.get('delivery_status')}"
            )
    lines.extend((
        "该摘要来自任务账本。不要把对话记忆当作任务完成证明。",
        "[/CurrentWork]",
    ))
    return "\n".join(lines)


def _optional(value: str) -> str | None:
    cleaned = value.strip()
    return cleaned or None


def _clean(value: object) -> str:
    return str(value or "").strip()
