from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from loguru import logger

from agentseek_wecom.config import WeComSettings


class UseridResolver(Protocol):
    """Resolve a WeCom intelligent robot open userid into a plaintext userid."""

    def resolve(self, open_userid: str) -> str | None: ...


@dataclass
class _AccessToken:
    value: str
    expires_at: float


@dataclass
class WeComOpenUseridResolver:
    """Use the self-built app API to convert robot encrypted userid values."""

    corp_id: str
    app_secret: str
    api_base_url: str = "https://qyapi.weixin.qq.com"
    cache_ttl_seconds: int = 3600
    timeout_seconds: float = 10.0
    _access_token: _AccessToken | None = field(default=None, init=False)
    _userid_cache: dict[str, tuple[str, float]] = field(default_factory=dict, init=False)

    def resolve(self, open_userid: str) -> str | None:
        open_userid = open_userid.strip()
        if not open_userid:
            return None

        cached = self._get_cached_userid(open_userid)
        if cached:
            return cached

        access_token = self._get_access_token()
        userid = self._convert_open_userid(access_token, open_userid)
        if userid:
            self._userid_cache[open_userid] = (userid, time.time() + self.cache_ttl_seconds)
        return userid

    @property
    def _base_url(self) -> str:
        return self.api_base_url.strip().rstrip("/")

    def _get_cached_userid(self, open_userid: str) -> str | None:
        cached = self._userid_cache.get(open_userid)
        if cached is None:
            return None
        userid, expires_at = cached
        if time.time() >= expires_at:
            self._userid_cache.pop(open_userid, None)
            return None
        return userid

    def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token is not None and now < self._access_token.expires_at:
            return self._access_token.value

        query = urllib.parse.urlencode({"corpid": self.corp_id, "corpsecret": self.app_secret})
        data = self._get_json(f"{self._base_url}/cgi-bin/gettoken?{query}", label="gettoken")
        if data.get("errcode") != 0:
            raise RuntimeError(f"gettoken failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}")

        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError("gettoken response missing access_token")

        expires_in = int(data.get("expires_in") or 7200)
        self._access_token = _AccessToken(token, now + max(expires_in - 120, 60))
        return token

    def _convert_open_userid(self, access_token: str, open_userid: str) -> str | None:
        query = urllib.parse.urlencode({"access_token": access_token})
        data = self._post_json(
            f"{self._base_url}/cgi-bin/batch/openuserid_to_userid?{query}",
            {"open_userid_list": [open_userid]},
            label="openuserid_to_userid",
        )
        if data.get("errcode") != 0:
            raise RuntimeError(
                f"openuserid_to_userid failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
            )

        invalid = data.get("invalid_open_userid_list") or []
        if open_userid in invalid:
            logger.info("wecom.userid_resolve invalid_open_userid={}", open_userid)
            return None

        for item in data.get("userid_list") or []:
            if isinstance(item, dict) and item.get("open_userid") == open_userid and item.get("userid"):
                return str(item["userid"])
        return None

    def _get_json(self, url: str, *, label: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        return self._open_json(request, label=label)

    def _post_json(self, url: str, payload: dict[str, Any], *, label: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        return self._open_json(request, label=label)

    def _open_json(self, request: urllib.request.Request, *, label: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{label} failed with HTTP {exc.code}: {body[:200]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{label} network error: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{label} returned non-JSON response") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"{label} returned unexpected JSON response")
        return data


def make_userid_resolver(settings: WeComSettings) -> UseridResolver | None:
    mode = settings.userid_resolve_mode.strip().lower()
    if mode in {"", "none", "disabled", "false", "off"}:
        return None
    if mode != "openuserid_to_userid":
        raise ValueError(f"Unsupported WeCom userid resolve mode: {settings.userid_resolve_mode!r}")
    if not settings.corp_id or not settings.app_secret:
        logger.warning("wecom.userid_resolve disabled: AGENTSEEK_WECOM_CORP_ID and APP_SECRET are required")
        return None
    return WeComOpenUseridResolver(
        corp_id=settings.corp_id,
        app_secret=settings.app_secret,
        api_base_url=settings.api_base_url,
        cache_ttl_seconds=settings.userid_cache_ttl_seconds,
        timeout_seconds=settings.api_timeout_seconds,
    )
