from __future__ import annotations

from typing import Any

import enterprise_wecom_digital_employee.settings as settings_module


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
        api_key="test-key",
        api_base="https://example.invalid/v1",
        openai_request_timeout_s=12.5,
        openai_max_retries=1,
    )

    settings.build_model()

    assert captured == {
        "model": "qwen-flash",
        "model_provider": "openai",
        "api_key": "test-key",
        "base_url": "https://example.invalid/v1",
        "stream_chunk_timeout": 300.0,
        "timeout": 12.5,
        "max_retries": 1,
    }
