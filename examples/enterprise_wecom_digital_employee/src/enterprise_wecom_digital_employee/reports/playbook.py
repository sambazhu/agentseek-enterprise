from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agentseek_work import WorkItem
from bub.types import Envelope, State

from enterprise_wecom_digital_employee.capability_registry import CapabilityRegistry
from enterprise_wecom_digital_employee.draft_generation import generate_draft_claims
from enterprise_wecom_digital_employee.pack_loader import PlaybookSpec, ServiceCatalogEntry
from enterprise_wecom_digital_employee.report_artifact import match_report_artifact_render_version
from enterprise_wecom_digital_employee.report_delivery import (
    match_report_delivery_version,
    match_report_history_command,
)
from enterprise_wecom_digital_employee.report_draft import (
    DraftClaimProposal,
    DraftContextResult,
    explicitly_requests_report_draft,
)
from enterprise_wecom_digital_employee.report_output_guard import enforce_m2_output_guard
from enterprise_wecom_digital_employee.report_status import (
    match_report_status_sections,
    render_report_status,
)
from enterprise_wecom_digital_employee.tools import call_mcp_tool
from enterprise_wecom_digital_employee.work_composition import (
    IndustryReportWorkComposition,
    WorkCompositionError,
)
from enterprise_wecom_digital_employee.work_tools import (
    deliver_report_artifact_action,
    generate_report_draft_action,
    work_tools,
)


@dataclass(frozen=True, slots=True)
class IndustryReportPlaybookBinding:
    spec: PlaybookSpec
    composition: IndustryReportWorkComposition
    service: ServiceCatalogEntry
    capability_registry: CapabilityRegistry | None = None

    @property
    def playbook_ref(self) -> str:
        return self.spec.ref

    @property
    def pack_snapshot_id(self) -> str:
        return self.composition.pack_snapshot_id

    def load_message_state(self, message: Envelope, session_id: str) -> State:
        return self.composition.load_message_state(message, session_id)

    def authorize_state(self, state: State) -> None:
        self.composition.authorize_state(state)

    def enrich_state(self, message: Envelope, session_id: str, state: State) -> None:
        self.composition.enrich_state(message, session_id, state)

    def tools(self) -> Sequence[Any]:
        if self.capability_registry is None:
            return work_tools(self.composition)
        return work_tools(
            self.composition,
            invoke_mcp=self.capability_registry.invoke_mcp,
        )

    def guard_output(self, result: object, output: str) -> str:
        return enforce_m2_output_guard(result, output)

    def current_work(
        self,
        state: Mapping[str, object],
        runtime_context: object | None = None,
    ) -> WorkItem | None:
        return self.composition.current_work(state, runtime_context)

    def introduction(self) -> str:
        return f"{self.service.title}：{self.service.summary}"

    def instructions(self) -> str:
        return ""  # The report instructions are supplied by the report agent prompt.

    def _history_response(self, history, state, runtime_context) -> str:
        work_id, version = history
        scoped_state = dict(state, _report_history_work_id=work_id)
        try:
            if self.composition.current_work(scoped_state, runtime_context) is None:
                return "未找到当前身份可访问的已发布报告。"
            if version is None:
                return render_report_status(self.composition.current_work_summary(scoped_state, runtime_context))
            return deliver_report_artifact_action(
                composition=self.composition, state=scoped_state, runtime_context=runtime_context,
                expected_version=version, latest_user_message=f"交付 ReportArtifact v{version} 给我",
            )
        except (OSError, TypeError, ValueError, WorkCompositionError) as exc:
            return str(exc)

    async def direct_response(
        self,
        message: str,
        state: Mapping[str, object],
        runtime_context: object | None = None,
        callbacks: Sequence[object] = (),
    ) -> str | None:
        from enterprise_wecom_digital_employee.work_commands import explicitly_cancels_current_work

        history = match_report_history_command(message)
        if history is not None:
            return self._history_response(history, state, runtime_context)
        if explicitly_cancels_current_work(message):
            return self.composition.cancel_current_work(
                state, runtime_context, latest_user_message=message,
            )
        sections = match_report_status_sections(message)
        if sections is not None:
            summary = self.composition.current_work_summary(state, runtime_context)
            return render_report_status(summary, sections=sections)
        if match_report_artifact_render_version(message) is not None:
            return None
        if explicitly_requests_report_draft(message):
            invoke_mcp = (
                self.capability_registry.invoke_mcp
                if self.capability_registry is not None
                else call_mcp_tool
            )

            async def claim_generator(
                context: DraftContextResult,
                generator_callbacks: Sequence[object],
            ) -> Sequence[DraftClaimProposal]:
                return await generate_draft_claims(
                    context,
                    callbacks=generator_callbacks,
                )

            try:
                return await generate_report_draft_action(
                    composition=self.composition,
                    state=state,
                    runtime_context=runtime_context,
                    latest_user_message=message,
                    invoke_mcp=invoke_mcp,
                    claim_generator=claim_generator,
                    callbacks=callbacks,
                )
            except (RuntimeError, TypeError, ValueError, WorkCompositionError) as exc:
                return (
                    f"本轮初稿未能保存：{exc} "
                    "请查看当前报告状态；若为证据校验拒绝，可补充适用资料后重新生成，"
                    "也可回复“生成初稿”重试。既有稿件与审批记录保留，未发布新版本。"
                )
        delivery_version = match_report_delivery_version(message)
        if delivery_version is not None:
            try:
                return deliver_report_artifact_action(
                    composition=self.composition,
                    state=state,
                    runtime_context=runtime_context,
                    expected_version=delivery_version,
                    latest_user_message=message,
                )
            except (OSError, TypeError, ValueError, WorkCompositionError) as exc:
                return str(exc)
        return None


def build_playbook(
    *,
    composition: IndustryReportWorkComposition,
    spec: PlaybookSpec,
    service: ServiceCatalogEntry,
    capability_registry: CapabilityRegistry | None = None,
) -> IndustryReportPlaybookBinding:
    if composition.playbook.ref != spec.ref or service.playbook_ref != spec.ref:
        raise ValueError("report Playbook binding inputs do not share one versioned reference")
    return IndustryReportPlaybookBinding(
        spec=spec,
        composition=composition,
        service=service,
        capability_registry=capability_registry,
    )
