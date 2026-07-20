from __future__ import annotations

import json
import urllib.request

import pytest
from agentseek_wecom.response_url import WeComResponseUrlSender


def test_response_url_validation_accepts_official_endpoint() -> None:
    sender = WeComResponseUrlSender()

    sender._validate_response_url(
        "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=sensitive",
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=sensitive",
        "https://example.com/cgi-bin/aibot/response?response_code=sensitive",
        "https://qyapi.weixin.qq.com/cgi-bin/message/send?response_code=sensitive",
        "https://qyapi.weixin.qq.com/cgi-bin/aibot/response",
    ],
)
def test_response_url_validation_rejects_untrusted_urls(url: str) -> None:
    sender = WeComResponseUrlSender()

    with pytest.raises(ValueError):
        sender._validate_response_url(url)


def test_template_card_uses_official_response_url_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []

    def fake_open_json(self: WeComResponseUrlSender, request: urllib.request.Request) -> dict[str, object]:
        assert isinstance(request.data, bytes)
        captured.append(json.loads(request.data))
        return {"errcode": 0}

    monkeypatch.setattr(WeComResponseUrlSender, "_open_json", fake_open_json)
    sender = WeComResponseUrlSender()
    sender.send_template_card(
        "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=sensitive",
        {
            "card_type": "text_notice",
            "main_title": {"title": "报告已生成"},
            "card_action": {"type": 1, "url": "https://reports.example.test/download/signed"},
        },
    )

    assert captured == [
        {
            "msgtype": "template_card",
            "template_card": {
                "card_type": "text_notice",
                "main_title": {"title": "报告已生成"},
                "card_action": {"type": 1, "url": "https://reports.example.test/download/signed"},
            },
        }
    ]


def test_template_card_requires_card_type() -> None:
    sender = WeComResponseUrlSender()

    with pytest.raises(ValueError, match="card_type"):
        sender.send_template_card(
            "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=sensitive",
            {"main_title": {"title": "missing type"}},
        )
