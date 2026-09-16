"""Live create-time collector for trusted single-attempt composition.

This callback reads schema-3 private installation/approval/W0 and live template,
empty platform list, supervisor identity and alarms on EVERY call. No cached
report, readiness flag, user-supplied JSON evidence or deadline import is used.
The worker's mandatory installation fence still blocks every subsequent attempt.
The pinned m3_create_process composes it internally; this is not a batch quota,
UNKNOWN release protocol or approved installed operator entry.
Invoke only inside the outer creating-process watchdog, including filesystem I/O.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from .m3_create_worker import CreatePlan
from .m3_precreate_process import _digest, collect_evidence
from .models import Code, require


class LiveCreateAdmission:
    def __init__(self, path: Path, *, installation_digest: str, approval_digest: str, plan: CreatePlan, registered_ids=()):
        require(path.is_absolute() and path.resolve() == path, Code.DENIED)
        _digest(installation_digest)
        _digest(approval_digest)
        plan.binding(approval_digest)
        self._path = path
        self._installation_digest = installation_digest
        self._approval_digest = approval_digest
        self._plan = plan
        self._registered_ids = registered_ids

    def __call__(self) -> float:
        evidence = collect_evidence(
            {
                "path": str(self._path),
                "installation_digest": self._installation_digest,
                "approval_digest": self._approval_digest,
            },
            expected_plan=self._plan,
            **({"registered_ids": self._registered_ids} if self._registered_ids else {}),
        )
        require(evidence.schema == 3, Code.DENIED)
        require(
            evidence.plan == self._plan
            and evidence.installation_digest == self._installation_digest
            and evidence.approval_digest == self._approval_digest,
            Code.DENIED,
        )
        now = time.monotonic()
        require(evidence.observed_mono <= now < evidence.valid_until_mono, Code.DENIED)
        deadline = evidence.approval_deadline_mono
        require(math.isfinite(deadline) and deadline - now >= 11, Code.DENIED)
        # Observation freshness is NOT an execution lease: it is checked now.
        # Return the independent min(approval, W0) bound; the creating worker and
        # its watchdog shorten it further and re-collect after reserving intent.
        return deadline
