"""Bounded read-only pre-create evidence, never a create admission grant.

Only existing private installation/approval/supervisor files and fixed GETs are
read. No service start, manifest registration, intent reservation, create/kill
or dispatch. Schema 2 adds T1 API binding, not artifact-byte verification.
Schema 3 adds W0 coordination records, not technical exclusion. One independent
M3 node supervisor covers guests; the local worker watchdog covers calls, not
guest termination. This result is not a creating-worker admission deadline.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .m3_create_worker import CreatePlan, read_create_approval
from .m3_exclusive_window import read_window
from .m3_platform_evidence import PlatformReader
from .m3_probe_process import _decode, config_bytes
from .m3_supervisor_identity import IdentityPins, verify_identity
from .m3_supervisor_snapshot import SupervisorReader
from .m3_template_evidence import TemplatePins
from .models import Code, canonical, require
from .worker_process import run_worker

CHECKS = ["pinned_create_approval", "local_empty_manifest", "local_supervisor_identity", "platform_empty_list"]
MISSING = [
    "exclusive_window_confirmations",
    "template_artifact",
    "create_watchdog",
    "unknown_outcome_reconciliation",
    "unified_create_admission",
]
TEMPLATE_CHECKS = [*CHECKS, "template_api_binding"]
TEMPLATE_MISSING = [item for item in MISSING if item != "template_artifact"]
WINDOW_CHECKS = [*TEMPLATE_CHECKS, "pinned_window_confirmations"]
WINDOW_MISSING = [item for item in TEMPLATE_MISSING if item != "exclusive_window_confirmations"]
RESULTS = {1: (CHECKS, MISSING), 2: (TEMPLATE_CHECKS, TEMPLATE_MISSING), 3: (WINDOW_CHECKS, WINDOW_MISSING)}


def _digest(value: str) -> None:
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None, Code.DENIED)


def collect_isolated(path: Path, *, installation_digest: str, approval_digest: str) -> dict:
    """10s process budget, up to 1s cleanup; only references/pins cross stdin."""
    _digest(installation_digest)
    _digest(approval_digest)
    require(path.is_absolute() and path.resolve() == path, Code.DENIED)
    raw = run_worker(
        [sys.executable, "-I", "-m", "agentseek_execution.m3_precreate_process"],
        canonical({
            "path": str(path),
            "installation_digest": installation_digest,
            "approval_digest": approval_digest,
        }).encode(),
        budget=10,
        environment={},
    )
    result = _decode(raw)
    require(set(result) == {"schema", "aggregate_ready", "plan_digest", "checks", "missing"}, Code.UNKNOWN)
    require(
        type(result["schema"]) is int and result["schema"] in RESULTS and result["aggregate_ready"] is False,
        Code.UNKNOWN,
    )
    checks, missing = RESULTS[result["schema"]]
    require(result["checks"] == checks and result["missing"] == missing, Code.UNKNOWN)
    _digest(result["plan_digest"])
    return result


@dataclass(frozen=True)
class PrecreateEvidence:
    """Internal observation, not a grant or a CreateWorker admission deadline.

    Keep this in the trusted process. Do not serialize the plan (create token),
    and do not turn the report back into evidence. Freshness covers observations
    only; quota, UNKNOWN reconciliation and the watchdog are separate gates.
    """

    schema: int
    plan: CreatePlan = field(repr=False)
    installation_digest: str
    approval_digest: str
    observed_mono: float
    valid_until_mono: float
    approval_deadline_mono: float

    def report(self) -> dict:
        checks, missing = RESULTS[self.schema]
        return {
            "schema": self.schema,
            "aggregate_ready": False,
            "plan_digest": hashlib.sha256(canonical(asdict(self.plan)).encode()).hexdigest(),
            "checks": list(checks),
            "missing": list(missing),
        }


def collect_evidence(payload: dict, *, expected_plan: CreatePlan | None = None, registered_ids=()) -> PrecreateEvidence:
    """Live private-file/GET collection for a future trusted creating process.

    This function is blocking and must run inside that process's watchdog.
    Schema 1/2 evidence is intentionally incomplete, as the public report says.
    """
    require(os.getuid() == 0 and os.geteuid() == 0, Code.DENIED)
    require(type(payload) is dict and set(payload) == {"path", "installation_digest", "approval_digest"})
    _digest(payload["installation_digest"])
    _digest(payload["approval_digest"])
    require(type(payload["path"]) is str)
    path = Path(payload["path"])
    raw = config_bytes(path)
    require(hashlib.sha256(raw).hexdigest() == payload["installation_digest"], Code.DENIED)
    config = _decode(raw)
    require(type(config.get("schema")) is int and config["schema"] in RESULTS)
    version = config["schema"]
    base = {
        "schema",
        "plan",
        "candidate_sha256",
        "approval_file",
        "supervisor_directory",
        "supervisor_identity",
        "api_key_file",
        "ca_file",
    }
    extra = set() if version == 1 else {"template_pins"}
    if version == 3:
        extra |= {"exclusive_window"}
    require(set(config) == base | extra)
    require(type(config["plan"]) is dict)
    plan = CreatePlan(**config["plan"])
    plan.binding(payload["approval_digest"])
    require(expected_plan is None or plan == expected_plan, Code.DENIED)
    _digest(config["candidate_sha256"])
    require(plan.candidate_sha256 == config["candidate_sha256"], Code.DENIED)
    paths = {}
    for key in ("approval_file", "supervisor_directory", "api_key_file", "ca_file"):
        require(type(config[key]) is str)
        paths[key] = Path(config[key])
        require(paths[key].is_absolute() and paths[key].resolve() == paths[key], Code.DENIED)
    require(type(config["supervisor_identity"]) is dict)
    pins = IdentityPins(**config["supervisor_identity"])
    pins.validate()
    supervisor = SupervisorReader(paths["supervisor_directory"])
    require(supervisor.anchor.boot_id == plan.boot_id, Code.DENIED)

    def approve():
        deadline = read_create_approval(plan, paths["approval_file"], pinned_digest=payload["approval_digest"])
        if version == 3:
            window = config["exclusive_window"]
            require(type(window) is dict and set(window) == {"path", "digest", "creator_id", "writers"})
            require(type(window["path"]) is str and type(window["writers"]) is list)
            snapshot = read_window(
                Path(window["path"]),
                pinned_digest=window["digest"],
                plan=plan,
                creator_id=window["creator_id"],
                expected_writers=tuple(window["writers"]),
                now=supervisor._sample(),
            )
            deadline = min(deadline, snapshot.expires_mono)
        return deadline

    first_deadline = approve()
    def read_supervisor():
        if registered_ids:
            require(expected_plan is not None and version == 3, Code.DENIED)
            return supervisor.read_registered(run_id=plan.run_id, template_id=plan.template_id, registered_ids=registered_ids)
        return supervisor.read_empty(run_id=plan.run_id, template_id=plan.template_id)

    first = read_supervisor()
    first_identity = verify_identity(first, pins)
    api_key = config_bytes(paths["api_key_file"])
    reader = PlatformReader(
        endpoint=plan.endpoint,
        api_key=api_key.decode("ascii"),
        ca_file=paths["ca_file"],
        domain=plan.domain,
        proxy_port=13080,
    )
    template_observed = None
    if version >= 2:
        require(type(config["template_pins"]) is dict)
        template_pins = TemplatePins(**config["template_pins"])
        require(template_pins.template_id == plan.template_id, Code.DENIED)
        _, template_observed = reader.collect_template(template_pins)
    observed = reader.collect_empty()
    final = read_supervisor()
    final_identity = verify_identity(final, pins)
    require(
        (first_identity.pid, first_identity.start_ticks) == (final_identity.pid, final_identity.start_ticks),
        Code.DENIED,
    )
    final_deadline = approve()
    now = supervisor._sample()
    if template_observed is not None:
        require(0 <= now.monotonic - template_observed <= 2, Code.DENIED)
    require(0 <= now.monotonic - observed <= 2 and 0 <= now.monotonic - final.observed_mono <= 2, Code.DENIED)
    require(config_bytes(paths["api_key_file"]) == api_key, Code.DENIED)
    require(hashlib.sha256(config_bytes(path)).hexdigest() == payload["installation_digest"], Code.DENIED)
    # Include private-file revalidation time in freshness, not just HTTP time.
    finished = supervisor._sample()
    expiry = min(first_deadline, final_deadline, observed + 2, final.observed_mono + 2)
    if template_observed is not None:
        expiry = min(expiry, template_observed + 2)
    require(math.isfinite(expiry) and now.monotonic <= finished.monotonic < expiry, Code.DENIED)
    return PrecreateEvidence(
        version,
        plan,
        payload["installation_digest"],
        payload["approval_digest"],
        finished.monotonic,
        expiry,
        min(first_deadline, final_deadline),
    )


def perform(payload: dict) -> dict:
    return collect_evidence(payload).report()


def main() -> None:
    try:
        sys.stdout.write(canonical(perform(_decode(sys.stdin.buffer.read(65537)))))
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
