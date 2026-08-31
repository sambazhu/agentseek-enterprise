from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from agentseek_work import DeliveryRecord, DeliveryStatus

from enterprise_wecom_digital_employee.channel_command import authenticated_user_command_text

_DELIVERY_REQUEST_RE = re.compile(
    r"^\s*交付\s+report\s*artifact\s*[vV](\d+)\s*给我\s*[。.!！]?\s*$",
    re.IGNORECASE,
)
REPORT_DELIVERY_CARD_ACTION_KIND = "enterprise.report_delivery.commit.v1"


def explicitly_requests_report_delivery(message: str, *, expected_version: int) -> bool:
    """Accept only an exact self-delivery action for one Artifact version."""

    version = match_report_delivery_version(message)
    return version is not None and expected_version > 0 and version == expected_version


def match_report_delivery_version(message: str) -> int | None:
    """Return the exact requested self-delivery Artifact version, if any."""

    match = _DELIVERY_REQUEST_RE.fullmatch(authenticated_user_command_text(message))
    return int(match.group(1)) if match is not None else None


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


def delivery_record_action_payload(record: DeliveryRecord) -> dict[str, Any]:
    return {
        "delivery_id": record.delivery_id,
        "delivery_version": record.delivery_version,
        "work_id": record.work_id,
        "tenant_id": record.tenant_id,
        "artifact_id": record.artifact_id,
        "publication_id": record.publication_id,
        "content_sha256": record.content_sha256,
        "size_bytes": record.size_bytes,
        "recipient_key": record.recipient_key,
        "grant_hash": record.grant_hash,
        "grant_expires_at": record.grant_expires_at.isoformat(),
        "status": record.status.value,
        "delivered_by": record.delivered_by,
        "delivered_at": record.delivered_at.isoformat(),
        "grant_consumed_at": record.grant_consumed_at.isoformat() if record.grant_consumed_at else None,
        "metadata": dict(record.metadata),
    }


def delivery_record_from_action_payload(payload: Mapping[str, Any]) -> DeliveryRecord:
    consumed = payload.get("grant_consumed_at")
    return DeliveryRecord(
        delivery_id=str(payload["delivery_id"]),
        delivery_version=int(payload["delivery_version"]),
        work_id=str(payload["work_id"]),
        tenant_id=str(payload["tenant_id"]),
        artifact_id=str(payload["artifact_id"]),
        publication_id=str(payload["publication_id"]),
        content_sha256=str(payload["content_sha256"]),
        size_bytes=int(payload["size_bytes"]),
        recipient_key=str(payload["recipient_key"]),
        grant_hash=str(payload["grant_hash"]),
        grant_expires_at=datetime.fromisoformat(str(payload["grant_expires_at"])),
        status=DeliveryStatus(str(payload["status"])),
        delivered_by=str(payload["delivered_by"]),
        delivered_at=datetime.fromisoformat(str(payload["delivered_at"])),
        grant_consumed_at=datetime.fromisoformat(str(consumed)) if consumed else None,
        metadata=dict(payload.get("metadata") or {}),
    )


def grant_is_active(record: DeliveryRecord, *, now: datetime) -> bool:
    return record.grant_consumed_at is None and now < record.grant_expires_at
