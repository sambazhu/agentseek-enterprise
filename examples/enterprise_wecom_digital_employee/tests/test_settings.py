from __future__ import annotations

from io import StringIO
from typing import Any

import enterprise_wecom_digital_employee.settings as settings_module
from pydantic import SecretStr
from rich.console import Console
from rich.traceback import Traceback


def test_openai_compatible_model_has_bounded_request_timeout(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_init_chat_model(*, model: str, **kwargs: Any) -> object:
        captured.update(model=model, **kwargs)
        return object()

    monkeypatch.setattr(settings_module, "init_chat_model", fake_init_chat_model)
    settings = settings_module.ProjectSettings(
        _env_file=None,  # ty: ignore[unknown-argument]
        model="openai:qwen-flash",
        model_provider="openai",
        api_key=SecretStr("test-key"),
        api_base="https://example.invalid/v1",
        openai_request_timeout_s=12.5,
        openai_max_retries=1,
    )

    settings.build_model()

    api_key = captured.pop("api_key")
    assert isinstance(api_key, SecretStr)
    assert api_key.get_secret_value() == "test-key"
    assert captured == {
        "model": "qwen-flash",
        "model_provider": "openai",
        "base_url": "https://example.invalid/v1",
        "stream_chunk_timeout": 300.0,
        "timeout": 12.5,
        "max_retries": 1,
    }


def test_settings_secrets_are_redacted_from_rich_turn_traceback(monkeypatch) -> None:
    monkeypatch.setenv("AGENTSEEK_MODEL", "openai:test-model")
    monkeypatch.setenv("AGENTSEEK_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("AGENTSEEK_API_KEY", "sk-sensitive-test-value")
    monkeypatch.setenv(
        "AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL",
        "postgresql+psycopg://agentseek:sensitive-password@example.invalid/agentseek",
    )
    monkeypatch.setenv(
        "AGENTSEEK_WORK_SQLALCHEMY_URL",
        "postgresql+psycopg://agentseek:sensitive-password@example.invalid/agentseek",
    )
    settings = settings_module.ProjectSettings(
        _env_file=None,  # ty: ignore[unknown-argument]
    )

    def fail_turn() -> None:
        current_settings = settings
        assert current_settings.model
        raise RuntimeError("synthetic turn failure")

    try:
        fail_turn()
    except RuntimeError as exc:
        rendered = StringIO()
        console = Console(file=rendered, force_terminal=False, width=160)
        console.print(
            Traceback.from_exception(
                type(exc),
                exc,
                exc.__traceback__,
                show_locals=True,
            )
        )
    else:  # pragma: no cover - defensive assertion around the synthetic failure.
        raise AssertionError("synthetic turn failure was not raised")

    traceback_output = rendered.getvalue()
    assert "sk-sensitive-test-value" not in traceback_output
    assert "sk-sensitive" not in traceback_output
    assert "postgresql+psycopg://agentseek:sensitive-password@example.invalid/agentseek" not in traceback_output
    assert "sensitive-password" not in traceback_output
