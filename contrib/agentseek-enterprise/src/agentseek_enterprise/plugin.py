from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any, Protocol

from bub import hookimpl
from bub.envelope import field_of
from bub.types import Envelope, State

from agentseek_enterprise.identity import DmStaffIdentityProvider, EmployeeContext

EMPLOYEE_CONTEXT_STATE_KEY = "employee_context"
EMPLOYEE_IDENTITY_STATE_KEY = "_employee_identity"

_LOG = logging.getLogger(__name__)
_OA_ACCOUNT_FIELDS = (
    "oa_account",
    "fdLoginName",
    "fd_login_name",
    "from_userid",
    "FromUserName",
    "from_user_name",
    "userid",
    "user_id",
    "sender_id",
)
_NESTED_OA_ACCOUNT_PATHS = (
    ("context", "oa_account"),
    ("context", "from_userid"),
    ("context", "userid"),
    ("context", "user_id"),
    ("metadata", "oa_account"),
    ("metadata", "from_userid"),
    ("metadata", "userid"),
    ("metadata", "user_id"),
    ("wecom", "from_userid"),
    ("wecom", "userid"),
    ("_wecom", "from_userid"),
    ("_wecom", "userid"),
    ("sender", "oa_account"),
    ("sender", "userid"),
    ("sender", "user_id"),
    ("user", "oa_account"),
    ("user", "userid"),
    ("user", "user_id"),
)


class StaffIdentityProvider(Protocol):
    def get_employee_context(self, oa_account: str) -> EmployeeContext | None: ...


class EnterprisePlugin:
    """Bub plugin that injects enterprise runtime context into AgentSeek turns."""

    def __init__(self, framework: object | None = None) -> None:
        del framework
        self._provider: StaffIdentityProvider | None = None
        self._provider_initialized = False

    @hookimpl
    def load_state(self, message: Envelope, session_id: str) -> State:
        del session_id
        if not _identity_enabled():
            return {}

        oa_account = extract_oa_account(message)
        if oa_account is None:
            return {}

        provider = self._get_identity_provider()
        if provider is None:
            return {
                EMPLOYEE_IDENTITY_STATE_KEY: {
                    "source": _identity_provider_name(),
                    "status": "unavailable",
                    "oa_account": oa_account,
                }
            }

        try:
            context = provider.get_employee_context(oa_account)
        except Exception as exc:
            _LOG.warning("Employee identity lookup failed for %s: %s", oa_account, exc)
            return {
                EMPLOYEE_IDENTITY_STATE_KEY: {
                    "source": _identity_provider_name(),
                    "status": "error",
                    "oa_account": oa_account,
                    "error_type": type(exc).__name__,
                }
            }

        if context is None:
            return {
                EMPLOYEE_IDENTITY_STATE_KEY: {
                    "source": _identity_provider_name(),
                    "status": "not_found",
                    "oa_account": oa_account,
                }
            }

        return {
            EMPLOYEE_CONTEXT_STATE_KEY: context.to_dict(),
            EMPLOYEE_IDENTITY_STATE_KEY: {
                "source": _identity_provider_name(),
                "status": "found",
                "oa_account": context.oa_account,
                "user_id": context.user_id,
            },
        }

    @hookimpl
    def system_prompt(self, prompt: str | list[dict[str, Any]], state: State) -> str | None:
        del prompt
        if not _truthy(os.environ.get("AGENTSEEK_ENTERPRISE_IDENTITY_SYSTEM_PROMPT", "false")):
            return None
        context = state.get(EMPLOYEE_CONTEXT_STATE_KEY)
        if not isinstance(context, Mapping):
            return None
        return format_employee_context_for_prompt(context)

    def _get_identity_provider(self) -> StaffIdentityProvider | None:
        if self._provider_initialized:
            return self._provider
        self._provider_initialized = True

        if _identity_provider_name() != "dm":
            return None
        try:
            self._provider = DmStaffIdentityProvider()
        except Exception as exc:
            _LOG.warning("Employee identity provider initialization failed: %s", exc)
            self._provider = None
        return self._provider


def main(framework: object | None = None) -> EnterprisePlugin:
    return EnterprisePlugin(framework)


def extract_oa_account(message: Envelope) -> str | None:
    """Extract the employee OA account / WeCom userid from common channel envelope shapes."""
    for field_name in _OA_ACCOUNT_FIELDS:
        if value := _clean_text(field_of(message, field_name)):
            return value

    for path in _NESTED_OA_ACCOUNT_PATHS:
        if value := _clean_text(_lookup_path(message, path)):
            return value

    return None


def format_employee_context_for_prompt(context: Mapping[str, Any]) -> str:
    lines = ["[EmployeeContext]"]
    for key, label in (
        ("name", "姓名"),
        ("oa_account", "OA账号"),
        ("belong_to_label", "组织主体"),
        ("primary_org_name", "一级组织"),
        ("org_path_label", "组织路径"),
        ("role_label", "角色"),
        ("dept_name", "部门"),
        ("post", "岗位"),
    ):
        value = _clean_text(context.get(key))
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _identity_enabled() -> bool:
    explicit = os.environ.get("AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED")
    if explicit is not None:
        return _truthy(explicit)
    return _identity_provider_name() == "dm"


def _identity_provider_name() -> str:
    return os.environ.get("AGENTSEEK_IDENTITY_PROVIDER", "").strip().lower()


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _lookup_path(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if isinstance(current, Mapping):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
        if current is None:
            return None
    return current


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
