"""Concrete WeCom transport adapters."""

from agentseek_wecom.transports.callback import AiBotCallbackTransport

__all__ = ["AiBotCallbackTransport"]
from agentseek_wecom.transports.long_connection import AiBotLongConnectionTransport

__all__ = ["AiBotCallbackTransport", "AiBotLongConnectionTransport"]
