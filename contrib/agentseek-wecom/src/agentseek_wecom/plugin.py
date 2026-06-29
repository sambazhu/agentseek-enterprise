from __future__ import annotations

from typing import Any

from bub import hookimpl
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


def main(framework: Any) -> WeComPlugin:
    return WeComPlugin(framework)
