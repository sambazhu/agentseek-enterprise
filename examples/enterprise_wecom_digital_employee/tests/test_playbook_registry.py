from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from enterprise_wecom_digital_employee.pack_loader import (
    DigitalEmployeeProfile,
    PlaybookRoutingSpec,
    PlaybookSpec,
    RestrictedPackLoader,
)
from enterprise_wecom_digital_employee.playbook_registry import (
    PlaybookBinding,
    PlaybookRegistry,
    PlaybookRegistryError,
    load_playbook_factory,
)

PROJECT_ROOT = Path(__file__).parents[1]
PACK_ROOT = PROJECT_ROOT / "digital_employees" / "industry-report"
ASSET_REF = "trusted-asset://strategic-report-docx/1.0.0"


@dataclass(frozen=True, slots=True)
class FakeBinding:
    spec: PlaybookSpec
    label: str = "report"

    @property
    def playbook_ref(self) -> str:
        return self.spec.ref

    @property
    def pack_snapshot_id(self) -> str:
        return f"pack_snapshot_test_{self.label}"

    def load_message_state(self, message, session_id: str) -> dict:
        return {"loaded_by": self.label, "session_id": session_id, "message": message}

    def authorize_state(self, state: dict) -> None:
        state["digital_employee_status"] = "found"

    def enrich_state(self, message, session_id: str, state: dict) -> None:
        state["enriched_by"] = self.label

    def tools(self) -> tuple[object, ...]:
        return (f"{self.label}-tool",)

    def guard_output(self, result: object, output: str) -> str:
        return f"{self.label}:{output}"

    def current_work(self, state, runtime_context=None) -> object | None:
        active = state.get("active_playbook_refs", ())
        return {"playbook_ref": self.playbook_ref} if self.playbook_ref in active else None

    def introduction(self) -> str:
        return f"{self.label} introduction"

    def instructions(self) -> str:
        return f"{self.label} instructions"


def load_profile_and_playbook() -> tuple[DigitalEmployeeProfile, PlaybookSpec]:
    def resolve_asset(artifact_ref: str) -> Path:
        assert artifact_ref == ASSET_REF
        return PACK_ROOT / "assets" / "neutral-industry-report-v1.docx"

    loaded = RestrictedPackLoader(
        pack_root=PACK_ROOT,
        allowed_entrypoint_package="enterprise_wecom_digital_employee",
        asset_resolver=resolve_asset,
    ).load()
    return loaded.profile, loaded.playbooks[0]


def test_single_binding_delegates_runtime_contract(monkeypatch) -> None:
    profile, spec = load_profile_and_playbook()
    registry = PlaybookRegistry(profile=profile, bindings=(FakeBinding(spec),))
    state: dict[str, object] = {"active_playbook_refs": (spec.ref,)}
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "enterprise_wecom_digital_employee.playbook_registry.emit_enterprise_event",
        lambda name, **payload: events.append((name, payload)),
    )

    assert registry.playbook_refs == ("securities-industry-report@1",)
    assert registry.active_binding().playbook_ref == spec.ref
    assert registry.effective_tool_grants == spec.tool_grants
    assert registry.load_message_state({"content": "hello"}, "session-1") == {}
    registry.enrich_state({"content": "查看当前报告任务状态"}, "session-1", state)
    assert state["enriched_by"] == "report"
    assert state["playbook_route"] == {
        "route_status": "selected",
        "reason_code": "exact_action",
        "playbook_ref": spec.ref,
        "candidate_playbook_refs": [spec.ref],
        "digital_employee_id": profile.digital_employee_id,
        "profile_version": profile.profile_version,
        "pack_snapshot_id": "pack_snapshot_test_report",
    }
    assert registry.tools() == ("report-tool",)
    assert registry.tools_for(spec.ref) == ("report-tool",)
    assert registry.guard_output(object(), "answer") == "report:answer"
    assert registry.current_work(state) == {"playbook_ref": spec.ref}
    assert registry.introduction() == "report introduction"
    assert events[0][0] == "digital_employee_playbook_routed"
    assert "message" not in events[0][1]


def test_multi_binding_registry_requires_m3_selection_before_invocation() -> None:
    profile, spec = load_profile_and_playbook()
    second = replace(
        spec,
        playbook_id="department-summary",
        routing=PlaybookRoutingSpec(
            explicit_aliases=("部门简报",),
            intent_terms=("部门简报",),
            owned_command_terms=("简报任务状态",),
            priority=90,
        ),
    )
    multi_profile = replace(
        profile,
        supported_playbooks=(spec.ref, second.ref),
    )
    registry = PlaybookRegistry(
        profile=multi_profile,
        bindings=(FakeBinding(spec), FakeBinding(second, label="summary")),
    )

    assert registry.get(second.ref).introduction() == "summary introduction"
    with pytest.raises(PlaybookRegistryError, match="selection is required"):
        registry.active_binding()


def test_multi_binding_routes_and_enriches_only_the_selected_playbook(monkeypatch) -> None:
    profile, spec = load_profile_and_playbook()
    second = replace(
        spec,
        playbook_id="department-summary",
        routing=PlaybookRoutingSpec(
            explicit_aliases=("部门简报",),
            intent_terms=("部门经营情况",),
            owned_command_terms=("简报任务状态",),
            priority=90,
        ),
    )
    multi_profile = replace(profile, supported_playbooks=(spec.ref, second.ref))
    registry = PlaybookRegistry(
        profile=multi_profile,
        bindings=(FakeBinding(spec), FakeBinding(second, label="summary")),
    )
    monkeypatch.setattr(
        "enterprise_wecom_digital_employee.playbook_registry.emit_enterprise_event",
        lambda *_args, **_kwargs: None,
    )
    state: dict[str, object] = {"active_playbook_refs": (spec.ref, second.ref)}

    registry.enrich_state({"content": "查看简报任务状态"}, "session-1", state)

    route = cast(dict[str, object], state["playbook_route"])
    assert state["enriched_by"] == "summary"
    assert state["loaded_by"] == "summary"
    assert route["playbook_ref"] == second.ref
    assert registry.tools_for(spec.ref) == ("report-tool",)
    assert registry.tools_for(second.ref) == ("summary-tool",)
    assert registry.guard_for(second.ref, object(), "answer") == "summary:answer"


def test_multi_binding_ambiguity_loads_no_playbook_context(monkeypatch) -> None:
    profile, spec = load_profile_and_playbook()
    second = replace(
        spec,
        playbook_id="department-summary",
        routing=PlaybookRoutingSpec(
            explicit_aliases=("部门简报",),
            intent_terms=("编写一份报告",),
            owned_command_terms=("简报任务状态",),
            priority=1,
        ),
    )
    multi_profile = replace(profile, supported_playbooks=(spec.ref, second.ref))
    registry = PlaybookRegistry(
        profile=multi_profile,
        bindings=(FakeBinding(spec), FakeBinding(second, label="summary")),
    )
    monkeypatch.setattr(
        "enterprise_wecom_digital_employee.playbook_registry.emit_enterprise_event",
        lambda *_args, **_kwargs: None,
    )
    state: dict[str, object] = {"active_playbook_refs": (spec.ref, second.ref)}

    registry.enrich_state({"content": "请帮我编写一份报告"}, "session-1", state)

    route = cast(dict[str, object], state["playbook_route"])
    assert "enriched_by" not in state
    assert "loaded_by" not in state
    assert route["route_status"] == "clarification_required"
    assert set(cast(list[str], route["candidate_playbook_refs"])) == {spec.ref, second.ref}


def test_registry_rejects_missing_duplicate_and_permission_escalation() -> None:
    profile, spec = load_profile_and_playbook()

    with pytest.raises(PlaybookRegistryError, match="missing=securities-industry-report@1"):
        PlaybookRegistry(profile=profile, bindings=())
    with pytest.raises(PlaybookRegistryError, match="duplicate Playbook binding"):
        PlaybookRegistry(profile=profile, bindings=(FakeBinding(spec), FakeBinding(spec)))
    with pytest.raises(PlaybookRegistryError, match="invalid binding"):
        PlaybookRegistry(profile=profile, bindings=(cast(PlaybookBinding, object()),))

    escalated = replace(spec, tool_grants=(*spec.tool_grants, "unapproved-tool"))
    with pytest.raises(PlaybookRegistryError, match="tool_grants exceeds Profile permissions"):
        PlaybookRegistry(profile=profile, bindings=(FakeBinding(escalated),))


def test_registry_keeps_profile_v1_policy_inheritance_compatible() -> None:
    profile, spec = load_profile_and_playbook()
    legacy = replace(
        profile,
        profile_schema_version=1,
        behavior_policy_refs=(),
        service_catalog=(),
    )

    registry = PlaybookRegistry(profile=legacy, bindings=(FakeBinding(spec),))

    assert registry.active_binding().spec.policy_refs == ("industry-report-v1",)


def test_entrypoint_loader_is_package_scoped() -> None:
    factory = load_playbook_factory(
        "enterprise_wecom_digital_employee.reports.playbook:build_playbook",
        allowed_package="enterprise_wecom_digital_employee",
    )

    assert factory.__name__ == "build_playbook"
    with pytest.raises(PlaybookRegistryError, match="outside the allowed package"):
        load_playbook_factory(
            "outside_package.playbook:build_playbook",
            allowed_package="enterprise_wecom_digital_employee",
        )
