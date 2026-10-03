from __future__ import annotations

from typing import Any

from bub import hookimpl
from bub.envelope import field_of
from bub.types import MessageHandler

from agentseek_wecom.channel import WeComChannel
from agentseek_wecom.config import load_settings


class WeComPlugin:
    def __init__(self, framework: Any) -> None:
        del framework
        self._channel = WeComChannel(on_receive=None, settings=load_settings())

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[WeComChannel]:
        self._channel.bind_receiver(message_handler)
        return [self._channel]

    @hookimpl
    def load_state(self, message, session_id):
        from agentseek_wecom.file_delivery import STATE_KEY

        del session_id
        context = field_of(message, "context", {})
        token = context.get(STATE_KEY) if isinstance(context, dict) else None
        # Always clear a previous turn's handle. The capability is process-local,
        # scope-bound and short-lived, not an identity supplied by tool arguments.
        return {STATE_KEY: token if isinstance(token, str) else ""}


def main(framework: Any) -> WeComPlugin:
    return WeComPlugin(framework)
