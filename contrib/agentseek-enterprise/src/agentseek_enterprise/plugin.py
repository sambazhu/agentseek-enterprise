from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from bub import hookimpl
from bub.envelope import content_of, field_of
from bub.turn_admission import AdmitDecision, TurnSnapshot
from bub.types import Envelope, State

from agentseek_enterprise.identity import DmStaffIdentityProvider, EmployeeContext
from agentseek_enterprise.memory import (
    SHORT_TERM_MEMORY_STATE_KEY,
    ShortTermMemorySettings,
    SQLiteShortTermMemoryStore,
    format_short_term_memory_for_prompt,
    short_term_memory_enabled_from_env,
    short_term_memory_state,
)
from agentseek_enterprise.runtime import LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY, enterprise_runtime_context
from agentseek_enterprise.runtime_logging import get_logger

EMPLOYEE_CONTEXT_STATE_KEY = "employee_context"
EMPLOYEE_IDENTITY_STATE_KEY = "_employee_identity"

logger = get_logger(__name__)
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


class ShortTermMemoryStore(Protocol):
    def load_recent_messages(self, session_id: str) -> list[dict[str, Any]]: ...

    def append_turn(self, session_id: str, user_content: str, assistant_content: str) -> None: ...


@dataclass(frozen=True)
class _IdentityCacheEntry:
    context: EmployeeContext
    expires_at: float


class EnterprisePlugin:
    """Bub plugin that injects enterprise runtime context into AgentSeek turns."""

    def __init__(self, framework: object | None = None) -> None:
        del framework
        self._provider: StaffIdentityProvider | None = None
        self._provider_initialized = False
        self._identity_cache: dict[tuple[str, str], _IdentityCacheEntry] = {}
        self._memory_store: ShortTermMemoryStore | None = None
        self._memory_store_initialized = False

    @hookimpl
    def admit_message(
        self,
        session_id: str,
        message: Envelope,
        turn: TurnSnapshot,
    ) -> AdmitDecision | None:
        """Serialize turns within one session.

        Enterprise channels (e.g. WeCom) are 1:1 with an employee: messages from the
        same employee must be processed in arrival order, otherwise concurrent turns
        race on shared per-session state (short-term memory, identity, the WeCom
        stream) and replies can overtake each other. Different sessions stay
        concurrent because Bub schedules each in its own task and passes a
        per-session ``TurnSnapshot``.

        Returns ``follow_up`` when a turn is already running or queued for this
        session; Bub parks the message in the session's pending queue and drains it
        after the active turn finishes. Returns ``None`` when the session is idle so
        Bub's default scheduling applies unchanged.
        """
        del session_id, message  # the decision is driven entirely by the snapshot
        if not _serialize_turns_enabled():
            return None
        if turn.is_running or turn.pending_count > 0:
            return AdmitDecision(action="follow_up", reason="serialize turns per session")
        return None

    @hookimpl
    def load_state(self, message: Envelope, session_id: str) -> State:
        state: State = {}
        state.update(self._load_short_term_memory_state(session_id))
        employee_state = self._load_employee_state(message)
        state.update(employee_state)
        employee_context = employee_state.get(EMPLOYEE_CONTEXT_STATE_KEY)
        if isinstance(employee_context, Mapping):
            runtime_context = enterprise_runtime_context(employee_context, session_id)
            if runtime_context is not None:
                state[LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY] = runtime_context
        return state

    @hookimpl
    def save_state(self, session_id: str, state: State, message: Envelope, model_output: str) -> None:
        del state
        store = self._get_short_term_memory_store()
        if store is None:
            return

        user_content = content_of(message).strip()
        assistant_content = str(model_output or "").strip()
        if not user_content and not assistant_content:
            return
        try:
            store.append_turn(session_id, user_content, assistant_content)
        except Exception as exc:
            logger.warning("Short-term memory save failed for {}: {}", session_id, exc)

    def _load_employee_state(self, message: Envelope) -> State:
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
            context, cache_hit = self._get_employee_context(provider, oa_account)
        except Exception as exc:
            logger.warning("Employee identity lookup failed for {}: {}", oa_account, exc)
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
                "cache": "hit" if cache_hit else "miss",
            },
        }

    def _get_employee_context(
        self,
        provider: StaffIdentityProvider,
        oa_account: str,
    ) -> tuple[EmployeeContext | None, bool]:
        if not _identity_cache_enabled():
            return provider.get_employee_context(oa_account), False

        now = time.monotonic()
        key = (_identity_provider_name(), _identity_cache_key(oa_account))
        cached = self._identity_cache.get(key)
        if cached is not None:
            if cached.expires_at > now:
                logger.debug("Employee identity cache hit for {}", oa_account)
                return cached.context, True
            logger.debug("Employee identity cache expired for {}", oa_account)
            self._identity_cache.pop(key, None)

        context = provider.get_employee_context(oa_account)
        if context is not None:
            ttl = _identity_cache_ttl_seconds()
            self._identity_cache[key] = _IdentityCacheEntry(context=context, expires_at=now + ttl)
            self._prune_identity_cache(now)
            logger.debug("Employee identity cache stored for {} ttl={}s", oa_account, ttl)
        return context, False

    def _prune_identity_cache(self, now: float) -> None:
        max_entries = _identity_cache_max_entries()
        expired_keys = [key for key, entry in self._identity_cache.items() if entry.expires_at <= now]
        for key in expired_keys:
            self._identity_cache.pop(key, None)
        if len(self._identity_cache) <= max_entries:
            return
        ordered = sorted(self._identity_cache.items(), key=lambda item: item[1].expires_at)
        for key, _entry in ordered[: len(self._identity_cache) - max_entries]:
            self._identity_cache.pop(key, None)

    def _load_short_term_memory_state(self, session_id: str) -> State:
        store = self._get_short_term_memory_store()
        if store is None:
            return {}
        try:
            messages = store.load_recent_messages(session_id)
        except Exception as exc:
            logger.warning("Short-term memory load failed for {}: {}", session_id, exc)
            return {"_short_term_memory": {"status": "error", "error_type": type(exc).__name__}}
        if not messages:
            return {}
        return {SHORT_TERM_MEMORY_STATE_KEY: short_term_memory_state(session_id, messages)}

    @hookimpl
    def system_prompt(self, prompt: str | list[dict[str, Any]], state: State) -> str | None:
        del prompt
        lines: list[str] = []

        if _truthy(os.environ.get("AGENTSEEK_ENTERPRISE_IDENTITY_SYSTEM_PROMPT", "false")):
            context = state.get(EMPLOYEE_CONTEXT_STATE_KEY)
            if isinstance(context, Mapping):
                lines.append(format_employee_context_for_prompt(context))

        if _truthy(os.environ.get("AGENTSEEK_ENTERPRISE_MEMORY_SYSTEM_PROMPT", "false")):
            memory_prompt = format_short_term_memory_for_prompt(state.get(SHORT_TERM_MEMORY_STATE_KEY))
            if memory_prompt:
                lines.append(memory_prompt)

        if not lines:
            return None
        return "\n\n".join(lines)

    def _get_identity_provider(self) -> StaffIdentityProvider | None:
        if self._provider_initialized:
            return self._provider
        self._provider_initialized = True

        if _identity_provider_name() != "dm":
            return None
        try:
            self._provider = DmStaffIdentityProvider()
        except Exception as exc:
            logger.warning("Employee identity provider initialization failed: {}", exc)
            self._provider = None
        return self._provider

    def _get_short_term_memory_store(self) -> ShortTermMemoryStore | None:
        if self._memory_store_initialized:
            return self._memory_store
        self._memory_store_initialized = True

        if not short_term_memory_enabled_from_env():
            return None
        try:
            settings = ShortTermMemorySettings.from_env()
            if not settings.enabled:
                return None
            self._memory_store = SQLiteShortTermMemoryStore(settings)
        except Exception as exc:
            logger.warning("Short-term memory store initialization failed: {}", exc)
            self._memory_store = None
        return self._memory_store


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


def _serialize_turns_enabled() -> bool:
    """Whether turns within a session are processed serially. Defaults to enabled."""
    explicit = os.environ.get("AGENTSEEK_ENTERPRISE_SERIALIZE_TURNS")
    if explicit is not None:
        return _truthy(explicit)
    return True


def _identity_enabled() -> bool:
    explicit = os.environ.get("AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED")
    if explicit is not None:
        return _truthy(explicit)
    return _identity_provider_name() == "dm"


def _identity_provider_name() -> str:
    return os.environ.get("AGENTSEEK_IDENTITY_PROVIDER", "").strip().lower()


def _identity_cache_enabled() -> bool:
    explicit = os.environ.get("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED")
    if explicit is None:
        return False
    return _truthy(explicit)


def _identity_cache_ttl_seconds() -> float:
    value = os.environ.get("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_TTL_SECONDS", "600").strip()
    try:
        ttl = float(value)
    except ValueError:
        return 600.0
    return max(1.0, ttl)


def _identity_cache_max_entries() -> int:
    value = os.environ.get("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_MAX_ENTRIES", "1024").strip()
    try:
        max_entries = int(value)
    except ValueError:
        return 1024
    return max(1, max_entries)


def _identity_cache_key(oa_account: str) -> str:
    return oa_account.strip().lower()


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _lookup_path(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        current = current.get(key) if isinstance(current, Mapping) else getattr(current, key, None)
        if current is None:
            return None
    return current


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
