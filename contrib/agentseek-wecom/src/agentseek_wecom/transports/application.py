from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Literal
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from agentseek_wecom.addressing import ConversationAddress, app_conversation_address
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.crypto import WeComCryptoError, WeComJsonCrypto
from agentseek_wecom.transport import InboundMessageHandler

_TOKEN_ERROR_CODES = {40014, 42001}
_AUTHORIZATION_ERROR_CODES = {60011, 60020, 60111, 81016}
_MESSAGE_TYPES = {
    "text",
    "image",
    "voice",
    "video",
    "file",
    "textcard",
    "news",
    "mpnews",
    "markdown",
    "template_card",
}


class WeComAppError(RuntimeError):
    """Base error for the self-built application transport."""


class WeComAppApiError(WeComAppError):
    def __init__(self, operation: str, errcode: int) -> None:
        self.operation = operation
        self.errcode = errcode
        super().__init__(f"WeCom application {operation} failed with errcode={errcode}")


class WeComAppPartialDelivery(WeComAppError):
    """The API accepted only part of a multi-recipient request."""


class WeComAppVisibilityDenied(WeComAppError):
    """A target is outside the resolved application visibility boundary."""


@dataclass(frozen=True, slots=True)
class WeComAppTarget:
    users: tuple[str, ...] = ()
    parties: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized = tuple(_normalize_ids(values) for values in (self.users, self.parties, self.tags))
        object.__setattr__(self, "users", normalized[0])
        object.__setattr__(self, "parties", normalized[1])
        object.__setattr__(self, "tags", normalized[2])
        if not any(normalized):
            raise ValueError("at least one application target is required")
        if "@all" in self.users:
            raise ValueError("@all application delivery is not supported")
        if len(self.users) > 1000 or len(self.parties) > 100 or len(self.tags) > 100:
            raise ValueError("application target exceeds the official recipient limit")
        if any(not value.isdigit() or int(value) <= 0 for value in (*self.parties, *self.tags)):
            raise ValueError("application department and tag IDs must be positive integers")

    def payload(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.users:
            result["touser"] = "|".join(self.users)
        if self.parties:
            result["toparty"] = "|".join(self.parties)
        if self.tags:
            result["totag"] = "|".join(self.tags)
        return result

    def stable_scope(self) -> str:
        return "\x1e".join(("|".join(self.users), "|".join(self.parties), "|".join(self.tags)))


@dataclass(frozen=True, slots=True)
class WeComAppVisibility:
    users: frozenset[str]
    parties: frozenset[str]
    tags: frozenset[str]
    expires_at: float
    users_authoritative: bool = True
    parties_authoritative: bool = True
    tags_authoritative: bool = True


@dataclass(frozen=True, slots=True)
class WeComAppDeliveryReceipt:
    invalid_user_count: int
    invalid_party_count: int
    invalid_tag_count: int


class WeComAppTransport:
    """Supplementary self-built application callback and proactive sender."""

    def __init__(
        self,
        *,
        settings: WeComSettings,
        tenant_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.tenant_id = tenant_id.strip() or "default"
        self.agent_id = settings.app_agent_id.strip()
        self._client = client or httpx.AsyncClient(timeout=settings.api_timeout_seconds)
        self._owns_client = client is None
        self._inbound_handler: InboundMessageHandler | None = None
        self._token_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._visibility: WeComAppVisibility | None = None
        self._crypto: WeComJsonCrypto | None = None

    @property
    def kind(self) -> Literal["wecom_app"]:
        return "wecom_app"

    @property
    def allowed_digital_employee_ids(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.settings.app_allowed_digital_employee_ids.split(",") if item.strip())

    def bind_inbound(self, handler: InboundMessageHandler) -> None:
        self._inbound_handler = handler

    def address_for(self, data: dict[str, Any], *, plaintext_userid: str | None = None) -> ConversationAddress:
        del plaintext_userid
        return app_conversation_address(
            data,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
        )

    def mount(self, app: FastAPI) -> None:
        path = self.settings.app_callback_path

        @app.get("/health/wecom-app")
        async def health() -> dict[str, Any]:
            ready = self._visibility is not None and self._visibility.expires_at > time.monotonic()
            return {
                "ok": ready,
                "channel": "wecom",
                "transport": "application",
                "enabled": self.settings.app_transport_enabled,
                "visibility_loaded": ready,
            }

        @app.get(path)
        async def verify_url(msg_signature: str, timestamp: str, nonce: str, echostr: str) -> Response:
            try:
                plain_echo = self._require_crypto().verify_url(
                    msg_signature=msg_signature,
                    timestamp=timestamp,
                    nonce=nonce,
                    echostr=echostr,
                )
            except WeComCryptoError as exc:
                raise HTTPException(status_code=400, detail="application callback verification failed") from exc
            return Response(content=plain_echo, media_type="text/plain")

        @app.post(path)
        async def handle_message(
            request: Request,
            msg_signature: str,
            timestamp: str,
            nonce: str,
        ) -> Response:
            body = await request.body()
            try:
                outer = _xml_fields(body)
                plain = self._require_crypto().decrypt_encrypted_value(
                    encrypt=outer["Encrypt"],
                    msg_signature=msg_signature,
                    timestamp=timestamp,
                    nonce=nonce,
                )
                data = _normalize_application_callback(plain, expected_agent_id=self.agent_id)
            except (KeyError, ValueError, WeComCryptoError) as exc:
                raise HTTPException(status_code=400, detail="application callback decrypt failed") from exc
            handler = self._inbound_handler
            if handler is None:
                raise RuntimeError("application transport inbound handler is not bound")
            await handler(data)
            return Response(content="success", media_type="text/plain")

    async def start(self) -> None:
        if self.settings.app_transport_enabled:
            await self.refresh_visibility()

    async def stop(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def refresh_visibility(self) -> WeComAppVisibility:
        data = await self._api_json("GET", "/cgi-bin/agent/get", params={"agentid": self.agent_id})
        if str(data.get("agentid") or "") != self.agent_id or int(data.get("close") or 0) != 0:
            raise WeComAppVisibilityDenied("application identity mismatch or application disabled")
        party_roots = frozenset(str(item) for item in ((data.get("allow_partys") or {}).get("partyid") or []))
        parties = await self._expand_visible_parties(party_roots) if party_roots else frozenset()
        visibility = WeComAppVisibility(
            users=frozenset(
                str(item.get("userid"))
                for item in ((data.get("allow_userinfos") or {}).get("user") or [])
                if isinstance(item, dict) and item.get("userid")
            ),
            parties=parties,
            tags=frozenset(str(item) for item in ((data.get("allow_tags") or {}).get("tagid") or [])),
            expires_at=time.monotonic() + self.settings.app_visibility_cache_ttl_seconds,
            # Some self-built applications return the allow_* containers but
            # leave every list empty even though message/send accepts targets
            # from the configured visible scope. An empty list is therefore an
            # unknown snapshot, not proof that the application can see nobody.
            users_authoritative=bool((data.get("allow_userinfos") or {}).get("user") or []),
            parties_authoritative=bool(party_roots),
            tags_authoritative=bool((data.get("allow_tags") or {}).get("tagid") or []),
        )
        self._visibility = visibility
        return visibility

    async def _expand_visible_parties(self, roots: frozenset[str]) -> frozenset[str]:
        expanded: set[str] = set()
        for root in roots:
            if not root.isdigit() or int(root) <= 0:
                raise WeComAppVisibilityDenied("agent/get returned an invalid visible department ID")
            data = await self._api_json("GET", "/cgi-bin/department/simplelist", params={"id": root})
            try:
                expanded.update(_validated_department_subtree(data.get("department_id"), root=root))
            except (TypeError, ValueError):
                raise WeComAppVisibilityDenied("WeCom returned an incomplete visible department subtree") from None
        return frozenset(expanded)

    async def send(
        self,
        *,
        target: WeComAppTarget,
        message_type: str,
        payload: dict[str, Any],
    ) -> WeComAppDeliveryReceipt:
        self.validate_outbound(message_type, payload)
        visibility = self._visibility
        if visibility is None or visibility.expires_at <= time.monotonic():
            visibility = await self.refresh_visibility()
        _assert_visible(target, visibility)
        body: dict[str, Any] = {
            **target.payload(),
            "msgtype": message_type,
            "agentid": int(self.agent_id),
            message_type: payload,
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
        }
        data = await self._api_json("POST", "/cgi-bin/message/send", json=body)
        receipt = WeComAppDeliveryReceipt(
            invalid_user_count=_pipe_count(data.get("invaliduser")),
            invalid_party_count=_pipe_count(data.get("invalidparty")),
            invalid_tag_count=_pipe_count(data.get("invalidtag")),
        )
        if any((receipt.invalid_user_count, receipt.invalid_party_count, receipt.invalid_tag_count)):
            raise WeComAppPartialDelivery("application message was accepted for only part of the target")
        return receipt

    def validate_outbound(self, message_type: str, payload: dict[str, Any]) -> None:
        if message_type not in _MESSAGE_TYPES:
            raise ValueError(f"unsupported application message type: {message_type}")
        if not payload:
            raise ValueError("application message payload must not be empty")

    async def upload_media(
        self,
        *,
        media_type: Literal["image", "voice", "video", "file"],
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        limits = {"image": 10, "voice": 2, "video": 10, "file": 20}
        safe_filename = filename.strip().replace("\r", "").replace("\n", "")
        if not safe_filename or "/" in safe_filename or "\\" in safe_filename:
            raise ValueError("application media filename must be a plain filename")
        if len(content) <= 5 or len(content) > limits[media_type] * 1024 * 1024:
            raise ValueError("application media size is outside the official limit")
        for attempt in range(2):
            token = await self._get_access_token()
            try:
                response = await self._client.post(
                    f"{self.settings.api_base_url.rstrip('/')}/cgi-bin/media/upload",
                    params={"access_token": token, "type": media_type},
                    files={"media": (safe_filename, content, content_type)},
                )
                status_code = response.status_code
                data = response.json()
            except (httpx.HTTPError, ValueError):
                token = ""
                content = b""
                raise WeComAppApiError("media/upload", -1) from None
            token = ""
            if status_code < 200 or status_code >= 300:
                content = b""
                raise WeComAppApiError("media/upload", status_code)
            if not isinstance(data, dict):
                content = b""
                raise WeComAppApiError("media/upload", -1)
            errcode = int(data.get("errcode") or 0)
            if errcode in _TOKEN_ERROR_CODES and attempt == 0:
                self._access_token = None
                self._access_token_expires_at = 0.0
                continue
            if errcode != 0:
                content = b""
                raise WeComAppApiError("media/upload", errcode)
            media_id = str(data.get("media_id") or "")
            if not media_id:
                content = b""
                raise WeComAppApiError("media/upload", -1)
            return media_id
        raise WeComAppApiError("media/upload", 40014)

    def validate_source(self, digital_employee_id: str) -> str:
        value = digital_employee_id.strip()
        if not value or value not in self.allowed_digital_employee_ids:
            raise WeComAppVisibilityDenied("digital employee is not allowed to use this application")
        return value

    async def _api_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(2):
            token = await self._get_access_token()
            query = dict(params or {})
            query["access_token"] = token
            try:
                response = await self._client.request(
                    method,
                    f"{self.settings.api_base_url.rstrip('/')}{path}",
                    params=query,
                    json=json,
                )
                status_code = response.status_code
                data = response.json()
            except (httpx.HTTPError, ValueError):
                token = ""
                query.clear()
                raise WeComAppApiError(path, -1) from None
            token = ""
            query.clear()
            if status_code < 200 or status_code >= 300:
                raise WeComAppApiError(path, status_code)
            if not isinstance(data, dict):
                raise WeComAppApiError(path, -1)
            errcode = int(data.get("errcode") or 0)
            if errcode in _TOKEN_ERROR_CODES and attempt == 0:
                self._access_token = None
                self._access_token_expires_at = 0.0
                continue
            if errcode != 0:
                if errcode in _AUTHORIZATION_ERROR_CODES:
                    raise WeComAppVisibilityDenied("WeCom rejected the application target or visibility scope")
                raise WeComAppApiError(path, errcode)
            return data
        raise WeComAppApiError(path, 40014)

    async def _get_access_token(self) -> str:
        now = time.monotonic()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token
        async with self._token_lock:
            now = time.monotonic()
            if self._access_token and now < self._access_token_expires_at:
                return self._access_token
            try:
                response = await self._client.get(
                    f"{self.settings.api_base_url.rstrip('/')}/cgi-bin/gettoken",
                    params={
                        "corpid": self.settings.corp_id,
                        "corpsecret": self.settings.app_transport_secret.get_secret_value(),
                    },
                )
                status_code = response.status_code
                data = response.json()
            except (httpx.HTTPError, ValueError):
                raise WeComAppApiError("gettoken", -1) from None
            if status_code < 200 or status_code >= 300:
                raise WeComAppApiError("gettoken", status_code)
            if not isinstance(data, dict) or int(data.get("errcode") or 0) != 0:
                raise WeComAppApiError("gettoken", int(data.get("errcode") or -1))
            token = str(data.get("access_token") or "")
            if not token:
                raise WeComAppApiError("gettoken", -1)
            expires_in = max(60, int(data.get("expires_in") or 7200) - 120)
            self._access_token = token
            self._access_token_expires_at = now + expires_in
            return token

    def _require_crypto(self) -> WeComJsonCrypto:
        if self._crypto is None:
            self._crypto = WeComJsonCrypto(
                token=self.settings.app_callback_token.get_secret_value(),
                encoding_aes_key=self.settings.app_callback_encoding_aes_key.get_secret_value(),
                receive_id=self.settings.corp_id,
            )
        return self._crypto


def _normalize_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if not value or "|" in value or len(value) > 128:
            raise ValueError("application target IDs must be non-empty and must not contain '|'")
        if value not in result:
            result.append(value)
    return tuple(result)


def _assert_visible(target: WeComAppTarget, visibility: WeComAppVisibility) -> None:
    if visibility.users_authoritative and not set(target.users).issubset(visibility.users):
        raise WeComAppVisibilityDenied("one or more users are outside explicit application visibility")
    if visibility.parties_authoritative and not set(target.parties).issubset(visibility.parties):
        raise WeComAppVisibilityDenied("one or more departments are outside application visibility")
    if visibility.tags_authoritative and not set(target.tags).issubset(visibility.tags):
        raise WeComAppVisibilityDenied("one or more tags are outside application visibility")


def _validated_department_subtree(value: Any, *, root: str) -> frozenset[str]:
    if not isinstance(value, list):
        raise TypeError("department subtree must be a list")
    parents: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("department subtree item must be an object")
        department_id = str(item.get("id") or "").strip()
        parent_id = str(item.get("parentid") if item.get("parentid") is not None else "").strip()
        if not department_id.isdigit() or int(department_id) <= 0:
            raise ValueError("department ID must be a positive integer")
        if not parent_id.isdigit() or int(parent_id) < 0:
            raise ValueError("department parent ID must be a non-negative integer")
        previous_parent = parents.setdefault(department_id, parent_id)
        if previous_parent != parent_id:
            raise ValueError("department has conflicting parents")
    if root not in parents:
        raise ValueError("visible department root is missing")

    descendants = {root}
    pending = set(parents) - descendants
    while pending:
        resolved = {department_id for department_id in pending if parents[department_id] in descendants}
        if not resolved:
            raise ValueError("department subtree contains an orphan or cycle")
        descendants.update(resolved)
        pending.difference_update(resolved)
    return frozenset(descendants)


def _pipe_count(value: Any) -> int:
    return len([item for item in str(value or "").split("|") if item])


def _xml_fields(value: bytes | str) -> dict[str, str]:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    if len(raw) > 1024 * 1024:
        raise ValueError("unsafe application callback XML")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("unsafe application callback XML")
    root = ElementTree.fromstring(raw)  # noqa: S314 - bounded input rejects DTD and entity declarations
    return {child.tag: child.text or "" for child in root}


def _normalize_application_callback(value: str, *, expected_agent_id: str) -> dict[str, Any]:
    fields = _xml_fields(value)
    agent_id = fields.get("AgentID", "")
    if agent_id and agent_id != expected_agent_id:
        raise ValueError("application callback AgentID mismatch")
    msgtype = fields.get("MsgType", "").strip().lower()
    from_userid = fields.get("FromUserName", "").strip()
    if not msgtype or not from_userid:
        raise ValueError("application callback missing MsgType or FromUserName")
    message_id = fields.get("MsgId", "").strip()
    if not message_id:
        stable_event = "\x1f".join(
            (from_userid, fields.get("CreateTime", ""), msgtype, fields.get("Event", ""), fields.get("EventKey", ""))
        )
        message_id = f"app-event-{hashlib.sha256(stable_event.encode()).hexdigest()}"
    data: dict[str, Any] = {
        "msgid": message_id,
        "msgtype": msgtype,
        "agentid": expected_agent_id,
        "chattype": "single",
        "from": {"userid": from_userid},
        "_agentseek_wecom_app": True,
    }
    if msgtype == "text":
        data["text"] = {"content": fields.get("Content", "")}
    elif msgtype in {"image", "voice", "video", "file"}:
        data[msgtype] = {"media_id": fields.get("MediaId", "")}
    elif msgtype == "event":
        data["event"] = {
            "eventtype": fields.get("Event", "").lower(),
            "event_key": fields.get("EventKey", ""),
        }
    return data
