from __future__ import annotations

from agentseek_langchain.config import LangChainSettings


def test_model_start_timeout_has_dedicated_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTSEEK_MODEL_REQUEST_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("AGENTSEEK_LANGCHAIN_MODEL_START_TIMEOUT_SECONDS", "12")

    settings = LangChainSettings()

    assert settings.MODEL_START_TIMEOUT_SECONDS == 12


def test_model_start_timeout_falls_back_to_provider_request_timeout(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTSEEK_LANGCHAIN_MODEL_START_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("BUB_LANGCHAIN_MODEL_START_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("AGENTSEEK_MODEL_REQUEST_TIMEOUT_SECONDS", "45")

    settings = LangChainSettings()

    assert settings.MODEL_START_TIMEOUT_SECONDS == 45
