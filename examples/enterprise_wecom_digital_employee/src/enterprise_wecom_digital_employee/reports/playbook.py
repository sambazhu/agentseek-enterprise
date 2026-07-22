from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agentseek_work import WorkItem
from bub.types import Envelope, State

from enterprise_wecom_digital_employee.pack_loader import PlaybookSpec, ServiceCatalogEntry
from enterprise_wecom_digital_employee.report_output_guard import enforce_m2_output_guard
from enterprise_wecom_digital_employee.work_composition import IndustryReportWorkComposition
from enterprise_wecom_digital_employee.work_tools import work_tools


@dataclass(frozen=True, slots=True)
class IndustryReportPlaybookBinding:
    spec: PlaybookSpec
    composition: IndustryReportWorkComposition
    service: ServiceCatalogEntry

    @property
    def playbook_ref(self) -> str:
        return self.spec.ref

    def load_message_state(self, message: Envelope, session_id: str) -> State:
        return self.composition.load_message_state(message, session_id)

    def enrich_state(self, message: Envelope, session_id: str, state: State) -> None:
        self.composition.enrich_state(message, session_id, state)

    def tools(self) -> Sequence[Any]:
        return work_tools(self.composition)

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


def build_playbook(
    *,
    composition: IndustryReportWorkComposition,
    spec: PlaybookSpec,
    service: ServiceCatalogEntry,
) -> IndustryReportPlaybookBinding:
    if composition.playbook.ref != spec.ref or service.playbook_ref != spec.ref:
        raise ValueError("report Playbook binding inputs do not share one versioned reference")
    return IndustryReportPlaybookBinding(spec=spec, composition=composition, service=service)
