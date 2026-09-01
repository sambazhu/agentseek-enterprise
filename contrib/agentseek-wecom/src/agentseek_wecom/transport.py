from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastapi import FastAPI

from agentseek_wecom.addressing import ConversationAddress, WeComTransportKind

InboundMessageHandler = Callable[[dict[str, Any]], Awaitable[str | None]]


class ActiveStreamTransport(Protocol):
    """Optional contract for transports that actively push stream refreshes."""

    async def deliver_stream(
        self,
        *,
        request_id: str,
        stream_id: str,
        content: str,
        finish: bool,
    ) -> None: ...


class WeComTransport(Protocol):
    """Lifecycle and addressing boundary implemented by every WeCom transport."""

    app: FastAPI | None

    @property
    def kind(self) -> WeComTransportKind: ...

    def bind_inbound(self, handler: InboundMessageHandler) -> None: ...

    def address_for(
        self,
        data: dict[str, Any],
        *,
        plaintext_userid: str | None = None,
    ) -> ConversationAddress: ...

    async def start(self, stop_event: asyncio.Event) -> None: ...

    async def stop(self) -> None: ...
