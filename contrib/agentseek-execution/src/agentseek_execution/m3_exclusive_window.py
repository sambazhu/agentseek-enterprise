"""Pinned W0 coordination record, NOT technical exclusion or create authority.

No writer discovery, confirmation generation, service control or network calls.
Installation must independently approve the complete inventory and document
digest. Valid signatures/real human consent are outside this local hash reader.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path

from .m3_create_worker import CreatePlan
from .m3_probe_process import _decode, config_bytes
from .m3_supervisor_snapshot import ClockSample, _clock_valid
from .models import Code, ContractError, canonical, require


@dataclass(frozen=True)
class WindowSnapshot:
    window_id: str
    expires_mono: float
    document_sha256: str
    technical_exclusion_proven: bool = False
    aggregate_ready: bool = False


def read_window(
    path: Path,
    *,
    pinned_digest: str,
    plan: CreatePlan,
    creator_id: str,
    expected_writers: tuple[str, ...],
    now: ClockSample,
) -> WindowSnapshot:
    """Every installed writer must explicitly abstain; inventory is never inferred.

    Expiry only shortens the calling operation's budget, never extends guest TTL.
    A valid record does not show that another writer actually obeyed it.
    """
    try:
        require(os.getuid() == 0 and os.geteuid() == 0, Code.DENIED)
        plan.binding(pinned_digest)
        _clock_valid(now)
        require(now.boot_id == plan.boot_id, Code.DENIED)
        require(type(expected_writers) is tuple and 0 < len(expected_writers) <= 256)
        require(len(set(expected_writers)) == len(expected_writers))
        for identifier in (*expected_writers, creator_id):
            require(
                type(identifier) is str and 0 < len(identifier) <= 256 and all(33 <= ord(c) <= 126 for c in identifier)
            )
        require(creator_id not in expected_writers, Code.DENIED)
        raw = config_bytes(path)
        require(hashlib.sha256(raw).hexdigest() == pinned_digest, Code.DENIED)
        value = _decode(raw)
        require(
            set(value)
            == {
                "schema",
                "window_id",
                "run_id",
                "boot_id",
                "candidate_sha256",
                "creator_id",
                "inventory_sha256",
                "accepted",
                "revoked",
                "starts_epoch",
                "ends_epoch",
                "writers",
            }
        )
        require(
            type(value["schema"]) is int
            and value["schema"] == 1
            and value["accepted"] is True
            and value["revoked"] is False,
            Code.DENIED,
        )
        require(
            value["run_id"] == plan.run_id
            and value["boot_id"] == plan.boot_id
            and value["candidate_sha256"] == plan.candidate_sha256
            and value["creator_id"] == creator_id,
            Code.DENIED,
        )
        require(
            value["inventory_sha256"] == hashlib.sha256(canonical(sorted(expected_writers)).encode()).hexdigest(),
            Code.DENIED,
        )
        window_id = value["window_id"]
        require(type(window_id) is str and 0 < len(window_id) <= 256 and all(33 <= ord(c) <= 126 for c in window_id))
        start, end = value["starts_epoch"], value["ends_epoch"]
        for number in (start, end):
            require(type(number) in {int, float} and math.isfinite(number), Code.DENIED)
        require(0 <= start <= now.epoch and end - now.epoch >= 11, Code.DENIED)
        writers = value["writers"]
        require(type(writers) is list and len(writers) == len(expected_writers), Code.DENIED)
        seen = set()
        for writer in writers:
            require(type(writer) is dict and set(writer) == {"id", "responsible", "confirmed_epoch", "abstain"})
            identifier = writer["id"]
            require(type(identifier) is str and identifier in expected_writers and identifier not in seen, Code.DENIED)
            seen.add(identifier)
            require(writer["abstain"] is True, Code.DENIED)
            responsible = writer["responsible"]
            require(type(responsible) is str and 0 < len(responsible) <= 256 and responsible.strip() == responsible)
            confirmed = writer["confirmed_epoch"]
            require(
                type(confirmed) in {int, float} and math.isfinite(confirmed) and 0 <= confirmed <= start, Code.DENIED
            )
        return WindowSnapshot(window_id, now.monotonic + end - now.epoch, pinned_digest)
    except Exception:
        raise ContractError(Code.DENIED) from None
