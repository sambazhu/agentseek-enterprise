"""Tenant and employee scoped context used by enterprise LangGraph runs."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from typing_extensions import TypedDict

LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY = "_langgraph_runtime_context"
ENTERPRISE_RUNTIME_CONTEXT_KEY = "enterprise"
_SCOPED_KEY_RE = re.compile(r"^(?:hmac|sha256)-[a-f0-9]{64}$")


class EnterpriseIdentityContext(TypedDict):
    """Non-PII identifiers exposed to LangGraph runtime-aware components."""

    version: str
    tenant_id: str
    tenant_key: str
    user_key: str
    session_key: str


@dataclass(frozen=True, slots=True)
class EnterpriseRuntimeContext:
    """Context schema passed from the enterprise plugin into a LangGraph run."""

    enterprise: EnterpriseIdentityContext


@dataclass(frozen=True, slots=True)
class EnterpriseRuntimeSettings:
    """Settings for runtime context and StoreBackend namespace isolation."""

    tenant_id: str
    namespace_secret: str = ""

    @classmethod
    def from_env(cls) -> EnterpriseRuntimeSettings:
        return cls(
            tenant_id=_clean(os.environ.get("AGENTSEEK_ENTERPRISE_TENANT_ID")) or "default",
            namespace_secret=_clean(os.environ.get("AGENTSEEK_ENTERPRISE_NAMESPACE_SECRET")),
        )

    def scoped_key(self, scope: str, value: str) -> str:
        """Return a stable, namespace-safe key without exposing source identifiers."""
        payload = f"{scope}:{value}".encode()
        if self.namespace_secret:
            digest = hmac.new(
                self.namespace_secret.encode("utf-8"), payload, hashlib.sha256
            ).hexdigest()
            return f"hmac-{digest}"
        return f"sha256-{hashlib.sha256(payload).hexdigest()}"


def enterprise_runtime_context(
    employee_context: Mapping[str, object],
    session_id: str,
    *,
    settings: EnterpriseRuntimeSettings | None = None,
) -> dict[str, EnterpriseIdentityContext] | None:
    """Build runtime identifiers after the authoritative employee lookup succeeds.

    OA account and the raw Bub session id deliberately never enter the LangGraph
    context. Components that need a storage namespace receive stable digest keys
    instead, while employee details remain in the existing model-visible state.
    """
    oa_account = _clean(employee_context.get("oa_account"))
    session = _clean(session_id)
    if not oa_account or not session:
        return None

    runtime_settings = settings or EnterpriseRuntimeSettings.from_env()
    return {
        ENTERPRISE_RUNTIME_CONTEXT_KEY: {
            "version": "v1",
            "tenant_id": runtime_settings.tenant_id,
            "tenant_key": runtime_settings.scoped_key("tenant", runtime_settings.tenant_id),
            "user_key": runtime_settings.scoped_key("employee", oa_account),
            "session_key": runtime_settings.scoped_key("session", session),
        }
    }


def enterprise_filesystem_namespace(runtime: Any) -> tuple[str, ...]:
    """Resolve the persistent, per-tenant/per-employee StoreBackend namespace."""
    context = getattr(runtime, "context", None)
    enterprise = (
        context.get(ENTERPRISE_RUNTIME_CONTEXT_KEY)
        if isinstance(context, Mapping)
        else getattr(context, ENTERPRISE_RUNTIME_CONTEXT_KEY, None)
    )
    if not isinstance(enterprise, Mapping):
        raise TypeError("Enterprise runtime context is required for persistent employee memory.")

    version = _clean(enterprise.get("version"))
    tenant_key = _clean(enterprise.get("tenant_key"))
    user_key = _clean(enterprise.get("user_key"))
    if version != "v1" or not _is_scoped_key(tenant_key) or not _is_scoped_key(user_key):
        raise RuntimeError("Enterprise runtime context contains an invalid persistent-memory scope.")
    return ("enterprise", version, tenant_key, user_key, "filesystem")


def _is_scoped_key(value: str) -> bool:
    return bool(_SCOPED_KEY_RE.fullmatch(value))


def _clean(value: object) -> str:
    return str(value or "").strip()
