from __future__ import annotations

import re
from hashlib import sha256

_PUBLISH_REQUEST_RE = re.compile(
    r"^\s*发布\s+report\s*artifact\s*[vV](\d+)\s*[。.!！]?\s*$",
    re.IGNORECASE,
)
_WECOM_CHANNEL_RE = re.compile(r"(?:^|\|)channel=\$?wecom(?:\||$)", re.IGNORECASE)
_CHANNEL_DATE_LINE_RE = re.compile(r"(?m)^---Date:[^\r\n]*---[ \t]*\r?$")


def explicitly_requests_report_publication(message: str, *, expected_version: int) -> bool:
    """Accept only one exact publication action bound to one Artifact version."""

    match = _PUBLISH_REQUEST_RE.fullmatch(_publication_command_text(message))
    return bool(match and expected_version > 0 and int(match.group(1)) == expected_version)


def _publication_command_text(message: str) -> str:
    text = str(message or "")
    matches = tuple(_CHANNEL_DATE_LINE_RE.finditer(text))
    if not matches:
        return text
    marker = matches[-1]
    if not _WECOM_CHANNEL_RE.search(text[:marker.start()]):
        return text
    return text[marker.end():].lstrip("\r\n")


def publication_id(
    *,
    tenant_id: str,
    work_id: str,
    artifact_id: str,
    content_sha256: str,
) -> str:
    material = "\0".join((tenant_id, work_id, artifact_id, content_sha256)).encode()
    return f"publication_{sha256(material).hexdigest()}"
