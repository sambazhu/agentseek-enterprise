"""Internal pinned single-create installation entry, NOT approved deployment.

Trusted root installation supplies out-of-band launch/candidate digests. A fresh
isolated Python subprocess reads all secrets and performs the create under one
outer 10s budget. Do not nest the fork watchdog: its separate session could escape
the outer parent's group cleanup. No directory/key/approval provisioning, retries,
empty fence release, supervisor installation or data-plane dispatch.
Schema 2 pins a batch quota and sequence for its first create. Schema 3 accepts
an adjacent sealed predecessor and collects live known-target disappearance
with an empty supervisor manifest before atomically handing off the fence.
The candidate digest is an installation trust anchor, not self-measurement of all
Python dependencies. Root/same-UID modification and rollback remain out of scope.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from .m3_batch_quota import BatchQuota
from .m3_closeout_evidence import collect_closeout
from .m3_create_admission import LiveCreateAdmission
from .m3_create_fence import CreateFence
from .m3_create_receipt import CreateBinding, CreateReceiptVault
from .m3_create_worker import CreatePlan, CreateWorker
from .m3_platform_evidence import PlatformReader
from .m3_precreate_process import _digest
from .m3_probe_process import _decode, config_bytes
from .m3_supervisor_identity import IdentityPins
from .m3_supervisor_snapshot import SupervisorReader, system_clock
from .models import Code, canonical, require
from .worker_process import run_worker


def launch_isolated(path: Path, *, installation_digest: str, candidate_sha256: str) -> CreateBinding:
    """Trusted caller only: fixed module, no secret values or arbitrary command.

    Covers private reads, TLS setup, both live gates, fence/intent and sealing.
    Process-launch overhead is not forcibly bounded; cleanup adds up to 1 second.
    Any uncertain result means no retry, even if no sealed binding is returned.
    """
    _digest(installation_digest)
    _digest(candidate_sha256)
    require(path.is_absolute() and path.resolve() == path, Code.DENIED)
    raw = run_worker(
        [sys.executable, "-I", "-m", "agentseek_execution.m3_create_process"],
        canonical({
            "path": str(path),
            "installation_digest": installation_digest,
            "candidate_sha256": candidate_sha256,
        }).encode(),
        budget=10,
        environment={},
    )
    require(len(raw) <= 8192, Code.UNKNOWN)
    result = _decode(raw)
    require(set(result) == {"schema", "installation_digest", "binding"}, Code.UNKNOWN)
    require(type(result["schema"]) is int and result["schema"] == 1, Code.UNKNOWN)
    require(result["installation_digest"] == installation_digest, Code.UNKNOWN)
    require(type(result["binding"]) is dict, Code.UNKNOWN)
    binding = CreateBinding(**result["binding"])
    binding.validate()
    require(binding.candidate_sha256 == candidate_sha256 and binding.intent_sha256 != "0" * 64, Code.UNKNOWN)
    return binding


def _path(value: str) -> Path:
    require(type(value) is str, Code.DENIED)
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path, Code.DENIED)
    return path


def _pinned(path: Path, digest: str) -> bytes:
    _digest(digest)
    raw = config_bytes(path)
    require(hashlib.sha256(raw).hexdigest() == digest, Code.DENIED)
    return raw


def _history_ids(vault, history, sequence):
    ids = []
    for index, binding in enumerate(history):
        require(replace(binding, intent_sha256="0" * 64) ==
                sequence[index].binding(binding.approval_sha256), Code.DENIED)
        vault.read_intent(binding)
        ids.append(vault.read(binding).sandbox_id)
    require(len(set(ids)) == len(ids), Code.DENIED)
    return tuple(ids)


def perform(payload: dict) -> dict:
    """Run ONLY in the externally bounded subprocess, never an in-process API."""
    require(sys.platform == "linux" and os.getuid() == 0 and os.geteuid() == 0, Code.DENIED)
    require(type(payload) is dict and set(payload) == {"path", "installation_digest", "candidate_sha256"})
    _digest(payload["candidate_sha256"])
    path = _path(payload["path"])
    raw = _pinned(path, payload["installation_digest"])
    installed = _decode(raw)
    require(type(installed.get("schema")) is int and installed["schema"] in {1, 2, 3}, Code.DENIED)
    batch_fields = {"batch_sequence", "quota_directory"} if installed["schema"] >= 2 else set()
    if installed["schema"] == 3:
        batch_fields |= {"previous_create", "previous_tracking_directory"}
        if "earlier_creates" in installed:
            batch_fields.add("earlier_creates")
    require(
        set(installed)
        == {
            "schema",
            "precreate_file",
            "precreate_sha256",
            "approval_sha256",
            "request_file",
            "vault_directory",
            "fence_directory",
            "receipt_key_file",
            "receipt_key_sha256",
            "api_key_sha256",
            "ca_sha256",
        } | batch_fields
    )
    precreate_path = _path(installed["precreate_file"])
    precreate = _decode(_pinned(precreate_path, installed["precreate_sha256"]))
    require(type(precreate.get("schema")) is int and precreate["schema"] == 3, Code.DENIED)
    require(type(precreate["plan"]) is dict, Code.DENIED)
    plan = CreatePlan(**precreate["plan"])
    plan.binding(installed["approval_sha256"])
    require(plan.candidate_sha256 == payload["candidate_sha256"], Code.DENIED)
    require(system_clock().boot_id == plan.boot_id, Code.DENIED)
    request_path = _path(installed["request_file"])
    request = _pinned(request_path, plan.request_sha256)
    api_path, ca_path = _path(precreate["api_key_file"]), _path(precreate["ca_file"])
    key_path = _path(installed["receipt_key_file"])
    api_key = _pinned(api_path, installed["api_key_sha256"])
    _pinned(ca_path, installed["ca_sha256"])
    receipt_key = _pinned(key_path, installed["receipt_key_sha256"])
    require(len(receipt_key) == 32, Code.DENIED)
    vault_path = _path(installed["vault_directory"])
    fence_path = _path(installed["fence_directory"])
    require(vault_path != fence_path, Code.DENIED)
    quota = None
    previous = None
    closeout = None
    registered_ids = ()
    history = []
    vault = CreateReceiptVault(vault_path, receipt_key)
    fence = CreateFence(fence_path)
    if installed["schema"] >= 2:
        require(type(installed["batch_sequence"]) is list, Code.DENIED)
        sequence = tuple(CreatePlan(**item) for item in installed["batch_sequence"])
        quota_path = _path(installed["quota_directory"])
        require(quota_path not in {vault_path, fence_path}, Code.DENIED)
        quota = BatchQuota(quota_path, sequence)
        if installed["schema"] == 2:
            require(plan == sequence[0], Code.DENIED)
        else:
            require(type(installed["previous_create"]) is dict, Code.DENIED)
            previous = CreateBinding(**installed["previous_create"])
            previous.validate()
            require(plan in sequence and sequence.index(plan) > 0, Code.DENIED)
            old = replace(previous, intent_sha256="0" * 64)
            require(old == sequence[sequence.index(plan) - 1].binding(previous.approval_sha256), Code.DENIED)
            vault.read(previous)
            vault.read_intent(previous)
            earlier = installed.get("earlier_creates", [])
            require(type(earlier) is list and len(earlier) == sequence.index(plan) - 1, Code.DENIED)
            history = [CreateBinding(**item) for item in earlier] + [previous]

            registered_ids = _history_ids(vault, history, sequence)
            tracking = _path(installed["previous_tracking_directory"])
            require(tracking not in {vault_path, fence_path, quota_path}, Code.DENIED)
            supervisor = SupervisorReader(_path(precreate["supervisor_directory"]))
            pins = IdentityPins(**precreate["supervisor_identity"])
            pins.validate()
            reader = PlatformReader(endpoint=plan.endpoint, api_key=api_key.decode("ascii"),
                                    ca_file=ca_path, domain=plan.domain, proxy_port=13080)

            def closeout():
                revalidate()
                result = collect_closeout(reader, fence, old, tracking, supervisor, pins,
                                          empty_manifest=True, registered_ids=registered_ids)
                revalidate()
                return result
    admission = LiveCreateAdmission(
        precreate_path,
        installation_digest=installed["precreate_sha256"],
        approval_digest=installed["approval_sha256"],
        plan=plan,
        registered_ids=registered_ids,
    )

    def revalidate():
        _pinned(path, payload["installation_digest"])
        _pinned(precreate_path, installed["precreate_sha256"])
        _pinned(request_path, plan.request_sha256)
        _pinned(api_path, installed["api_key_sha256"])
        _pinned(ca_path, installed["ca_sha256"])
        _pinned(key_path, installed["receipt_key_sha256"])
        require(system_clock().boot_id == plan.boot_id, Code.DENIED)
        if history:
            require(_history_ids(vault, history, sequence) == registered_ids, Code.DENIED)

    def collect():
        revalidate()
        started = time.monotonic()
        deadline = admission()
        revalidate()
        # Final private reads must not age the live observations past their 2s
        # freshness window. Bound the entire collection/revalidation interval.
        require(0 <= time.monotonic() - started < 2, Code.DENIED)
        return deadline

    worker = CreateWorker(
        plan=plan,
        approval_path=_path(precreate["approval_file"]),
        approval_digest=installed["approval_sha256"],
        installed_candidate_sha256=payload["candidate_sha256"],
        current_boot_id=plan.boot_id,
        api_key=api_key.decode("ascii"),
        ca_file=ca_path,
        vault=vault,
        fence=fence,
        collect_admission=collect,
        batch_quota=quota,
        previous_create=previous,
        collect_closeout=closeout,
    )
    binding = worker.create(request)
    return {"schema": 1, "installation_digest": payload["installation_digest"], "binding": asdict(binding)}


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(65537)
        sys.stdout.write(canonical(perform(_decode(raw))))
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
