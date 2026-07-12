from __future__ import annotations

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
