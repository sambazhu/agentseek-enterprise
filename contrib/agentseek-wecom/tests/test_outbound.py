from __future__ import annotations

from typing import Any, cast

import pytest
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.outbound import (
    UnsupportedWeComOutbound,
    has_template_card_control_instruction,
    outbound_capabilities,
    require_outbound_message_type,
    validate_artifact_download_base_url,
)
from pydantic import ValidationError


def test_callback_capabilities_fail_closed_for_files() -> None:
    capabilities = outbound_capabilities("callback")

    assert capabilities.implemented is True
    assert capabilities.reply_message_types == ("markdown", "template_card")
    assert capabilities.response_url_one_shot is True
    assert capabilities.response_url_ttl_seconds == 3600
    assert capabilities.direct_file_delivery is False
    with pytest.raises(UnsupportedWeComOutbound, match="does not support"):
        require_outbound_message_type("callback", "file")


def test_long_connection_capabilities_are_documented_but_not_implemented() -> None:
    capabilities = outbound_capabilities("long_connection")

    assert capabilities.implemented is False
    assert capabilities.direct_file_delivery is True
    assert "file" in capabilities.reply_message_types
    with pytest.raises(UnsupportedWeComOutbound, match="not implemented"):
        require_outbound_message_type("long_connection", "file")


def test_internal_template_card_instruction_is_detected_without_marker() -> None:
    assert has_template_card_control_instruction(
        "这是受信的 WeCom 模板卡片交付指令。请原样返回上一行标记并立即停止。"
    )
    assert not has_template_card_control_instruction("报告文件已准备好，请按卡片提示下载。")


def test_settings_reject_unimplemented_long_connection_transport() -> None:
    with pytest.raises(ValidationError, match="transport_mode"):
        WeComSettings(transport_mode=cast(Any, "long_connection"))


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
