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
    CreateWorkResult,
    InteractionRoute,
    RouteRequest,
    SideEffect,
    SQLAlchemyWorkRepository,
    ToolContract,
    ToolContractRegistry,
    WorkBudget,
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
        budget_id: str = _BUDGET_ID,
        budget: WorkBudget = _DEFAULT_BUDGET,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: f"work_{uuid4().hex}",
    ) -> None:
        self.repository = repository
        self.loaded_pack = loaded_pack
        self.profile = loaded_pack.profile
        self.playbook_id, _ = _single_playbook_ref(self.profile)
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
        summary = _work_summary(item)
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
        "updated_at": item.updated_at.isoformat(),
        "allowed_next_actions": ["provide_input", "cancel", "query_status"],
    }


def _current_work_context(summary: Mapping[str, object]) -> str:
    raw_file_ids = summary.get("input_file_ids")
    file_ids = [str(value) for value in raw_file_ids] if isinstance(raw_file_ids, list) else []
    return "\n".join((
        "[CurrentWork]",
        f"work_id: {summary['work_id']}",
        f"status: {summary['status']}",
        f"phase: {summary['current_phase']}",
        f"playbook: {summary['playbook_id']}@{summary['playbook_version']}",
        f"input_file_ids: {', '.join(file_ids) or '<none>'}",
        "该摘要来自任务账本。不要把对话记忆当作任务完成证明。",
        "[/CurrentWork]",
    ))


def _optional(value: str) -> str | None:
    cleaned = value.strip()
    return cleaned or None


def _clean(value: object) -> str:
    return str(value or "").strip()
