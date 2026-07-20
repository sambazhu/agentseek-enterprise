from __future__ import annotations

import re

_WECOM_CHANNEL_RE = re.compile(r"(?:^|\|)channel=\$?wecom(?:\||$)", re.IGNORECASE)
_CHANNEL_DATE_LINE_RE = re.compile(r"(?m)^---Date:[^\r\n]*---[ \t]*\r?$")


def authenticated_user_command_text(message: str) -> str:
    """Strip only the runtime's authenticated WeCom command envelope."""

    text = str(message or "")
    matches = tuple(_CHANNEL_DATE_LINE_RE.finditer(text))
    if not matches:
        return text
    marker = matches[-1]
    if not _WECOM_CHANNEL_RE.search(text[:marker.start()]):
        return text
    return text[marker.end():].lstrip("\r\n")
