"""Read-only successor eligibility, not quota reservation or fence release.

Only a previous sealed successful create can follow this path. UNKNOWN without
a receipt remains blocked even when its target disappeared. Installed sequence
and independent next-create approval are caller trust anchors, never job fields.
Real create still requires W0, live admission, quota reservation and fencing.
"""

import hashlib
import time
from dataclasses import asdict, dataclass, replace

from .m3_closeout_evidence import CloseoutEvidence
from .m3_create_worker import read_create_approval
from .models import Code, canonical, require


@dataclass(frozen=True)
class SuccessorEligibility:
    next_create_token: str
    approval_deadline: float
    remaining_planned_slots: int
    create_allowed: bool = False
    fence_release_allowed: bool = False


def check_successor(previous, vault, next_plan, approval_path, *, approval_digest, sequence, collect_closeout):
    """Sequence is a trusted frozen tuple of full CreatePlans, not a count claim.

    Basic sequence is restricted A/B; optional full sequence adds public C/D.
    This verifies adjacency only: it does not claim a durable total-send counter.
    Caller must bound receipt, approval and collector I/O with an outer watchdog.
    """
    require(type(sequence) is tuple and len(sequence) in {2, 4}, Code.DENIED)
    for index, plan in enumerate(sequence):
        plan.binding(approval_digest)
        require(plan.restricted is (index < 2), Code.DENIED)
        require((plan.run_id, plan.boot_id, plan.candidate_sha256, plan.template_id, plan.endpoint, plan.domain) ==
                (next_plan.run_id, next_plan.boot_id, next_plan.candidate_sha256, next_plan.template_id, next_plan.endpoint, next_plan.domain), Code.DENIED)
    require(len({plan.create_token for plan in sequence}) == len(sequence), Code.DENIED)
    require(next_plan in sequence, Code.DENIED)
    index = sequence.index(next_plan)
    require(index > 0, Code.DENIED)
    expected = sequence[index - 1].binding(previous.approval_sha256)
    require(replace(previous, intent_sha256="0" * 64) == expected, Code.DENIED)
    receipt = vault.read(previous)
    vault.read_intent(previous)
    deadline = read_create_approval(next_plan, approval_path, pinned_digest=approval_digest)
    closed = collect_closeout()
    require(type(closed) is CloseoutEvidence, Code.DENIED)
    require(closed.create_binding_sha256 == hashlib.sha256(canonical(asdict(expected)).encode()).hexdigest(), Code.DENIED)
    require(closed.sandbox_id == receipt.sandbox_id and closed.state in {"known_target_absent", "exact_terminal_observed"}, Code.DENIED)
    require(vault.read(previous) == receipt, Code.DENIED)
    deadline = min(deadline, read_create_approval(next_plan, approval_path, pinned_digest=approval_digest))
    now = time.monotonic()
    require(0 <= now - closed.observed_mono <= 2 and deadline - now >= 11, Code.DENIED)
    return SuccessorEligibility(next_plan.create_token, deadline, len(sequence) - index)
