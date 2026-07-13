import pytest
from agentseek_work.routing import (
    InteractionRoute,
    RouteRequest,
    SideEffect,
    ToolContract,
    ToolContractRegistry,
    WorkMode,
    decide_interaction_route,
)


def contract(
    *,
    work_mode: WorkMode = WorkMode.OPTIONAL,
    side_effect: SideEffect = SideEffect.READ,
    supports_idempotency: bool = True,
) -> ToolContract:
    return ToolContract(
        name="example_tool",
        work_mode=work_mode,
        side_effect=side_effect,
        supports_idempotency=supports_idempotency,
    )


@pytest.mark.parametrize(
    ("route_request", "reason"),
    [
        (RouteRequest(produces_formal_artifact=True), "formal_artifact"),
        (RouteRequest(enters_wait_state=True), "waiting_state"),
        (RouteRequest(spans_turns_systems_or_phases=True), "cross_turn_or_phase"),
        (RouteRequest(requester_requires_tracking=True), "requester_tracking"),
    ],
)
def test_frozen_required_conditions_always_route_to_work(route_request: RouteRequest, reason: str) -> None:
    decision = decide_interaction_route(route_request)

    assert decision.route is InteractionRoute.WORK_ITEM
    assert decision.reason == reason


def test_required_playbook_and_tool_cannot_be_downgraded() -> None:
    playbook = decide_interaction_route(RouteRequest(playbook_work_mode=WorkMode.REQUIRED))
    tool = decide_interaction_route(
        RouteRequest(
            tool_contract=contract(work_mode=WorkMode.REQUIRED, side_effect=SideEffect.WRITE),
            confirmation_completed=True,
            idempotency_enabled=True,
            mcp_audit_enabled=True,
        )
    )

    assert playbook.route is InteractionRoute.WORK_ITEM
    assert tool.route is InteractionRoute.WORK_ITEM


def test_read_and_plain_conversation_remain_direct() -> None:
    assert decide_interaction_route(RouteRequest()).route is InteractionRoute.DIRECT_TURN
    assert (
        decide_interaction_route(RouteRequest(tool_contract=contract(side_effect=SideEffect.READ))).route
        is InteractionRoute.DIRECT_TURN
    )


def test_write_defaults_to_work_item() -> None:
    decision = decide_interaction_route(
        RouteRequest(tool_contract=contract(work_mode=WorkMode.OPTIONAL, side_effect=SideEffect.WRITE))
    )

    assert decision.route is InteractionRoute.WORK_ITEM
    assert decision.reason == "write_defaults_to_work"


@pytest.mark.parametrize(
    "missing_guard",
    ["confirmation_completed", "idempotency_enabled", "mcp_audit_enabled"],
)
def test_direct_write_requires_every_runtime_guard(missing_guard: str) -> None:
    decision = decide_interaction_route(
        RouteRequest(
            tool_contract=contract(work_mode=WorkMode.DIRECT_ALLOWED, side_effect=SideEffect.WRITE),
            confirmation_completed=missing_guard != "confirmation_completed",
            idempotency_enabled=missing_guard != "idempotency_enabled",
            mcp_audit_enabled=missing_guard != "mcp_audit_enabled",
        )
    )

    assert decision.route is InteractionRoute.WORK_ITEM


def test_guarded_direct_write_requires_contract_idempotency_support() -> None:
    denied = decide_interaction_route(
        RouteRequest(
            tool_contract=contract(
                work_mode=WorkMode.DIRECT_ALLOWED,
                side_effect=SideEffect.WRITE,
                supports_idempotency=False,
            ),
            confirmation_completed=True,
            idempotency_enabled=True,
            mcp_audit_enabled=True,
        )
    )
    allowed = decide_interaction_route(
        RouteRequest(
            tool_contract=contract(work_mode=WorkMode.DIRECT_ALLOWED, side_effect=SideEffect.WRITE),
            confirmation_completed=True,
            idempotency_enabled=True,
            mcp_audit_enabled=True,
        )
    )

    assert denied.route is InteractionRoute.WORK_ITEM
    assert allowed.route is InteractionRoute.DIRECT_TURN
    assert allowed.reason == "guarded_direct_write"


def test_registry_is_exact_and_rejects_duplicates() -> None:
    registered = contract()
    registry = ToolContractRegistry((registered,))

    assert registry.resolve("example_tool") is registered
    with pytest.raises(KeyError, match="not registered"):
        registry.resolve("unknown")
    with pytest.raises(ValueError, match="duplicate"):
        ToolContractRegistry((registered, registered))
