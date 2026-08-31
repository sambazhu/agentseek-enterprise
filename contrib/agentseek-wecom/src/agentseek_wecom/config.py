from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WeComSettings(BaseSettings):
    """Runtime settings for the WeCom callback channel."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
        populate_by_name=True,
        hide_input_in_errors=True,
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
    durable_mode: Literal["memory", "sqlite"] = Field(
        default="memory",
        validation_alias=AliasChoices(
            "BUB_WECOM_DURABLE_MODE",
            "AGENTSEEK_WECOM_DURABLE_MODE",
        ),
    )
    durable_sqlite_path: str = Field(
        default="runtime/wecom-messages.sqlite3",
        validation_alias=AliasChoices(
            "BUB_WECOM_DURABLE_SQLITE_PATH",
            "AGENTSEEK_WECOM_DURABLE_SQLITE_PATH",
        ),
    )
    durable_secret: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        repr=False,
        validation_alias=AliasChoices(
            "BUB_WECOM_DURABLE_SECRET",
            "AGENTSEEK_WECOM_DURABLE_SECRET",
        ),
    )
    durable_recovery_limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        validation_alias=AliasChoices(
            "BUB_WECOM_DURABLE_RECOVERY_LIMIT",
            "AGENTSEEK_WECOM_DURABLE_RECOVERY_LIMIT",
        ),
    )
    durable_recovery_interval_seconds: float = Field(
        default=30.0,
        gt=0,
        le=3600,
        validation_alias=AliasChoices(
            "BUB_WECOM_DURABLE_RECOVERY_INTERVAL_SECONDS",
            "AGENTSEEK_WECOM_DURABLE_RECOVERY_INTERVAL_SECONDS",
        ),
    )
    durable_lease_seconds: float = Field(
        default=600.0,
        gt=0,
        le=3600,
        validation_alias=AliasChoices(
            "BUB_WECOM_DURABLE_LEASE_SECONDS",
            "AGENTSEEK_WECOM_DURABLE_LEASE_SECONDS",
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
    template_card_event_probe_trigger: str = Field(
        default="",
        validation_alias=AliasChoices(
            "BUB_WECOM_TEMPLATE_CARD_EVENT_PROBE_TRIGGER",
            "AGENTSEEK_WECOM_TEMPLATE_CARD_EVENT_PROBE_TRIGGER",
        ),
    )
    response_url_probe_delay_seconds: float = Field(
        default=5.0,
        validation_alias=AliasChoices(
            "BUB_WECOM_RESPONSE_URL_PROBE_DELAY_SECONDS",
            "AGENTSEEK_WECOM_RESPONSE_URL_PROBE_DELAY_SECONDS",
        ),
    )
    artifact_delivery_mode: Literal["disabled", "signed_link"] = Field(
        default="disabled",
        validation_alias=AliasChoices(
            "BUB_WORK_ARTIFACT_DELIVERY_MODE",
            "AGENTSEEK_WORK_ARTIFACT_DELIVERY_MODE",
        ),
    )
    artifact_public_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "BUB_WORK_ARTIFACT_PUBLIC_BASE_URL",
            "AGENTSEEK_WORK_ARTIFACT_PUBLIC_BASE_URL",
        ),
    )
    welcome_text: str = Field(
        default="你好，我是你的企业数字员工。",
        validation_alias=AliasChoices("BUB_WECOM_WELCOME_TEXT", "AGENTSEEK_WECOM_WELCOME_TEXT"),
    )

    @model_validator(mode="after")
    def validate_durable_settings(self) -> WeComSettings:
        if self.durable_mode != "sqlite":
            return self
        if not self.durable_sqlite_path.strip():
            raise ValueError("durable_sqlite_path is required when durable_mode='sqlite'")
        if len(self.durable_secret.get_secret_value()) < 32:
            raise ValueError("durable_secret must contain at least 32 characters when durable_mode='sqlite'")
        return self


def load_settings() -> WeComSettings:
    return WeComSettings()
