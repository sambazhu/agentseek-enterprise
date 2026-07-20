from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from agentseek_work import DeliveryRecord

from enterprise_wecom_digital_employee.channel_command import authenticated_user_command_text

_DELIVERY_REQUEST_RE = re.compile(
    r"^\s*交付\s+report\s*artifact\s*[vV](\d+)\s*给我\s*[。.!！]?\s*$",
    re.IGNORECASE,
)


def explicitly_requests_report_delivery(message: str, *, expected_version: int) -> bool:
    """Accept only an exact self-delivery action for one Artifact version."""

    match = _DELIVERY_REQUEST_RE.fullmatch(authenticated_user_command_text(message))
    return bool(match and expected_version > 0 and int(match.group(1)) == expected_version)


def new_grant_token() -> str:
    return secrets.token_urlsafe(32)


def grant_digest(grant_token: str) -> str:
    return f"sha256:{sha256(grant_token.encode('ascii')).hexdigest()}"


def delivery_id(
    *,
    tenant_id: str,
    work_id: str,
    artifact_id: str,
    recipient_key: str,
    grant_hash: str,
) -> str:
    material = "\0".join((tenant_id, work_id, artifact_id, recipient_key, grant_hash)).encode()
    return f"delivery_{sha256(material).hexdigest()}"


@dataclass(frozen=True, slots=True)
class PreparedReportDelivery:
    record: DeliveryRecord
    grant_token: str
    download_url: str
    filename: str
    already_delivered: bool = False


def grant_is_active(record: DeliveryRecord, *, now: datetime) -> bool:
    return record.grant_consumed_at is None and now < record.grant_expires_at
