from __future__ import annotations

from typing import Literal

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
    transport_mode: Literal["callback"] = Field(
        default="callback",
        validation_alias=AliasChoices(
            "BUB_WECOM_TRANSPORT_MODE",
            "AGENTSEEK_WECOM_TRANSPORT_MODE",
        ),
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
        default=0.5,
        validation_alias=AliasChoices("BUB_WECOM_INITIAL_WAIT_SECONDS", "AGENTSEEK_WECOM_INITIAL_WAIT_SECONDS"),
    )
    cache_ttl_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices("BUB_WECOM_CACHE_TTL_SECONDS", "AGENTSEEK_WECOM_CACHE_TTL_SECONDS"),
    )
    turn_timeout_seconds: float = Field(
        default=195.0,
        validation_alias=AliasChoices(
            "BUB_WECOM_TURN_TIMEOUT_SECONDS",
            "AGENTSEEK_WECOM_TURN_TIMEOUT_SECONDS",
        ),
    )
    session_queue_maxsize: int = Field(
        default=3,
        validation_alias=AliasChoices(
            "BUB_WECOM_SESSION_QUEUE_MAXSIZE",
            "AGENTSEEK_WECOM_SESSION_QUEUE_MAXSIZE",
        ),
    )
    queue_wait_timeout_seconds: float = Field(
        default=240.0,
        validation_alias=AliasChoices(
            "BUB_WECOM_QUEUE_WAIT_TIMEOUT_SECONDS",
            "AGENTSEEK_WECOM_QUEUE_WAIT_TIMEOUT_SECONDS",
        ),
    )
    shutdown_timeout_seconds: float = Field(
        default=10.0,
        validation_alias=AliasChoices(
            "BUB_WECOM_SHUTDOWN_TIMEOUT_SECONDS",
            "AGENTSEEK_WECOM_SHUTDOWN_TIMEOUT_SECONDS",
        ),
    )
    corp_id: str = Field(
        default="",
        validation_alias=AliasChoices("BUB_WECOM_CORP_ID", "AGENTSEEK_WECOM_CORP_ID"),
    )
    app_secret: str = Field(
        default="",
        validation_alias=AliasChoices("BUB_WECOM_APP_SECRET", "AGENTSEEK_WECOM_APP_SECRET"),
    )
    api_base_url: str = Field(
        default="https://qyapi.weixin.qq.com",
        validation_alias=AliasChoices("BUB_WECOM_API_BASE_URL", "AGENTSEEK_WECOM_API_BASE_URL"),
    )
    userid_resolve_mode: str = Field(
        default="",
        validation_alias=AliasChoices("BUB_WECOM_USERID_RESOLVE_MODE", "AGENTSEEK_WECOM_USERID_RESOLVE_MODE"),
    )
    userid_cache_ttl_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices(
            "BUB_WECOM_USERID_CACHE_TTL_SECONDS",
            "AGENTSEEK_WECOM_USERID_CACHE_TTL_SECONDS",
        ),
    )
    api_timeout_seconds: float = Field(
        default=10.0,
        validation_alias=AliasChoices("BUB_WECOM_API_TIMEOUT_SECONDS", "AGENTSEEK_WECOM_API_TIMEOUT_SECONDS"),
    )
    response_url_probe_trigger: str = Field(
        default="",
        validation_alias=AliasChoices(
            "BUB_WECOM_RESPONSE_URL_PROBE_TRIGGER",
            "AGENTSEEK_WECOM_RESPONSE_URL_PROBE_TRIGGER",
        ),
    )
    response_url_template_card_probe_trigger: str = Field(
        default="",
        validation_alias=AliasChoices(
            "BUB_WECOM_RESPONSE_URL_TEMPLATE_CARD_PROBE_TRIGGER",
            "AGENTSEEK_WECOM_RESPONSE_URL_TEMPLATE_CARD_PROBE_TRIGGER",
        ),
    )
    response_url_probe_delay_seconds: float = Field(
        default=5.0,
        validation_alias=AliasChoices(
            "BUB_WECOM_RESPONSE_URL_PROBE_DELAY_SECONDS",
            "AGENTSEEK_WECOM_RESPONSE_URL_PROBE_DELAY_SECONDS",
        ),
    )
    welcome_text: str = Field(
        default="你好，我是你的企业数字员工。",
        validation_alias=AliasChoices("BUB_WECOM_WELCOME_TEXT", "AGENTSEEK_WECOM_WELCOME_TEXT"),
    )


def load_settings() -> WeComSettings:
    return WeComSettings()
