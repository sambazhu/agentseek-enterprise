from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from agentseek_enterprise.runtime_logging import get_logger

logger = get_logger(__name__)

_DEFAULT_EVENTS_LOG_PATH = "./runtime/enterprise-events.jsonl"
_SENSITIVE_KEY_FRAGMENTS = (
    "access_token",
    "apikey",
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "encoding_aes_key",
    "passwd",
    "password",
    "private_key",
    "secret",
    "token",
)
_IDENTITY_FIELD_MAP = {
    "chat_id": "chat_key",
    "employee": "employee_key",
    "employee_id": "employee_key",
    "from_userid": "from_user_key",
    "namespace": "namespace_key",
    "oa_account": "employee_key",
    "open_userid": "open_user_key",
    "scope": "scope_key",
    "session_id": "session_key",
    "user_id": "user_key",
    "userid": "user_key",
}


@dataclass(frozen=True)
class EnterpriseObservabilitySettings:
    """Settings for enterprise runtime event emission.

    The event writer is intentionally best-effort. Observability must never make
    a WeCom turn fail, so every emission path catches and logs exceptions.
    """

    events_enabled: bool = False
    events_log_path: Path = Path(_DEFAULT_EVENTS_LOG_PATH)
    max_value_chars: int = 500
    hash_secret: str = ""
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""
    langfuse_environment: str = ""
    langfuse_release: str = ""
    langfuse_sample_rate: float = 1.0

    @classmethod
    def from_env(cls, *, project_root: str | Path | None = None) -> EnterpriseObservabilitySettings:
        root = _resolve_project_root(project_root)
        events_log_path = _resolve_path(
            os.environ.get("AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH", _DEFAULT_EVENTS_LOG_PATH),
            root,
        )
        return cls(
            events_enabled=_truthy(os.environ.get("AGENTSEEK_ENTERPRISE_EVENTS_ENABLED", "false")),
            events_log_path=events_log_path,
            max_value_chars=_bounded_int(os.environ.get("AGENTSEEK_ENTERPRISE_EVENTS_MAX_VALUE_CHARS"), 80, 10_000, 500),
            hash_secret=(
                os.environ.get("AGENTSEEK_ENTERPRISE_EVENTS_HASH_SECRET")
                or os.environ.get("AGENTSEEK_ENTERPRISE_NAMESPACE_SECRET")
                or ""
            ),
            langfuse_enabled=_truthy(os.environ.get("AGENTSEEK_LANGFUSE_ENABLED", "false")),
            langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip(),
            langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY", "").strip(),
            langfuse_host=os.environ.get("LANGFUSE_HOST", "").strip(),
            langfuse_environment=os.environ.get("AGENTSEEK_LANGFUSE_ENV", "").strip(),
            langfuse_release=os.environ.get("AGENTSEEK_LANGFUSE_RELEASE", "").strip(),
            langfuse_sample_rate=_bounded_float(os.environ.get("AGENTSEEK_LANGFUSE_SAMPLE_RATE"), 0.0, 1.0, 1.0),
        )


class EnterpriseEventWriter:
    def __init__(
        self,
        settings: EnterpriseObservabilitySettings | None = None,
        *,
        project_root: str | Path | None = None,
    ) -> None:
        self.settings = settings or EnterpriseObservabilitySettings.from_env(project_root=project_root)
        self._langfuse = _LangfuseEmitter(self.settings)

    def emit(self, event: str, **fields: Any) -> None:
        if not event:
            return
        if not self.settings.events_enabled and not self.settings.langfuse_enabled:
            return
        try:
            payload = self._payload(event, fields)
            if self.settings.events_enabled:
                self._write_jsonl(payload)
            self._langfuse.emit(payload)
        except Exception as exc:  # pragma: no cover - defensive, must never break runtime.
            logger.warning("enterprise event emission failed event={} error={}", event, exc)

    def identity_key(self, value: Any) -> str:
        return _stable_hash(value, secret=self.settings.hash_secret)

    def _payload(self, event: str, fields: dict[str, Any]) -> dict[str, Any]:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "event": str(event),
            **_sanitize_fields(fields, secret=self.settings.hash_secret, max_chars=self.settings.max_value_chars),
        }

    def _write_jsonl(self, payload: dict[str, Any]) -> None:
        path = self.settings.events_log_path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


class _LangfuseEmitter:
    def __init__(self, settings: EnterpriseObservabilitySettings) -> None:
        self._settings = settings
        self._disabled = False
        self._client: Any | None = None

    def emit(self, payload: dict[str, Any]) -> None:
        if self._disabled or not self._settings.langfuse_enabled:
            return
        if (secrets.randbelow(1_000_000) / 1_000_000) > self._settings.langfuse_sample_rate:
            return
        try:
            client = self._get_client()
            if client is None:
                return
            metadata = dict(payload)
            event_name = str(metadata.pop("event", "agentseek.enterprise.event"))
            metadata.setdefault("environment", self._settings.langfuse_environment)
            metadata.setdefault("release", self._settings.langfuse_release)
            trace = client.trace(name="agentseek.enterprise", metadata=metadata)
            span = trace.span(name=event_name, metadata=metadata) if hasattr(trace, "span") else None
            if span is not None and hasattr(span, "end"):
                span.end()
            if hasattr(client, "flush"):
                client.flush()
        except Exception as exc:  # pragma: no cover - depends on optional SDK/API version.
            self._disabled = True
            logger.warning("Langfuse enterprise event emission disabled after error: {}", exc)

    def _get_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not self._settings.langfuse_public_key or not self._settings.langfuse_secret_key:
            logger.warning("AGENTSEEK_LANGFUSE_ENABLED=true but Langfuse keys are not configured")
            self._disabled = True
            return None
        try:
            from langfuse import Langfuse  # type: ignore[import-not-found]
        except ImportError:
            logger.warning("AGENTSEEK_LANGFUSE_ENABLED=true but the langfuse package is not installed")
            self._disabled = True
            return None
        kwargs: dict[str, str] = {
            "public_key": self._settings.langfuse_public_key,
            "secret_key": self._settings.langfuse_secret_key,
        }
        if self._settings.langfuse_host:
            kwargs["host"] = self._settings.langfuse_host
        self._client = Langfuse(**kwargs)
        return self._client


@lru_cache(maxsize=1)
def get_event_writer() -> EnterpriseEventWriter:
    return EnterpriseEventWriter()


def emit_enterprise_event(event: str, **fields: Any) -> None:
    get_event_writer().emit(event, **fields)


def enterprise_identity_key(value: Any) -> str:
    return get_event_writer().identity_key(value)


def event_timer() -> float:
    return time.monotonic()


def elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


def reset_observability_for_tests() -> None:
    get_event_writer.cache_clear()


def _sanitize_fields(fields: dict[str, Any], *, secret: str, max_chars: int) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        key_text = str(key)
        lowered = key_text.lower()
        mapped_key = _IDENTITY_FIELD_MAP.get(lowered)
        if mapped_key is not None:
            safe[mapped_key] = _stable_hash(value, secret=secret)
        elif any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS):
            safe[key_text] = "[REDACTED]"
        else:
            safe[key_text] = _sanitize_value(value, secret=secret, max_chars=max_chars)
    return safe


def _sanitize_value(value: Any, *, secret: str, max_chars: int) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value) > max_chars:
            return value[:max_chars] + "...[truncated]"
        return value
    if isinstance(value, dict):
        return _sanitize_fields(dict(value), secret=secret, max_chars=max_chars)
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(item, secret=secret, max_chars=max_chars) for item in list(value)[:20]]
    text = str(value)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text


def _stable_hash(value: Any, *, secret: str) -> str:
    text = "|".join(str(part) for part in value) if isinstance(value, tuple) else str(value or "")
    key = secret.encode("utf-8")
    digest = hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest() if key else hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()
    return f"hmac-{digest[:24]}" if key else f"sha256-{digest[:24]}"


def _resolve_project_root(project_root: str | Path | None) -> Path:
    if project_root is not None:
        return Path(project_root).expanduser().resolve()
    explicit = os.environ.get("AGENTSEEK_ENTERPRISE_PROJECT_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_file = os.environ.get("AGENTSEEK_ENV_FILE", "").strip()
    if env_file:
        path = Path(env_file).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.parent.resolve()
    return Path.cwd().resolve()


def _resolve_path(value: str | Path, project_root: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return project_root / path


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(value: str | None, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: str | None, minimum: float, maximum: float, default: float) -> float:
    try:
        parsed = float(str(value or "").strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))
