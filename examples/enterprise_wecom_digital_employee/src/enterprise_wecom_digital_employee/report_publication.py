from __future__ import annotations

import re
from hashlib import sha256

_PUBLISH_REQUEST_RE = re.compile(
    r"^\s*发布\s+report\s*artifact\s*[vV](\d+)\s*[。.!！]?\s*$",
    re.IGNORECASE,
)


def explicitly_requests_report_publication(message: str, *, expected_version: int) -> bool:
    """Accept only one exact publication action bound to one Artifact version."""

    match = _PUBLISH_REQUEST_RE.fullmatch(str(message or ""))
    return bool(match and expected_version > 0 and int(match.group(1)) == expected_version)


def publication_id(
    *,
    tenant_id: str,
    work_id: str,
    artifact_id: str,
    content_sha256: str,
) -> str:
    material = "\0".join((tenant_id, work_id, artifact_id, content_sha256)).encode()
    return f"publication_{sha256(material).hexdigest()}"
