"""Current control-plane closeout with live supervisor identity, not release.

The supervisor heartbeat has no per-target termination receipt. Do not invent
one or require a new kernel proof. Compose existing reads and retain that
distinction. A trusted outer watchdog must bound all reads and filesystem work.
"""

import hashlib
from dataclasses import asdict, dataclass

from .m3_supervisor_identity import verify_identity
from .m3_target_tracking import track_pending
from .models import Code, canonical, require


@dataclass(frozen=True)
class CloseoutEvidence:
    sandbox_id: str
    state: str
    observed_mono: float
    supervisor_pid: int
    supervisor_start_ticks: int
    create_binding_sha256: str = ""
    supervisor_target_confirmation: bool = False
    fence_release_allowed: bool = False


def collect_closeout(reader, fence, binding, directory, supervisor, identity_pins, *, empty_manifest=False, registered_ids=()) -> CloseoutEvidence:
    """Require exact tracked target plus current, unchanged supervisor identity.

    UNKNOWN with no discovered target, foreign/multiple instances, alarms and
    running targets cannot produce closeout evidence. Terminal/disappearance
    remain observations: no guest kill, manifest mutation or fence release.
    """
    expected = {"run_id": binding.run_id, "template_id": binding.template_id}
    require(type(empty_manifest) is bool, Code.DENIED)
    # Discover/verify the exact target before asking for its manifest binding.
    target = track_pending(reader, fence, binding, directory)
    require(target.sandbox_id is not None and target.state in {
        "known_target_absent", "exact_terminal_observed",
    }, Code.DENIED)
    # Successor admission requires the supervisor's current manifest empty.
    # In that mode only durable known-target disappearance qualifies; an empty
    # manifest is never substituted for platform/identity tracking evidence.
    if empty_manifest:
        require(target.state == "known_target_absent", Code.DENIED)
        if registered_ids:
            expected["registered_ids"] = registered_ids
            read_supervisor = supervisor.read_registered
        else:
            read_supervisor = supervisor.read_empty
    else:
        expected["sandbox_id"] = target.sandbox_id
        read_supervisor = supervisor.read
    first = read_supervisor(**expected)
    first_identity = verify_identity(first, identity_pins)
    current = track_pending(reader, fence, binding, directory)
    require(current.sandbox_id == target.sandbox_id and current.state in {
        "known_target_absent", "exact_terminal_observed",
    }, Code.DENIED)
    if empty_manifest:
        require(current.state == "known_target_absent", Code.DENIED)
    final = read_supervisor(**expected)
    final_identity = verify_identity(final, identity_pins)
    require((first_identity.pid, first_identity.start_ticks) ==
            (final_identity.pid, final_identity.start_ticks), Code.DENIED)
    require(first.boot_id == final.boot_id == binding.boot_id, Code.DENIED)
    fence.verify_pending(binding)
    now = supervisor._sample().monotonic
    for observed in (first.observed_mono, current.observed_mono, final.observed_mono):
        require(0 <= now - observed <= 2, Code.DENIED)
    return CloseoutEvidence(target.sandbox_id, current.state, min(first.observed_mono, current.observed_mono),
                            final_identity.pid, final_identity.start_ticks,
                            hashlib.sha256(canonical(asdict(binding)).encode()).hexdigest())
