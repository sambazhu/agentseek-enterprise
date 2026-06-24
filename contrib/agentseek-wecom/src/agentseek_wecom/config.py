from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WeComSettings(BaseSettings):
    """Runtime settings for the WeCom callback channel."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
        populate_by_name=True,
    )

    enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("BUB_WECOM_ENABLED", "AGENTSEEK_WECOM_ENABLED"),
    )
    host: str = Field(
        default="0.0.0.0",
        validation_alias=AliasChoices("BUB_WECOM_HOST", "AGENTSEEK_WECOM_HOST"),
    )
    port: int = Field(
        default=12000,
        validation_alias=AliasChoices("BUB_WECOM_PORT", "AGENTSEEK_WECOM_PORT"),
    )
    callback_path: str = Field(
        default="/ai-bot/callback/demo/{botid}",
        validation_alias=AliasChoices("BUB_WECOM_CALLBACK_PATH", "AGENTSEEK_WECOM_CALLBACK_PATH"),
    )
    token: str = Field(
        default="",
        validation_alias=AliasChoices("BUB_WECOM_TOKEN", "AGENTSEEK_WECOM_TOKEN", "Token"),
    )
    encoding_aes_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "BUB_WECOM_ENCODING_AES_KEY",
            "AGENTSEEK_WECOM_ENCODING_AES_KEY",
            "EncodingAESKey",
        ),
    )
    receive_id: str = Field(
        default="",
        validation_alias=AliasChoices("BUB_WECOM_RECEIVE_ID", "AGENTSEEK_WECOM_RECEIVE_ID"),
    )
    initial_wait_seconds: float = Field(
        default=1.5,
        validation_alias=AliasChoices("BUB_WECOM_INITIAL_WAIT_SECONDS", "AGENTSEEK_WECOM_INITIAL_WAIT_SECONDS"),
    )
    cache_ttl_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices("BUB_WECOM_CACHE_TTL_SECONDS", "AGENTSEEK_WECOM_CACHE_TTL_SECONDS"),
    )
    welcome_text: str = Field(
        default="你好，我是你的企业数字员工。",
        validation_alias=AliasChoices("BUB_WECOM_WELCOME_TEXT", "AGENTSEEK_WECOM_WELCOME_TEXT"),
    )


def load_settings() -> WeComSettings:
    return WeComSettings()
