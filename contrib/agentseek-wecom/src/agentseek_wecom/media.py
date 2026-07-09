from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

from agentseek_wecom.config import WeComSettings


@dataclass
class MediaDownload:
    media_id: str
    data: bytes
    filename: str
    mime_type: str


@dataclass
class _AccessToken:
    value: str
    expires_at: float


@dataclass
class WeComMediaClient:
    """Download temporary media from the self-built WeCom app API."""

    corp_id: str
    app_secret: str
    api_base_url: str = "https://qyapi.weixin.qq.com"
    timeout_seconds: float = 10.0
    _access_token: _AccessToken | None = field(default=None, init=False)

    @classmethod
    def from_settings(cls, settings: WeComSettings) -> WeComMediaClient | None:
        if not settings.corp_id or not settings.app_secret:
            return None
        return cls(
            corp_id=settings.corp_id,
            app_secret=settings.app_secret,
            api_base_url=settings.api_base_url,
            timeout_seconds=settings.api_timeout_seconds,
        )

    @property
    def _base_url(self) -> str:
        base_url = self.api_base_url.strip().rstrip("/")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("WeCom API base URL must be http(s)")
        return base_url

    def download(self, media_id: str, *, fallback_filename: str, fallback_mime_type: str) -> MediaDownload:
        media_id = str(media_id or "").strip()
        if not media_id:
            raise ValueError("media_id is required")
        token = self._get_access_token()
        query = urllib.parse.urlencode({"access_token": token, "media_id": media_id})
        request = urllib.request.Request(  # noqa: S310 - URL is validated to http(s) in _base_url.
            f"{self._base_url}/cgi-bin/media/get?{query}",
            headers={"Accept": "*/*"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                data = response.read()
                headers = response.headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"media/get failed with HTTP {exc.code}: {body[:200]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"media/get network error: {exc}") from exc

        content_type = headers.get_content_type() or fallback_mime_type
        if content_type == "application/json":
            error = _json_error(data)
            if error is not None:
                raise RuntimeError(
                    f"media/get failed: errcode={error.get('errcode')} errmsg={error.get('errmsg')}"
                )

        filename = _filename_from_headers(headers) or fallback_filename
        mime_type = content_type or fallback_mime_type or _guess_mime_type(filename)
        return MediaDownload(media_id=media_id, data=data, filename=filename, mime_type=mime_type)

    def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token is not None and now < self._access_token.expires_at:
            return self._access_token.value

        query = urllib.parse.urlencode({"corpid": self.corp_id, "corpsecret": self.app_secret})
        request = urllib.request.Request(  # noqa: S310 - URL is validated to http(s) in _base_url.
            f"{self._base_url}/cgi-bin/gettoken?{query}",
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"gettoken failed with HTTP {exc.code}: {body[:200]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"gettoken network error: {exc}") from exc

        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("errcode") != 0:
            raise RuntimeError(f"gettoken failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError("gettoken response missing access_token")
        expires_in = int(data.get("expires_in") or 7200)
        self._access_token = _AccessToken(token, now + max(expires_in - 120, 60))
        return token


def _filename_from_headers(headers: Message) -> str | None:
    disposition = headers.get("Content-Disposition") or ""
    if not disposition:
        return None
    message = Message()
    message["Content-Disposition"] = disposition
    params = dict(message.get_params(header="Content-Disposition") or [])
    value = params.get("filename") or params.get("filename*")
    return str(value).strip() if value else None


def _guess_mime_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _json_error(data: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict) and "errcode" in parsed:
        return parsed
    return None
