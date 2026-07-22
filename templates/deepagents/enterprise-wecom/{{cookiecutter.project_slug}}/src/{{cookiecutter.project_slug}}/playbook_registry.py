from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from bub.types import Envelope, State

from {{ cookiecutter.project_slug }}.pack_loader import (
    DigitalEmployeeProfile,
    PlaybookSpec,
)


class PlaybookRegistryError(RuntimeError):
    """Raised when declared Playbook bindings violate the Profile boundary."""


@runtime_checkable
class PlaybookBinding(Protocol):
    spec: PlaybookSpec

    @property
    def playbook_ref(self) -> str: ...

    def load_message_state(self, message: Envelope, session_id: str) -> State: ...

    def enrich_state(self, message: Envelope, session_id: str, state: State) -> None: ...

    def tools(self) -> Sequence[Any]: ...

    def guard_output(self, result: object, output: str) -> str: ...

    def current_work(
        self,
        state: Mapping[str, object],
        runtime_context: object | None = None,
    ) -> Any | None: ...

    def introduction(self) -> str: ...


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
        """Return the sole production binding until M3 adds deterministic routing."""

        if len(self._bindings) != 1:
            raise PlaybookRegistryError("Playbook selection is required before invoking a binding")
        return next(iter(self._bindings.values()))

    @property
    def effective_tool_grants(self) -> tuple[str, ...]:
        return self.active_binding().spec.tool_grants

    def load_message_state(self, message: Envelope, session_id: str) -> State:
        return self.active_binding().load_message_state(message, session_id)

    def enrich_state(self, message: Envelope, session_id: str, state: State) -> None:
        self.active_binding().enrich_state(message, session_id, state)

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
