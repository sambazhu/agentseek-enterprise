from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentseek_wecom.outbound import require_outbound_message_type


@dataclass(frozen=True)
class WeComResponseUrlSender:
    """Consume one short-connection response_url without exposing it to model context."""

    api_base_url: str = "https://qyapi.weixin.qq.com"
    timeout_seconds: float = 10.0

    def send_markdown(self, response_url: str, content: str) -> None:
        require_outbound_message_type("callback", "markdown")
        self._send_payload(
            response_url,
            {"msgtype": "markdown", "markdown": {"content": content}},
        )

    def send_template_card(self, response_url: str, template_card: Mapping[str, Any]) -> None:
        require_outbound_message_type("callback", "template_card")
        card = dict(template_card)
        if not isinstance(card.get("card_type"), str) or not card["card_type"].strip():
            raise ValueError("template card requires a non-empty card_type")
        self._send_payload(
            response_url,
            {"msgtype": "template_card", "template_card": card},
        )

    def _send_payload(self, response_url: str, payload: Mapping[str, Any]) -> None:
        self._validate_response_url(response_url)
        request = urllib.request.Request(
            response_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        result = self._open_json(request)
        if result.get("errcode") != 0:
            raise RuntimeError(
                f"response_url send failed: errcode={result.get('errcode')} errmsg={result.get('errmsg')}"
            )

    def _validate_response_url(self, response_url: str) -> None:
        parsed = urllib.parse.urlparse(response_url)
        expected = urllib.parse.urlparse(self.api_base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.hostname != expected.hostname:
            raise ValueError("response_url must use HTTPS and the configured WeCom API host")
        if parsed.path != "/cgi-bin/aibot/response":
            raise ValueError("response_url path is not an AI Bot response endpoint")
        query = urllib.parse.parse_qs(parsed.query)
        if not query.get("response_code"):
            raise ValueError("response_url is missing response_code")

    def _open_json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"response_url send failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"response_url network error: {exc.reason}") from exc
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("response_url returned non-JSON response") from exc
        if not isinstance(result, dict):
            raise TypeError("response_url returned unexpected JSON response")
        return result
