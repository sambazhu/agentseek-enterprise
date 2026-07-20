from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal
from urllib.parse import urlparse

TransportMode = Literal["callback", "long_connection"]


class UnsupportedWeComOutbound(RuntimeError):
    """Raised when a requested outbound action is unavailable on the transport."""


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
    return value
