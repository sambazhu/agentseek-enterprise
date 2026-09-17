"""Trusted host entry for a watchdog-bounded complete approved R1 row.

No automatic installation, approvals, create/kill, retry or fence release.
Host supplies independent pins and deadline, never values from a user job.
One fresh process contains collection AND the data-plane request. It does not
spawn the detached receipt worker, which would escape outer group cleanup.
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from .m3_auth_probe import observe
from .m3_case_dispatch import dispatch_approved_case, installed_collector
from .m3_probe_dispatch import Binding, DispatchDirectory, _validate_result
from .m3_probe_process import _decode, config_bytes
from .m3_receipt_probe import ReceiptProbeSource, prepare_receipted
from .m3_template_evidence import TemplatePins
from .models import Code, canonical, require
from .worker_process import run_worker

CASE_BUDGET_SECONDS = 15
CLEANUP_RESERVE_SECONDS = 1


def dispatch_isolated(installation: Path, directory: Path, *, installation_digest: str,
                      approval_digest: str, deadline: float) -> dict:
    """At most 15s total plus 1s cleanup; OS launch is not a realtime guarantee.

    Deadline uses the same host's monotonic clock. Timeout is UNKNOWN, never
    evidence that the row did not run or that its guest has terminated.
    """
    require(type(deadline) in {int, float} and math.isfinite(deadline), Code.DENIED)
    now = time.monotonic()
    # Never start a truncated row near the guest/approval cutoff. The caller
    # supplies the earlier trusted cutoff, not a fresh per-row guest lifetime.
    require(deadline - now >= CASE_BUDGET_SECONDS + CLEANUP_RESERVE_SECONDS, Code.DENIED)
    stop = now + CASE_BUDGET_SECONDS
    raw = run_worker(
        [sys.executable, "-I", "-m", "agentseek_execution.m3_case_process"],
        canonical({"installation": str(installation), "directory": str(directory),
                   "installation_digest": installation_digest, "approval_digest": approval_digest,
                   "deadline": stop}).encode(),
        budget=stop - now, environment={},
    )
    result = _decode(raw)
    _validate_result(result)
    return result


def _inline(path, *, binding, source, verified_deadline):
    request = prepare_receipted(path, binding=binding, source=source)
    remaining = verified_deadline - time.monotonic()
    require(remaining >= 10, Code.DENIED)
    result = asdict(observe(request, remaining_seconds=min(remaining, 10)))
    _validate_result(result)
    return result


def perform(payload: dict) -> dict:
    require(os.getuid() == 0 and os.geteuid() == 0, Code.DENIED)
    require(type(payload) is dict and set(payload) == {
        "installation", "directory", "installation_digest", "approval_digest", "deadline",
    }, Code.DENIED)
    stop = payload["deadline"]
    require(type(stop) in {int, float} and math.isfinite(stop)
            and 11 <= stop - time.monotonic() <= CASE_BUDGET_SECONDS, Code.DENIED)
    installation = Path(payload["installation"])
    raw = config_bytes(installation)
    require(hashlib.sha256(raw).hexdigest() == payload["installation_digest"], Code.DENIED)
    config = _decode(raw)
    require(type(config.get("schema")) is int and config["schema"] == 4, Code.DENIED)
    binding = Binding(**config["binding"])
    binding.validate()
    collector = installed_collector(installation, installation_digest=payload["installation_digest"],
                                    approval_digest=payload["approval_digest"])

    def collect(actual):
        require(actual == binding and stop - time.monotonic() >= 11, Code.DENIED)
        result = collector(actual)
        bounded = replace(result, approval_deadline=min(result.approval_deadline, stop))
        bounded.verify(actual, time.monotonic())
        return bounded

    return dispatch_approved_case(
        DispatchDirectory(Path(payload["directory"])), Path(config["probe_config"]), Path(config["approval_file"]),
        approval_digest=payload["approval_digest"], approval_ref=binding.approval_ref, case_id=binding.case_id,
        source=ReceiptProbeSource.from_wire(config["receipt_source"]), template_pins=TemplatePins(**config["template_pins"]),
        collect=collect, observe=_inline,
    )


def main():
    try:
        result = perform(_decode(sys.stdin.buffer.read(65537)))
        sys.stdout.write(canonical(result))
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
