from __future__ import annotations

import pytest
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.outbound import (
    UnsupportedWeComOutbound,
    has_template_card_control_instruction,
    outbound_capabilities,
    require_outbound_message_type,
    validate_artifact_download_base_url,
)
from pydantic import SecretStr, ValidationError


def test_callback_capabilities_fail_closed_for_files() -> None:
    capabilities = outbound_capabilities("callback")

    assert capabilities.implemented is True
    assert capabilities.reply_message_types == ("markdown", "template_card")
    assert capabilities.response_url_one_shot is True
    assert capabilities.response_url_ttl_seconds == 3600
    assert capabilities.direct_file_delivery is False
    with pytest.raises(UnsupportedWeComOutbound, match="does not support"):
        require_outbound_message_type("callback", "file")


def test_long_connection_capabilities_are_implemented() -> None:
    capabilities = outbound_capabilities("long_connection")

    assert capabilities.implemented is True
    assert capabilities.direct_file_delivery is False
    assert "file" not in capabilities.reply_message_types
    assert capabilities.proactive_message_types == ("template_card", "markdown")
    with pytest.raises(UnsupportedWeComOutbound, match="does not support"):
        require_outbound_message_type("long_connection", "file")


def test_internal_template_card_instruction_is_detected_without_marker() -> None:
    assert has_template_card_control_instruction(
        "这是受信的 WeCom 模板卡片交付指令。请原样返回上一行标记并立即停止。"
    )
    assert not has_template_card_control_instruction("报告文件已准备好，请按卡片提示下载。")


def test_settings_require_long_connection_credentials_only_when_enabled() -> None:
    disabled = WeComSettings(transport_mode="long_connection")
    assert disabled.transport_mode == "long_connection"

    with pytest.raises(ValidationError, match="long_connection_bot_id"):
        WeComSettings(enabled=True, transport_mode="long_connection")

    enabled = WeComSettings(
        enabled=True,
        transport_mode="long_connection",
        long_connection_bot_id="bot-1",
        long_connection_secret=SecretStr("long-secret"),
    )
    assert enabled.long_connection_secret.get_secret_value() == "long-secret"


def test_callback_ignores_inactive_long_connection_endpoint() -> None:
    settings = WeComSettings(transport_mode="callback", long_connection_url="https://unused.invalid")

    assert settings.transport_mode == "callback"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://reports.example.test/download/", "https://reports.example.test/download"),
        (" https://reports.example.test ", "https://reports.example.test"),
    ],
)
def test_validate_artifact_download_base_url(value: str, expected: str) -> None:
    assert validate_artifact_download_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://reports.example.test",
        "https://user:secret@reports.example.test",
        "https://reports.example.test?token=secret",
        "https://reports.example.test/#fragment",
        "https://reports.example.test/../admin",
        "https://reports.example.test/%2e%2e/admin",
        "https://reports.example.test/{delivery_id}",
    ],
)
def test_validate_artifact_download_base_url_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_artifact_download_base_url(value)
