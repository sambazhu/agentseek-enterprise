"""Internal approved-row to durable dispatch composition; not a live readiness gate.

The caller must supply the trusted, bounded live collector and an outer process
watchdog. No operator CLI, default collector, guest creation or fence release.
In particular, historical preflight JSON must never be used as live Evidence.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .m3_approved_case import load_approved_case
from .m3_probe_dispatch import Binding, DispatchDirectory, Evidence
from .m3_receipt_probe import ReceiptProbeSource
from .m3_template_evidence import TemplatePins
from .models import Code, require


def dispatch_approved_case(
    directory: DispatchDirectory,
    config_path: Path,
    approval_path: Path,
    *,
    approval_digest: str,
    approval_ref: str,
    case_id: str,
    source: ReceiptProbeSource,
    template_pins: TemplatePins,
    collect: Callable[[Binding], Evidence],
    observe=None,
) -> dict:
    """Recheck independent case approval around live collection and reservation.

    Derive the guest ID from the sealed receipt, not the caller. Approval and
    config changes cannot change the reserved binding or extend its deadline.
    A denial after reservation burns the row through DispatchDirectory's normal
    at-most-once contract; this wrapper does not delete or retry any records.
    """
    def approved():
        return load_approved_case(
            config_path, approval_path, approval_digest=approval_digest,
            approval_ref=approval_ref, case_id=case_id, source=source,
            template_pins=template_pins,
        )

    initial = approved()
    binding = initial.binding
    binding.validate()

    def checked() -> Evidence:
        before = approved()
        require(before.binding == binding, Code.DENIED)
        live = collect(binding)
        require(type(live) is Evidence, Code.DENIED)
        # Do not refresh observed_mono: approval reads cannot make old platform
        # observations current. Verify after the last private-file re-read.
        after = approved()
        require(after.binding == binding, Code.DENIED)
        bounded = replace(live, approval_deadline=min(
            initial.approval_deadline_mono, before.approval_deadline_mono,
            after.approval_deadline_mono, live.approval_deadline,
        ))
        bounded.verify(binding, time.monotonic())
        return bounded

    options = {} if observe is None else {"observe": observe}
    return directory.dispatch_receipted(binding, config_path, source=source, collect=checked, **options)


def installed_collector(installation_file: Path, *, installation_digest: str, approval_digest: str):
    """Bind trusted installation pins, never a previous observation document.

    Internal factory for dispatch_approved_case's collect argument. The hosting
    process must be watchdog-bounded; this is not an executable operator entry.
    """
    from .m3_evidence_process import collect_dispatch_evidence

    payload = {"path": str(installation_file), "installation_digest": installation_digest,
               "approval_digest": approval_digest}

    def collect(binding: Binding) -> Evidence:
        return collect_dispatch_evidence(dict(payload), binding=binding)

    return collect
