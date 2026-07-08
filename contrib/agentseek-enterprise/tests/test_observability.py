from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import Any

import pytest
from agentseek_enterprise.observability import (
    EnterpriseEventWriter,
    EnterpriseObservabilitySettings,
    emit_enterprise_event,
    reset_observability_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_writer_cache() -> Any:
    reset_observability_for_tests()
    yield
    reset_observability_for_tests()


def test_enterprise_events_disabled_by_default(monkeypatch: Any, tmp_path: Any) -> None:
    log_path = tmp_path / "enterprise-events.jsonl"
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_EVENTS_ENABLED", raising=False)
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH", str(log_path))
    reset_observability_for_tests()

    emit_enterprise_event("identity_lookup", oa_account="zhuchunlin")

    assert not log_path.exists()


def test_enterprise_events_write_redacted_jsonl(monkeypatch: Any, tmp_path: Any) -> None:
    log_path = tmp_path / "enterprise-events.jsonl"
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_ENABLED", "true")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH", str(log_path))
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_HASH_SECRET", "test-secret")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_MAX_VALUE_CHARS", "80")
    reset_observability_for_tests()

    emit_enterprise_event(
        "identity_lookup",
        status="found",
        oa_account="zhuchunlin",
        session_id="wecom:zhuchunlin",
        password="plain-password",
        metadata={"api_key": "secret-key", "note": "x" * 90},
    )

    redacted = "[REDACTED]"
    payload = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert payload["event"] == "identity_lookup"
    assert payload["status"] == "found"
    assert payload["employee_key"].startswith("hmac-")
    assert payload["session_key"].startswith("hmac-")
    assert payload["password"] == redacted
    assert payload["metadata"]["api_key"] == redacted
    assert payload["metadata"]["note"] == ("x" * 80) + "...[truncated]"
    assert "zhuchunlin" not in log_path.read_text(encoding="utf-8")
    assert "plain-password" not in log_path.read_text(encoding="utf-8")


def test_langfuse_enabled_without_package_does_not_break_local_events(monkeypatch: Any, tmp_path: Any) -> None:
    log_path = tmp_path / "enterprise-events.jsonl"
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_ENABLED", "true")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH", str(log_path))
    monkeypatch.setenv("AGENTSEEK_LANGFUSE_ENABLED", "true")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    reset_observability_for_tests()

    emit_enterprise_event("wecom_message_received", session_id="wecom:zhuchunlin")

    assert json.loads(log_path.read_text(encoding="utf-8").strip())["event"] == "wecom_message_received"


def test_langfuse_enabled_emits_trace_event_with_fake_sdk(monkeypatch: Any, tmp_path: Any) -> None:
    log_path = tmp_path / "enterprise-events.jsonl"
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeTrace:
        def event(self, *, name: str, metadata: dict[str, Any]) -> None:
            calls.append((name, metadata))

    class FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(("client", kwargs))

        def trace(self, *, name: str, metadata: dict[str, Any]) -> FakeTrace:
            calls.append((name, metadata))
            return FakeTrace()

        def flush(self) -> None:
            calls.append(("flush", {}))

    fake_module = ModuleType("langfuse")
    fake_module.Langfuse = FakeLangfuse
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_ENABLED", "true")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH", str(log_path))
    monkeypatch.setenv("AGENTSEEK_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse.local")
    monkeypatch.setenv("AGENTSEEK_LANGFUSE_TRACE_NAME", "agentseek.test")
    monkeypatch.setenv("AGENTSEEK_LANGFUSE_ENV", "test")
    monkeypatch.setenv("AGENTSEEK_LANGFUSE_RELEASE", "v-test")
    monkeypatch.setenv("AGENTSEEK_LANGFUSE_SAMPLE_RATE", "1.0")
    reset_observability_for_tests()

    writer = EnterpriseEventWriter()
    assert writer.emit("identity_lookup", oa_account="zhuchunlin", status="found")

    assert calls[0] == (
        "client",
        {"public_key": "pk-test", "secret_key": "sk-test", "host": "http://langfuse.local"},
    )
    assert calls[1][0] == "agentseek.test"
    assert calls[2][0] == "identity_lookup"
    assert calls[-1] == ("flush", {})
    assert writer.langfuse_status() == {"status": "sent"}
    assert "zhuchunlin" not in json.dumps(calls, ensure_ascii=False)


def test_relative_event_path_resolves_against_explicit_project_root(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH", "./runtime/enterprise-events.jsonl")

    settings = EnterpriseObservabilitySettings.from_env(project_root=tmp_path / "project")

    assert settings.events_log_path == tmp_path / "project" / "runtime" / "enterprise-events.jsonl"


def test_relative_event_path_resolves_against_agentseek_env_file(monkeypatch: Any, tmp_path: Any) -> None:
    project_root = tmp_path / "examples" / "enterprise_wecom_digital_employee"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTSEEK_ENV_FILE", "examples/enterprise_wecom_digital_employee/.env")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH", "./runtime/enterprise-events.jsonl")

    settings = EnterpriseObservabilitySettings.from_env()

    assert settings.events_log_path == project_root / "runtime" / "enterprise-events.jsonl"


def test_absolute_event_path_is_not_rebased(monkeypatch: Any, tmp_path: Any) -> None:
    absolute_path = tmp_path / "logs" / "enterprise-events.jsonl"
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH", str(absolute_path))

    settings = EnterpriseObservabilitySettings.from_env(project_root=tmp_path / "project")

    assert settings.events_log_path == absolute_path
