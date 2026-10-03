"""R1 local request stop time, not a platform hard-termination proof.

The send intent must have been persisted BEFORE create by trusted composition.
This reader never generates/backdates an intent or extends a lease. Hash binding
proves correspondence, not the historical truth of a fabricated intent.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .m3_create_receipt import CreateBinding, CreateReceiptVault
from .m3_probe_dispatch import Binding
from .m3_probe_process import _decode, config_bytes
from .m3_supervisor_snapshot import ClockSample
from .models import Code, ContractError, require


def platform_epoch(value: object) -> float:
    """Explicit timezone, nanosecond strings supported on Python 3.10 as well."""
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        require(type(value) in {float, int}, Code.DENIED)
        require(math.isfinite(value) and value > 0, Code.DENIED)
        return float(value)
    if not isinstance(value, str):
        raise ContractError(Code.DENIED)
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?([Zz]|[+-]\d{2}:?\d{2})", value)
    if match is None:
        raise ContractError(Code.DENIED)
    date, clock, fraction, zone = match.groups()
    zone = "+00:00" if zone.lower() == "z" else zone
    if len(zone) == 5:
        zone = zone[:3] + ":" + zone[3:]
    fraction = (fraction or "").ljust(6, "0")[:6]
    parsed = datetime.fromisoformat(f"{date}T{clock}.{fraction}{zone}")
    return parsed.astimezone(timezone.utc).timestamp()


@dataclass(frozen=True)
class LifetimeSnapshot:
    stop_mono: float
    remaining_seconds: float
    platform_hard_termination_proven: bool = False


def read_lifetime(
    path: Path, *, pinned_digest: str, binding: Binding, now: ClockSample, started_epoch: float, end_epoch: float | None
) -> LifetimeSnapshot:
    binding.validate()
    require(type(pinned_digest) is str and re.fullmatch(r"[0-9a-f]{64}", pinned_digest) is not None)
    raw = config_bytes(path)
    require(hashlib.sha256(raw).hexdigest() == pinned_digest, Code.DENIED)
    intent = _decode(raw)
    return _evaluate_intent(intent, binding=binding, now=now, started_epoch=started_epoch, end_epoch=end_epoch)


def read_sealed_lifetime(
    vault: CreateReceiptVault,
    *,
    create: CreateBinding,
    binding: Binding,
    now: ClockSample,
    started_epoch: float,
    end_epoch: float | None,
) -> LifetimeSnapshot:
    """Local stop time from the creating worker's sealed send intent and receipt.

    A reserved slot with no receipt remains UNKNOWN, never test authority.
    Neither this function nor the AEAD store proves remote hard termination.
    """
    binding.validate()
    for key in ("run_id", "create_token", "boot_id", "template_id", "candidate_sha256"):
        require(getattr(binding, key) == getattr(create, key), Code.DENIED)
    receipt = vault.read(create)
    require(receipt.sandbox_id == binding.sandbox_id, Code.DENIED)
    return _evaluate_intent(
        vault.read_intent(create), binding=binding, now=now, started_epoch=started_epoch, end_epoch=end_epoch
    )


def _evaluate_intent(
    intent: dict, *, binding: Binding, now: ClockSample, started_epoch: float, end_epoch: float | None
) -> LifetimeSnapshot:
    require(set(intent) == {"schema", "run_id", "create_token", "boot_id", "sent_epoch", "sent_mono"})
    require(type(intent["schema"]) is int and intent["schema"] == 1)
    require(
        intent["run_id"] == binding.run_id
        and intent["create_token"] == binding.create_token
        and intent["boot_id"] == binding.boot_id == now.boot_id,
        Code.DENIED,
    )
    for value in (now.epoch, now.monotonic, now.uptime, intent["sent_epoch"], intent["sent_mono"], started_epoch):
        require(type(value) in {int, float} and math.isfinite(value) and value >= 0, Code.DENIED)
    age = now.monotonic - intent["sent_mono"]
    require(0 <= age <= now.uptime and abs((now.epoch - intent["sent_epoch"]) - age) <= 0.5, Code.DENIED)
    # Reject impossible/future platform timestamps. Earlier platform start only
    # shortens the budget; it never repairs a missing/invalid intent.
    require(started_epoch <= now.epoch, Code.DENIED)
    stop = min(intent["sent_mono"] + 120, now.monotonic + started_epoch + 120 - now.epoch)
    if end_epoch is not None:
        require(
            type(end_epoch) in {int, float} and math.isfinite(end_epoch) and end_epoch >= started_epoch, Code.DENIED
        )
        stop = min(stop, now.monotonic + end_epoch - now.epoch)
    require(stop - now.monotonic >= 11, Code.DENIED)
    return LifetimeSnapshot(stop, stop - now.monotonic)
