from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LangChainSettings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_prefix="BUB_LANGCHAIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # Uppercase field name so the env var is `BUB_LANGCHAIN_SPEC` (not `BUB_LANGCHAIN_spec`).
    SPEC: str = ""
    RUN_TIMEOUT_SECONDS: float = Field(
        default=180.0,
        validation_alias=AliasChoices(
            "BUB_LANGCHAIN_RUN_TIMEOUT_SECONDS",
            "AGENTSEEK_LANGCHAIN_RUN_TIMEOUT_SECONDS",
        ),
    )
    MODEL_START_TIMEOUT_SECONDS: float = Field(
        default=60.0,
        validation_alias=AliasChoices(
            "BUB_LANGCHAIN_MODEL_START_TIMEOUT_SECONDS",
            "AGENTSEEK_LANGCHAIN_MODEL_START_TIMEOUT_SECONDS",
            "AGENTSEEK_MODEL_REQUEST_TIMEOUT_SECONDS",
        ),
    )


@lru_cache(maxsize=1)
def get_langchain_settings() -> LangChainSettings:
    return LangChainSettings()
