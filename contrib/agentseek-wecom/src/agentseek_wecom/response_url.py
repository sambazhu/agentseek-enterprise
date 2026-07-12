from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WeComResponseUrlSender:
    """Consume one short-connection response_url without exposing it to model context."""

    api_base_url: str = "https://qyapi.weixin.qq.com"
    timeout_seconds: float = 10.0

    def send_markdown(self, response_url: str, content: str) -> None:
        self._validate_response_url(response_url)
        request = urllib.request.Request(
            response_url,
            data=json.dumps(
                {"msgtype": "markdown", "markdown": {"content": content}},
                ensure_ascii=False,
            ).encode("utf-8"),
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
