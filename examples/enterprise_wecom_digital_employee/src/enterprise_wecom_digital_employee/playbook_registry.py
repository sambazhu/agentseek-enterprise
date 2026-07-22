from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from agentseek_enterprise.observability import emit_enterprise_event
from bub.envelope import content_of
from bub.types import Envelope, State

from enterprise_wecom_digital_employee.pack_loader import (
    DigitalEmployeeProfile,
    PlaybookSpec,
)
from enterprise_wecom_digital_employee.playbook_router import (
    PlaybookRouteReason,
    PlaybookRouteResult,
    PlaybookRouteStatus,
    render_route_clarification,
    route_playbook,
)


class PlaybookRegistryError(RuntimeError):
    """Raised when declared Playbook bindings violate the Profile boundary."""


@runtime_checkable
class PlaybookBinding(Protocol):
    spec: PlaybookSpec

    @property
    def playbook_ref(self) -> str: ...

    @property
    def pack_snapshot_id(self) -> str: ...

    def load_message_state(self, message: Envelope, session_id: str) -> State: ...

    def authorize_state(self, state: State) -> None: ...

    def enrich_state(self, message: Envelope, session_id: str, state: State) -> None: ...

    def tools(self) -> Sequence[Any]: ...

    def guard_output(self, result: object, output: str) -> str: ...

    def current_work(
        self,
        state: Mapping[str, object],
        runtime_context: object | None = None,
    ) -> Any | None: ...

    def introduction(self) -> str: ...

    def instructions(self) -> str: ...


class PlaybookRegistry:
    """Versioned Profile-scoped registry for independently governed Playbooks."""

    def __init__(
        self,
        *,
        profile: DigitalEmployeeProfile,
        bindings: Sequence[PlaybookBinding],
    ) -> None:
        by_ref: dict[str, PlaybookBinding] = {}
        for binding in bindings:
            if not isinstance(binding, PlaybookBinding):
                raise PlaybookRegistryError("Playbook factory returned an invalid binding")
            if binding.playbook_ref in by_ref:
                raise PlaybookRegistryError(f"duplicate Playbook binding: {binding.playbook_ref}")
            _validate_binding_permissions(profile, binding)
            by_ref[binding.playbook_ref] = binding
        declared = set(profile.supported_playbooks)
        registered = set(by_ref)
        if declared != registered:
            missing = sorted(declared - registered)
            extra = sorted(registered - declared)
            details = ", ".join([*(f"missing={item}" for item in missing), *(f"extra={item}" for item in extra)])
            raise PlaybookRegistryError(f"Playbook bindings do not match Profile: {details}")
        self.profile = profile
        self._bindings = MappingProxyType({reference: by_ref[reference] for reference in profile.supported_playbooks})

    @property
    def playbook_refs(self) -> tuple[str, ...]:
        return tuple(self._bindings)

    def get(self, playbook_ref: str) -> PlaybookBinding:
        try:
            return self._bindings[playbook_ref]
        except KeyError as exc:
            raise PlaybookRegistryError(f"Playbook is not registered: {playbook_ref}") from exc

    def active_binding(self) -> PlaybookBinding:
        """Return the sole binding for backward-compatible single-Playbook callers."""

        if len(self._bindings) != 1:
            raise PlaybookRegistryError("Playbook selection is required before invoking a binding")
        return next(iter(self._bindings.values()))

    @property
    def effective_tool_grants(self) -> tuple[str, ...]:
        return self.active_binding().spec.tool_grants

    def load_message_state(self, message: Envelope, session_id: str) -> State:
        del message, session_id
        return {}

    def enrich_state(self, message: Envelope, session_id: str, state: State) -> None:
        identity_binding = next(iter(self._bindings.values()))
        identity_binding.authorize_state(state)
        requester_allowed = state.get("digital_employee_status") == "found"
        active_refs = tuple(
            reference
            for reference, binding in self._bindings.items()
            if requester_allowed and binding.current_work(state) is not None
        )
        result = route_playbook(
            content_of(message),
            playbooks=tuple(binding.spec for binding in self._bindings.values()),
            active_playbook_refs=active_refs,
            requester_allowed=requester_allowed,
        )
        route_state = result.to_state()
        route_state.update({
            "digital_employee_id": self.profile.digital_employee_id,
            "profile_version": self.profile.profile_version,
        })
        if result.selected_playbook_ref is not None:
            selected_binding = self.get(result.selected_playbook_ref)
            pack_snapshot_id = selected_binding.pack_snapshot_id.strip()
            if pack_snapshot_id:
                route_state["pack_snapshot_id"] = pack_snapshot_id
        state["playbook_route"] = route_state
        emit_enterprise_event(
            "digital_employee_playbook_routed",
            status="succeeded",
            session_id=session_id,
            digital_employee_id=self.profile.digital_employee_id,
            profile_version=self.profile.profile_version,
            pack_snapshot_id=str(route_state.get("pack_snapshot_id", "")),
            route_status=result.status.value,
            reason_code=result.reason_code.value,
            playbook_ref=result.selected_playbook_ref or "",
            candidate_playbook_refs=list(result.candidate_playbook_refs),
        )
        if result.selected_playbook_ref is None:
            return
        binding = self.get(result.selected_playbook_ref)
        state.update(binding.load_message_state(message, session_id))
        binding.enrich_state(message, session_id, state)

    def tools(self) -> tuple[Any, ...]:
        return tuple(self.active_binding().tools())

    def guard_output(self, result: object, output: str) -> str:
        return self.active_binding().guard_output(result, output)

    def current_work(
        self,
        state: Mapping[str, object],
        runtime_context: object | None = None,
    ) -> Any | None:
        return self.active_binding().current_work(state, runtime_context)

    def introduction(self) -> str:
        return self.active_binding().introduction()

    def binding_for_state(self, state: Mapping[str, object]) -> PlaybookBinding | None:
        route = _route_from_state(state)
        if route is None or route.selected_playbook_ref is None:
            return None
        return self.get(route.selected_playbook_ref)

    def route_clarification(self, state: Mapping[str, object]) -> str | None:
        route = _route_from_state(state)
        if route is None:
            return None
        titles = {service.playbook_ref: service.title for service in self.profile.service_catalog}
        return render_route_clarification(route, service_titles=titles)

    def tools_for(self, playbook_ref: str) -> tuple[Any, ...]:
        return tuple(self.get(playbook_ref).tools())

    def guard_for(self, playbook_ref: str, result: object, output: str) -> str:
        return self.get(playbook_ref).guard_output(result, output)


def load_playbook_factory(entrypoint: str, *, allowed_package: str) -> Any:
    module_name, separator, attribute = entrypoint.partition(":")
    if (
        separator != ":"
        or not attribute.isidentifier()
        or not (module_name == allowed_package or module_name.startswith(f"{allowed_package}."))
    ):
        raise PlaybookRegistryError("Playbook entrypoint is outside the allowed package")
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
    except (AttributeError, ImportError) as exc:
        raise PlaybookRegistryError(f"Playbook entrypoint cannot be loaded: {entrypoint}") from exc
    if not callable(factory):
        raise PlaybookRegistryError(f"Playbook entrypoint is not callable: {entrypoint}")
    return factory


def _validate_binding_permissions(
    profile: DigitalEmployeeProfile,
    binding: PlaybookBinding,
) -> None:
    spec = binding.spec
    permitted_policies = (
        profile.behavior_policy_refs
        if profile.profile_schema_version == 2
        else spec.policy_refs
    )
    boundaries = (
        ("skill_refs", spec.skill_refs, profile.skill_refs),
        ("policy_refs", spec.policy_refs, permitted_policies),
        ("tool_grants", spec.tool_grants, profile.tool_grants),
        ("data_scopes", spec.data_scopes, profile.data_scopes),
    )
    for label, requested, permitted in boundaries:
        excess = sorted(set(requested) - set(permitted))
        if excess:
            raise PlaybookRegistryError(
                f"{binding.playbook_ref} {label} exceeds Profile permissions: {', '.join(excess)}"
            )


def _route_from_state(state: Mapping[str, object]) -> PlaybookRouteResult | None:
    value = state.get("playbook_route")
    if not isinstance(value, Mapping):
        return None
    candidates = value.get("candidate_playbook_refs")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        return None
    try:
        return PlaybookRouteResult(
            status=PlaybookRouteStatus(str(value.get("route_status", ""))),
            reason_code=PlaybookRouteReason(str(value.get("reason_code", ""))),
            selected_playbook_ref=str(value.get("playbook_ref") or "") or None,
            candidate_playbook_refs=tuple(str(item) for item in candidates),
        )
    except (TypeError, ValueError):
        return None
