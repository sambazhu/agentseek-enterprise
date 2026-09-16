"""Read-only bridge from sealed create receipt to an independently approved row.

No create approval is promoted to case approval. The existing exact-Binding
approval format must already be privately provisioned and independently pinned.
The sandbox ID is read from the sealed response, never a caller-selected ID.
No approval/config writer, HTTP, dispatch, case claim or aggregate gate here.
Live W0/template/network/supervision/lifetime checks are still required before
sending; an approved row alone is not evidence that the guest is alive or safe.
Run private-file/AEAD work inside the trusted caller's outer process budget.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .m3_auth_probe import Request
from .m3_create_receipt import CreateReceiptVault
from .m3_platform_evidence import read_approval
from .m3_probe_dispatch import Binding
from .m3_probe_process import config_bytes
from .m3_receipt_probe import ReceiptProbeSource, prepare_receipted
from .m3_supervisor_snapshot import _clock_valid, system_clock
from .m3_template_evidence import TemplatePins
from .models import Code, ContractError, require


@dataclass(frozen=True)
class ApprovedReceiptCase:
    binding: Binding
    request: Request = field(repr=False)
    approval_deadline_mono: float
    observed_mono: float
    aggregate_ready: bool = False


def load_approved_case(
    config_path: Path,
    approval_path: Path,
    *,
    approval_digest: str,
    approval_ref: str,
    case_id: str,
    source: ReceiptProbeSource,
    template_pins: TemplatePins,
) -> ApprovedReceiptCase:
    """Bind existing schema-3 selectors and exact schema-1 case approval.

    Approval ref is a stable installed identifier, NOT its own document digest
    (which would introduce a circular hash). Digest remains an independent pin.
    Wrong-token rows use fresh random credentials via the existing adapter.
    """
    try:
        require(os.getuid() == 0 and os.geteuid() == 0, Code.DENIED)
        require(type(source) is ReceiptProbeSource and type(template_pins) is TemplatePins, Code.DENIED)
        source.create.validate()
        template_pins.validate()
        require(source.create.template_id == template_pins.template_id, Code.DENIED)
        started = system_clock()
        _clock_valid(started)
        require(started.boot_id == source.create.boot_id, Code.DENIED)
        key = config_bytes(source.vault_key_file)
        api_key = config_bytes(source.api_key_file)
        vault = CreateReceiptVault(source.vault_directory, key)
        receipt = vault.read(source.create)
        # Reject missing/noncanonical intent as well as missing receipt. This
        # does not establish the live guest deadline; that needs platform reads.
        vault.read_intent(source.create)
        raw = config_bytes(config_path)
        binding = Binding(
            source.create.run_id,
            source.create.create_token,
            receipt.sandbox_id,
            source.create.template_id,
            template_pins.artifact_sha256,
            case_id,
            hashlib.sha256(raw).hexdigest(),
            approval_ref,
            source.create.candidate_sha256,
            source.create.boot_id,
        )
        approval = read_approval(
            approval_path,
            pinned_digest=approval_digest,
            binding=binding,
            now_epoch=started.epoch,
        )
        request = prepare_receipted(config_path, binding=binding, source=source)
        finished = system_clock()
        _clock_valid(finished)
        elapsed = finished.monotonic - started.monotonic
        require(
            finished.boot_id == started.boot_id
            and 0 <= elapsed <= 2
            and abs(finished.epoch - started.epoch - elapsed) <= 0.5
            and abs(finished.uptime - started.uptime - elapsed) <= 0.5,
            Code.DENIED,
        )
        final = read_approval(
            approval_path,
            pinned_digest=approval_digest,
            binding=binding,
            now_epoch=finished.epoch,
        )
        require(config_bytes(source.vault_key_file) == key and config_bytes(config_path) == raw, Code.DENIED)
        require(config_bytes(source.api_key_file) == api_key, Code.DENIED)
        require(vault.read(source.create) == receipt, Code.DENIED)
        deadline = min(
            started.monotonic + approval.expires_epoch - started.epoch,
            finished.monotonic + final.expires_epoch - finished.epoch,
        )
        end = system_clock()
        _clock_valid(end)
        elapsed = end.monotonic - started.monotonic
        require(
            end.boot_id == started.boot_id
            and finished.monotonic <= end.monotonic
            and 0 <= elapsed <= 2
            and abs(end.epoch - started.epoch - elapsed) <= 0.5
            and abs(end.uptime - started.uptime - elapsed) <= 0.5
            and deadline - end.monotonic >= 11,
            Code.DENIED,
        )
        return ApprovedReceiptCase(binding, request, deadline, end.monotonic)
    except Exception:
        raise ContractError(Code.DENIED) from None
