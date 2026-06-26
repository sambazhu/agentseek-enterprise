"""Settings for the {{ cookiecutter.project_name }} runtime binding."""

from __future__ import annotations

import os
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain.chat_models import init_chat_model
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parents[1]


class ProjectSettings(BaseSettings):
    """Settings consumed by the local DeepAgents binding and MCP adapter."""

    model_config = SettingsConfigDict(
        env_file=(Path(".env"), PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    model: str = Field(
        default="",
        validation_alias=AliasChoices("AGENTSEEK_MODEL", "BUB_MODEL", "DEEPAGENTS_MODEL"),
    )
    model_provider: str = Field(
        default="",
        validation_alias=AliasChoices("AGENTSEEK_MODEL_PROVIDER", "DEEPAGENTS_MODEL_PROVIDER"),
    )
    api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AGENTSEEK_API_KEY", "BUB_API_KEY"),
    )
    api_base: str = Field(
        default="",
        validation_alias=AliasChoices("AGENTSEEK_API_BASE", "BUB_API_BASE"),
    )
    openai_api_key: str = Field(default="", validation_alias=AliasChoices("OPENAI_API_KEY"))
    openai_api_base: str = Field(default="", validation_alias=AliasChoices("OPENAI_API_BASE"))
    openai_base_url: str = Field(default="", validation_alias=AliasChoices("OPENAI_BASE_URL"))
    openai_stream_chunk_timeout_s: float = Field(
        default=300.0,
        validation_alias=AliasChoices("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S"),
    )
    mcp_config_path: str = Field(
        default="{{ cookiecutter.mcp_config_path }}",
        validation_alias=AliasChoices("AGENTSEEK_MCP_CONFIG_PATH", "BUB_MCP_CONFIG_PATH"),
    )

    def require_model(self) -> str:
        model = self.model.strip()
        if model:
            return model
        msg = "Set AGENTSEEK_MODEL for the DeepAgents binding."
        raise RuntimeError(msg)

    def build_model(self) -> Any:
        """Build the LangChain chat model for DeepAgents."""

        raw_model = self.require_model()
        prefixed_provider, bare_model = _split_prefixed_model(raw_model)
        provider = _clean(self.model_provider) or prefixed_provider
        if not provider:
            self.apply_openai_env_bridge()
            return raw_model

        normalized_provider = provider.replace("-", "_").lower()
        if normalized_provider != "openai":
            raise ValueError(
                "This template currently supports AGENTSEEK_MODEL_PROVIDER=openai for OpenAI-compatible endpoints."
            )

        kwargs: dict[str, Any] = {"model_provider": "openai"}
        api_key = _clean(self.openai_api_key) or _clean(self.api_key)
        if api_key:
            kwargs["api_key"] = api_key
        base_url = _clean(self.openai_api_base) or _clean(self.openai_base_url) or _clean(self.api_base)
        if base_url:
            kwargs["base_url"] = base_url
        timeout = self.openai_stream_chunk_timeout_s
        kwargs["stream_chunk_timeout"] = None if timeout <= 0 else timeout
        return init_chat_model(model=bare_model, **kwargs)

    def resolved_mcp_config_path(self) -> Path:
        path = Path(self.mcp_config_path.strip() or "{{ cookiecutter.mcp_config_path }}")
        if path.is_absolute():
            return path
        return PROJECT_ROOT / path

    def apply_openai_env_bridge(self) -> None:
        model = self.model.strip()
        if not model.lower().startswith("openai:"):
            return

        api_key = self.api_key.strip()
        if api_key and not self.openai_api_key.strip():
            os.environ["OPENAI_API_KEY"] = api_key

        api_base = self.api_base.strip()
        if api_base and not self.openai_api_base.strip() and not self.openai_base_url.strip():
            os.environ["OPENAI_API_BASE"] = api_base


@lru_cache(maxsize=1)
def get_settings() -> ProjectSettings:
    return ProjectSettings()


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _split_prefixed_model(model_name: str) -> tuple[str | None, str]:
    if ":" not in model_name:
        return None, model_name
    provider_candidate, bare_model = model_name.split(":", maxsplit=1)
    provider = provider_candidate.strip()
    if provider in {"openai"}:
        return provider, bare_model
    warnings.warn(f"Ignoring unsupported model provider prefix {provider_candidate!r}.", stacklevel=2)
    return None, model_name
