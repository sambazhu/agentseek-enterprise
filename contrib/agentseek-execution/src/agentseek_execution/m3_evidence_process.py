"""Bounded, read-only composition of R1 partial evidence collectors.

Internal entry only. Installation/approval digests are supplied by trusted host
composition, never taken from the file they authenticate. No probe dispatch,
guest commands, create/kill, approval writes or READY response exists here.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path

from .m3_create_receipt import CreateReceiptVault
from .m3_create_worker import CreatePlan
from .m3_exclusive_window import read_window
from .m3_lifetime_evidence import read_lifetime, read_sealed_lifetime
from .m3_network_evidence import read_network_intent
from .m3_platform_evidence import PlatformReader, read_approval
from .m3_probe_dispatch import Binding, Evidence
from .m3_probe_process import _decode, config_bytes
from .m3_receipt_probe import ReceiptProbeSource, prepare_receipted
from .m3_supervisor_identity import IdentityPins, verify_identity
from .m3_supervisor_snapshot import SupervisorReader
from .m3_template_evidence import TemplatePins
from .models import Code, ContractError, canonical, require
from .worker_process import run_worker

CHECKS = [
    "pinned_approval",
    "supervisor_files",
    "supervisor_process_identity",
    "platform_identity",
    "request_credentials",
    "local_request_deadline",
]
MISSING = [
    "template_artifact",
    "capacity_policy",
    "network_mode",
    "guest_hard_deadline",
    "donor_lifecycle",
    "unified_dispatch_gate",
]
RECEIPT_CHECKS = [*CHECKS, "sealed_create_receipt"]
RECEIPT_MISSING = ["exclusive_window_policy" if item == "capacity_policy" else item for item in MISSING]
WINDOW_CHECKS = [*RECEIPT_CHECKS, "template_api_binding", "pinned_window_confirmations"]
WINDOW_MISSING = [item for item in RECEIPT_MISSING if item not in {"template_artifact", "exclusive_window_policy"}]
NETWORK_CHECKS = [*WINDOW_CHECKS, "sealed_request_network_intent"]


def _report_fields(version: int) -> tuple[list, list]:
    return {
        1: (CHECKS, MISSING), 2: (RECEIPT_CHECKS, RECEIPT_MISSING),
        3: (WINDOW_CHECKS, WINDOW_MISSING), 4: (NETWORK_CHECKS, WINDOW_MISSING),
    }[version]


def _digest(value: str) -> None:
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def collect_isolated(installation_file: Path, *, installation_digest: str, approval_digest: str) -> dict:
    """10s total worker budget covers config, TLS/DNS, GETs and final rereads.

    Worker cleanup may take one additional second; OS process launch itself is
    not a hard real-time bound. Only hashes and path cross stdin, no key values.
    """
    _digest(installation_digest)
    _digest(approval_digest)
    require(installation_file.is_absolute() and installation_file.resolve() == installation_file, Code.DENIED)
    result = _decode(
        run_worker(
            [sys.executable, "-I", "-m", "agentseek_execution.m3_evidence_process"],
            canonical({
                "path": str(installation_file),
                "installation_digest": installation_digest,
                "approval_digest": approval_digest,
            }).encode(),
            budget=10,
            environment={},
        )
    )
    require(set(result) == {"schema", "aggregate_ready", "binding_digest", "checks", "missing"}, Code.UNKNOWN)
    require(
        type(result["schema"]) is int and result["schema"] in {1, 2, 3, 4} and result["aggregate_ready"] is False,
        Code.UNKNOWN,
    )
    checks, missing = _report_fields(result["schema"])
    require(result["checks"] == checks and result["missing"] == missing, Code.UNKNOWN)
    _digest(result["binding_digest"])
    return result


def _require_root() -> None:
    require(os.getuid() == 0 and os.geteuid() == 0, Code.DENIED)


def perform(payload: dict) -> dict:
    """Public observation stays non-authorizing for every configuration version."""
    return _collect(payload)


def collect_dispatch_evidence(payload: dict, *, binding: Binding) -> Evidence:
    """Trusted internal composition only, under an outer process watchdog.

    Reads installed schema-4 sources afresh; never accepts a report as evidence.
    Only the receipt adapter's correct/missing/wrong traffic cases are supported.
    Proxy enforcement and termination outcomes are observations AFTER a probe,
    not proofs required to send the independently approved probe itself.
    """
    return _collect(payload, expected_binding=binding)


def _collect(payload: dict, *, expected_binding: Binding | None = None):  # noqa: C901
    """Production child requires root; tests substitute OS ownership locally."""
    _require_root()
    require(type(payload) is dict and set(payload) == {"path", "installation_digest", "approval_digest"})
    _digest(payload["installation_digest"])
    _digest(payload["approval_digest"])
    require(type(payload["path"]) is str)
    installation = Path(payload["path"])
    raw = config_bytes(installation)
    require(hashlib.sha256(raw).hexdigest() == payload["installation_digest"], Code.DENIED)
    config = _decode(raw)
    require(type(config.get("schema")) is int and config["schema"] in {1, 2, 3, 4})
    version = config["schema"]
    if expected_binding is not None:
        require(version == 4, Code.DENIED)
    base = {
        "schema",
        "binding",
        "probe_config",
        "supervisor_directory",
        "approval_file",
        "control",
        "supervisor_identity",
    }
    extra = {"create_intent_file", "create_intent_digest"} if version == 1 else {"receipt_source"}
    if version >= 3:
        extra |= {"plan", "template_pins", "exclusive_window"}
    if version == 4:
        extra.add("create_request_file")
    require(set(config) == base | extra)
    require(type(config["binding"]) is dict)
    binding = Binding(**config["binding"])
    binding.validate()
    require(expected_binding is None or binding == expected_binding, Code.DENIED)
    if version == 1:
        require(type(config["create_intent_file"]) is str)
        _digest(config["create_intent_digest"])
    require(type(config["supervisor_identity"]) is dict)
    identity_pins = IdentityPins(**config["supervisor_identity"])
    identity_pins.validate()
    paths = {}
    for key in ("probe_config", "supervisor_directory", "approval_file"):
        require(type(config[key]) is str)
        path = Path(config[key])
        require(path.is_absolute() and path.resolve() == path, Code.DENIED)
        paths[key] = path
    control = config["control"]
    require(type(control) is dict and set(control) == {"endpoint", "api_key", "ca_file", "domain", "proxy_port"})
    require(type(control["ca_file"]) is str)
    supervisor = SupervisorReader(paths["supervisor_directory"])
    require(supervisor.anchor.boot_id == binding.boot_id, Code.DENIED)

    def approval():
        sample = supervisor._sample()
        snapshot = read_approval(
            paths["approval_file"], pinned_digest=payload["approval_digest"], binding=binding, now_epoch=sample.epoch
        )
        return sample.monotonic + snapshot.expires_epoch - sample.epoch

    approval_deadline = approval()
    expected = {"run_id": binding.run_id, "template_id": binding.template_id, "sandbox_id": binding.sandbox_id}
    initial = supervisor.read(**expected)
    initial_identity = verify_identity(initial, identity_pins)
    reader = PlatformReader(
        endpoint=control["endpoint"],
        api_key=control["api_key"],
        ca_file=Path(control["ca_file"]),
        domain=control["domain"],
        proxy_port=control["proxy_port"],
    )
    source = None
    vault = None
    template_observed = None
    first_window = None
    if version == 1:
        platform = reader.collect(binding, paths["probe_config"])
    else:
        source = ReceiptProbeSource.from_wire(config["receipt_source"])
        require(source.domain == control["domain"] and source.proxy_port == control["proxy_port"], Code.DENIED)
        require(config_bytes(source.api_key_file).decode("ascii") == control["api_key"], Code.DENIED)
        prepare_receipted(paths["probe_config"], binding=binding, source=source)
        vault = CreateReceiptVault(source.vault_directory, config_bytes(source.vault_key_file))
        if version >= 3:
            plan = CreatePlan(**config["plan"])
            expected_create = plan.binding(source.create.approval_sha256)
            require(
                all(getattr(expected_create, key) == value for key, value in asdict(source.create).items()
                    if key != "intent_sha256")
                and plan.endpoint == control["endpoint"], Code.DENIED,
            )
            template_pins = TemplatePins(**config["template_pins"])
            require(template_pins.template_id == binding.template_id
                    and template_pins.artifact_sha256 == binding.artifact_sha256, Code.DENIED)
            first_window = _window(config["exclusive_window"], plan, supervisor)
            _, template_observed = reader.collect_template(template_pins)
        if version == 4:
            require(type(config["create_request_file"]) is str, Code.DENIED)
            network = read_network_intent(Path(config["create_request_file"]), create=source.create, vault=vault)
            require(network.sandbox_id == binding.sandbox_id, Code.DENIED)
        platform = reader.collect_created(source.create, vault)
    # No claim that the sequential reads form an atomic platform snapshot.
    final = supervisor.read(**expected)
    final_identity = verify_identity(final, identity_pins)
    require(
        (initial_identity.pid, initial_identity.start_ticks) == (final_identity.pid, final_identity.start_ticks),
        Code.DENIED,
    )
    approval_deadline = min(approval_deadline, approval())
    now = supervisor._sample()
    if platform.started_epoch is None:
        raise ContractError(Code.DENIED)
    if version == 1:
        lifetime = read_lifetime(
            Path(config["create_intent_file"]),
            pinned_digest=config["create_intent_digest"],
            binding=binding,
            now=now,
            started_epoch=platform.started_epoch,
            end_epoch=platform.end_epoch,
        )
    else:
        if source is None or vault is None:
            raise ContractError(Code.DENIED)
        lifetime = read_sealed_lifetime(
            vault,
            create=source.create,
            binding=binding,
            now=now,
            started_epoch=platform.started_epoch,
            end_epoch=platform.end_epoch,
        )
        # Reread credential source after platform I/O too; never return a check
        # based solely on a receipt or private key that disappeared mid-collect.
        prepare_receipted(paths["probe_config"], binding=binding, source=source)
        require(config_bytes(source.api_key_file).decode("ascii") == control["api_key"], Code.DENIED)
    require(
        platform.run_id == final.run_id == binding.run_id
        and platform.sandbox_id == final.sandbox_id == binding.sandbox_id
        and platform.template_id == final.template_id == binding.template_id
        and platform.create_token == binding.create_token
        and final.boot_id == binding.boot_id,
        Code.DENIED,
    )
    require(
        0 <= now.monotonic - platform.observed_mono <= 2 and 0 <= now.monotonic - final.observed_mono <= 2, Code.DENIED
    )
    require(hashlib.sha256(config_bytes(paths["probe_config"])).hexdigest() == binding.config_sha256, Code.DENIED)
    require(hashlib.sha256(config_bytes(installation)).hexdigest() == payload["installation_digest"], Code.DENIED)
    if version >= 3:
        final_window = _window(config["exclusive_window"], plan, supervisor)
        if version == 4:
            require(read_network_intent(Path(config["create_request_file"]), create=source.create, vault=vault) == network, Code.DENIED)
        approval_deadline = min(approval_deadline, approval())
        require(hashlib.sha256(config_bytes(installation)).hexdigest() == payload["installation_digest"], Code.DENIED)
        finished = supervisor._sample()
        require(first_window.window_id == final_window.window_id, Code.DENIED)
        require(min(first_window.expires_mono, final_window.expires_mono) - finished.monotonic >= 11, Code.DENIED)
        for observed in (template_observed, platform.observed_mono, final.observed_mono):
            require(0 <= finished.monotonic - observed <= 2, Code.DENIED)
    if expected_binding is not None:
        evidence = Evidence(
            approval=binding, receipt=binding,
            observed_mono=min(template_observed, platform.observed_mono, final.observed_mono),
            heartbeat_mono=final.heartbeat_mono,
            approval_deadline=min(approval_deadline, first_window.expires_mono, final_window.expires_mono),
            guest_deadline=lifetime.stop_mono, supervisor_run_id=final.run_id, boot_id=final.boot_id,
            running=True, alarms_clear=True, capacity_one=False, platform_count=platform.platform_count,
            coordination_basis="approved_window", window_accepted=True,
        )
        evidence.verify(binding, supervisor._sample().monotonic)
        return evidence
    checks, missing = _report_fields(version)
    return {
        "schema": version,
        "aggregate_ready": False,
        "binding_digest": hashlib.sha256(canonical(asdict(binding)).encode()).hexdigest(),
        "checks": checks,
        "missing": missing,
    }


def _window(value: dict, plan: CreatePlan, supervisor: SupervisorReader):
    """Pinned human coordination only; not a platform admission guarantee."""
    require(type(value) is dict and set(value) == {"path", "digest", "creator_id", "writers"})
    require(type(value["path"]) is str and type(value["writers"]) is list)
    return read_window(
        Path(value["path"]), pinned_digest=value["digest"], plan=plan,
        creator_id=value["creator_id"], expected_writers=tuple(value["writers"]), now=supervisor._sample(),
    )


def main() -> None:
    try:
        sys.stdout.write(canonical(perform(_decode(sys.stdin.buffer.read(65537)))))
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
