from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlparse

TransportMode = Literal["callback", "long_connection"]


class UnsupportedWeComOutbound(RuntimeError):
    """Raised when a requested outbound action is unavailable on the transport."""


class ArtifactDownloadError(RuntimeError):
    """Base class for fail-closed Artifact download responses."""


class ArtifactDownloadNotFound(ArtifactDownloadError):
    """Raised without revealing which grant component was invalid."""


class ArtifactDownloadGone(ArtifactDownloadError):
    """Raised for an expired or already-consumed one-time grant."""


@dataclass(frozen=True)
class ArtifactDownload:
    data: bytes
    filename: str
    media_type: str


@dataclass(frozen=True)
class TemplateCardIntent:
    template_card: Mapping[str, Any]
    on_succeeded: Callable[[], None]
    on_failed: Callable[[str], None]
    expires_at_monotonic: float


_INTENT_MARKER_RE = re.compile(r"\[\[agentseek-wecom-template-card:([A-Za-z0-9_-]{32,128})\]\]")
_TEMPLATE_CARD_CONTROL_PHRASES = (
    "这是受信的 WeCom 模板卡片交付指令",
    "请原样返回上一行标记并立即停止",
    "不得复述、展示或猜测下载链接",
)
_INTENT_LOCK = threading.RLock()
_INTENTS: dict[str, TemplateCardIntent] = {}
_DOWNLOAD_RESOLVER: Callable[[str, str], ArtifactDownload] | None = None


@dataclass(frozen=True)
class WeComOutboundCapabilities:
    transport_mode: TransportMode
    implemented: bool
    reply_message_types: tuple[str, ...]
    proactive_message_types: tuple[str, ...]
    response_url_one_shot: bool
    response_url_ttl_seconds: int | None
    direct_file_delivery: bool
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_CAPABILITIES: dict[TransportMode, WeComOutboundCapabilities] = {
    "callback": WeComOutboundCapabilities(
        transport_mode="callback",
        implemented=True,
        reply_message_types=("markdown", "template_card"),
        proactive_message_types=(),
        response_url_one_shot=True,
        response_url_ttl_seconds=3600,
        direct_file_delivery=False,
        notes=(
            "AI Bot response_url does not accept file messages.",
            "Use an HTTPS template-card download link until long-connection delivery is implemented.",
        ),
    ),
    "long_connection": WeComOutboundCapabilities(
        transport_mode="long_connection",
        implemented=False,
        reply_message_types=(
            "stream",
            "template_card",
            "markdown",
            "file",
            "voice",
            "image",
            "video",
        ),
        proactive_message_types=("stream", "template_card", "markdown", "file", "voice", "image", "video"),
        response_url_one_shot=False,
        response_url_ttl_seconds=None,
        direct_file_delivery=True,
        notes=(
            "Official AI Bot long connection supports media upload and file messages.",
            "The AgentSeek WeCom plugin does not implement this transport yet.",
            "WeCom allows either long connection or callback mode for one AI Bot, not both.",
        ),
    ),
}


def outbound_capabilities(transport_mode: TransportMode) -> WeComOutboundCapabilities:
    return _CAPABILITIES[transport_mode]


def require_outbound_message_type(transport_mode: TransportMode, message_type: str) -> None:
    capabilities = outbound_capabilities(transport_mode)
    if not capabilities.implemented:
        raise UnsupportedWeComOutbound(f"WeCom transport {transport_mode!r} is not implemented")
    if message_type not in capabilities.reply_message_types:
        raise UnsupportedWeComOutbound(
            f"WeCom transport {transport_mode!r} does not support outbound message type {message_type!r}"
        )


def validate_artifact_download_base_url(url: str) -> str:
    value = url.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("artifact download base URL must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("artifact download base URL must not contain credentials, query, or fragment")
    decoded_path = unquote(parsed.path)
    if (
        "\\" in decoded_path
        or "//" in decoded_path
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        or not re.fullmatch(r"/[A-Za-z0-9._~/-]*|", decoded_path)
    ):
        raise ValueError("artifact download base URL contains an unsafe path")
    return value


def register_template_card_intent(intent: TemplateCardIntent) -> str:
    intent_id = secrets.token_urlsafe(32)
    with _INTENT_LOCK:
        _drop_expired_intents()
        _INTENTS[intent_id] = intent
    return f"[[agentseek-wecom-template-card:{intent_id}]]"


def take_template_card_intent(content: str) -> TemplateCardIntent | None:
    match = _INTENT_MARKER_RE.search(content)
    if match is None:
        return None
    with _INTENT_LOCK:
        intent = _INTENTS.pop(match.group(1), None)
    if intent is None or intent.expires_at_monotonic <= time.monotonic():
        return None
    return intent


def has_template_card_intent_marker(content: str) -> bool:
    return _INTENT_MARKER_RE.search(content) is not None


def has_template_card_control_instruction(content: str) -> bool:
    """Detect internal card-control prose that must never reach an employee."""

    return any(phrase in content for phrase in _TEMPLATE_CARD_CONTROL_PHRASES)


def register_artifact_download_resolver(
    resolver: Callable[[str, str], ArtifactDownload],
) -> None:
    global _DOWNLOAD_RESOLVER
    _DOWNLOAD_RESOLVER = resolver


def resolve_artifact_download(delivery_id: str, grant_token: str) -> ArtifactDownload:
    resolver = _DOWNLOAD_RESOLVER
    if resolver is None:
        raise ArtifactDownloadNotFound("Artifact download is unavailable")
    return resolver(delivery_id, grant_token)


def _drop_expired_intents() -> None:
    now = time.monotonic()
    for intent_id, intent in tuple(_INTENTS.items()):
        if intent.expires_at_monotonic <= now:
            _INTENTS.pop(intent_id, None)
