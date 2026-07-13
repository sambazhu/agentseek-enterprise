from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WorkMode(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    DIRECT_ALLOWED = "direct_allowed"


class SideEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    RISKY = "risky"


class InteractionRoute(StrEnum):
    DIRECT_TURN = "direct_turn"
    WORK_ITEM = "work_item"


@dataclass(frozen=True, slots=True)
class ToolContract:
    name: str
    work_mode: WorkMode
    side_effect: SideEffect
    supports_idempotency: bool

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool contract name must not be blank")
        if not isinstance(self.work_mode, WorkMode):
            raise TypeError("work_mode must be a WorkMode")
        if not isinstance(self.side_effect, SideEffect):
            raise TypeError("side_effect must be a SideEffect")


@dataclass(frozen=True, slots=True)
class RouteRequest:
    tool_contract: ToolContract | None = None
    playbook_work_mode: WorkMode | None = None
    produces_formal_artifact: bool = False
    enters_wait_state: bool = False
    spans_turns_systems_or_phases: bool = False
    requester_requires_tracking: bool = False
    confirmation_completed: bool = False
    idempotency_enabled: bool = False
    mcp_audit_enabled: bool = False


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: InteractionRoute
    reason: str


class ToolContractRegistry:
    def __init__(self, contracts: tuple[ToolContract, ...] = ()) -> None:
        by_name: dict[str, ToolContract] = {}
        for contract in contracts:
            key = contract.name.strip()
            if key in by_name:
                raise ValueError(f"duplicate tool contract: {key}")
            by_name[key] = contract
        self._contracts = by_name

    def resolve(self, name: str) -> ToolContract:
        key = name.strip()
        try:
            return self._contracts[key]
        except KeyError as exc:
            raise KeyError(f"tool contract is not registered: {key}") from exc


def decide_interaction_route(request: RouteRequest) -> RouteDecision:
    """Apply the frozen DirectTurn/WorkItem policy without model discretion."""

    required_reasons = (
        (request.produces_formal_artifact, "formal_artifact"),
        (request.enters_wait_state, "waiting_state"),
        (request.spans_turns_systems_or_phases, "cross_turn_or_phase"),
        (request.requester_requires_tracking, "requester_tracking"),
        (request.playbook_work_mode is WorkMode.REQUIRED, "playbook_required"),
        (
            request.tool_contract is not None and request.tool_contract.work_mode is WorkMode.REQUIRED,
            "tool_required",
        ),
    )
    for required, reason in required_reasons:
        if required:
            return RouteDecision(InteractionRoute.WORK_ITEM, reason)

    contract = request.tool_contract
    if contract is None or contract.side_effect is SideEffect.READ:
        return RouteDecision(InteractionRoute.DIRECT_TURN, "read_or_conversation")

    direct_write_allowed = (
        contract.work_mode is WorkMode.DIRECT_ALLOWED
        and contract.supports_idempotency
        and request.confirmation_completed
        and request.idempotency_enabled
        and request.mcp_audit_enabled
    )
    if direct_write_allowed:
        return RouteDecision(InteractionRoute.DIRECT_TURN, "guarded_direct_write")
    return RouteDecision(InteractionRoute.WORK_ITEM, "write_defaults_to_work")
