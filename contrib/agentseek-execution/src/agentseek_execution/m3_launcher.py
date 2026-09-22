"""Trusted explicit-slot launcher. No approval generation, retry, or automatic B slot.

Invoke from an installed isolated interpreter. Both create and attach run in
bounded subprocesses. A failed attach leaves the sealed receipt/fence untouched.
The next slot's existing schema-3 gate remains the only successor authority.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from .m3_closeout_evidence import collect_closeout
from .m3_create_fence import CreateFence
from .m3_create_process import _path, _pinned, launch_isolated
from .m3_create_receipt import CreateBinding, CreateReceiptVault
from .m3_platform_evidence import PlatformReader
from .m3_probe_process import _decode, config_bytes
from .m3_supervisor_identity import IdentityPins, verify_identity
from .m3_supervisor_snapshot import SupervisorReader
from .m3_target_tracking import track_pending
from .models import Code, canonical, require
from .worker_process import run_worker
from .execution_diagnostic import diagnosed, phase


@diagnosed("launcher")
def launch(config_path: Path, digest: str) -> dict:
    """Trusted installation pins the config digest out of band; never retries."""
    config = _decode(_pinned(config_path, digest))
    require(set(config) == {"schema", "create_file", "create_sha256", "candidate_sha256",
                            "supervisor_script", "supervisor_sha256", "tracking_directory"}, Code.DENIED)
    require(type(config["schema"]) is int and config["schema"] == 1, Code.DENIED)
    # Reject missing/corrupt registration installation before consuming a create.
    _pinned(_path(config["supervisor_script"]), config["supervisor_sha256"])
    tracking = _path(config["tracking_directory"])
    info = tracking.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
    require(not any(tracking.iterdir()), Code.DENIED)
    installed = _decode(_pinned(_path(config["create_file"]), config["create_sha256"]))
    precreate = _decode(_pinned(_path(installed["precreate_file"]), installed["precreate_sha256"]))
    require(tracking not in {_path(installed["vault_directory"]), _path(installed["fence_directory"]),
                             _path(precreate["supervisor_directory"])}, Code.DENIED)
    if "quota_directory" in installed:
        require(tracking != _path(installed["quota_directory"]), Code.DENIED)
    if "previous_tracking_directory" in installed:
        require(tracking != _path(installed["previous_tracking_directory"]), Code.DENIED)
    with phase("create_worker"):
        binding = launch_isolated(_path(config["create_file"]),
                                  installation_digest=config["create_sha256"],
                                  candidate_sha256=config["candidate_sha256"])
    with phase("attach_worker"):
        raw = run_worker([sys.executable, "-I", "-m", "agentseek_execution.m3_launcher", "--attach"],
                         canonical({"path": str(config_path), "sha256": digest, "binding": asdict(binding)}).encode(),
                         budget=10, environment={})
    result = _decode(raw)
    require(result == {"schema": 1, "binding": asdict(binding), "registered_and_observed": True}, Code.UNKNOWN)
    return result


@diagnosed("closeout")
def check_closed(config_path: Path, digest: str, binding: CreateBinding) -> dict:
    """One bounded read-only check; caller may not treat this as B approval.

    No polling or kill here. The independent supervisor owns termination.
    B's schema-3 create repeats current closeout and successor admission.
    """
    raw = run_worker([sys.executable, "-I", "-m", "agentseek_execution.m3_launcher", "--closeout"],
                     canonical({"path": str(config_path), "sha256": digest, "binding": asdict(binding)}).encode(),
                     budget=10, environment={})
    result = _decode(raw)
    require(result == {"schema": 1, "binding": asdict(binding), "known_target_absent": True,
                       "next_create_authorized": False}, Code.UNKNOWN)
    return result


def launch_successor(config_path: Path, digest: str, previous_path: Path, previous_digest: str,
                     binding: CreateBinding) -> dict:
    """Explicit B invocation; never prepares approvals or replaces previous binding."""
    binding.validate()
    current = _decode(_pinned(config_path, digest))
    previous = _decode(_pinned(previous_path, previous_digest))
    installed = _decode(_pinned(_path(current["create_file"]), current["create_sha256"]))
    old_installed = _decode(_pinned(_path(previous["create_file"]), previous["create_sha256"]))
    require(installed["schema"] == 3 and installed["previous_create"] == asdict(binding), Code.DENIED)
    require(installed["previous_tracking_directory"] == previous["tracking_directory"], Code.DENIED)
    for key in ("vault_directory", "fence_directory", "quota_directory", "batch_sequence"):
        require(installed[key] == old_installed[key], Code.DENIED)
    require(current["candidate_sha256"] == previous["candidate_sha256"] == binding.candidate_sha256, Code.DENIED)
    check_closed(previous_path, previous_digest, binding)
    # launch/create revalidate the pinned installation and current successor gate.
    return launch(config_path, digest)


def attach(payload: dict, *, closeout: bool = False) -> dict:
    """Internal bounded worker; accepts only a binding matching the pinned create."""
    require(sys.platform == "linux" and os.getuid() == 0 and os.geteuid() == 0, Code.DENIED)
    require(type(payload) is dict and set(payload) == {"path", "sha256", "binding"}, Code.DENIED)
    config = _decode(_pinned(_path(payload["path"]), payload["sha256"]))
    installed = _decode(_pinned(_path(config["create_file"]), config["create_sha256"]))
    precreate = _decode(_pinned(_path(installed["precreate_file"]), installed["precreate_sha256"]))
    binding = CreateBinding(**payload["binding"])
    binding.validate()
    require(binding.candidate_sha256 == config["candidate_sha256"], Code.DENIED)
    plan = precreate["plan"]
    for field in ("run_id", "create_token", "template_id", "boot_id", "candidate_sha256",
                  "request_sha256", "domain", "restricted"):
        require(getattr(binding, field) == plan[field], Code.DENIED)
    require(binding.approval_sha256 == installed["approval_sha256"], Code.DENIED)
    key = _pinned(_path(installed["receipt_key_file"]), installed["receipt_key_sha256"])
    vault = CreateReceiptVault(_path(installed["vault_directory"]), key)
    vault.read_intent(binding)
    receipt = vault.read(binding)
    fence = CreateFence(_path(installed["fence_directory"]))
    old = replace(binding, intent_sha256="0" * 64)
    fence.verify_pending(old)
    history = [CreateBinding(**item) for item in installed.get("earlier_creates", [])]
    if "previous_create" in installed:
        history.append(CreateBinding(**installed["previous_create"]))
    previous_ids = tuple(vault.read(item).sandbox_id for item in history)
    require(len(set(previous_ids)) == len(previous_ids) and receipt.sandbox_id not in previous_ids, Code.DENIED)
    supervisor_dir = _path(precreate["supervisor_directory"])
    reader = SupervisorReader(supervisor_dir)
    pins = IdentityPins(**precreate["supervisor_identity"])
    pins.validate()
    api_key = _pinned(_path(precreate["api_key_file"]), installed["api_key_sha256"])
    ca = _path(precreate["ca_file"])
    _pinned(ca, installed["ca_sha256"])
    platform = PlatformReader(endpoint=plan["endpoint"], api_key=api_key.decode("ascii"), ca_file=ca,
                              domain=binding.domain, proxy_port=13080)
    if closeout:
        evidence = collect_closeout(platform, fence, old, _path(config["tracking_directory"]), reader, pins,
                                    empty_manifest=True, registered_ids=(*previous_ids, receipt.sandbox_id))
        require(evidence.sandbox_id == receipt.sandbox_id and evidence.state == "known_target_absent", Code.UNKNOWN)
        _pinned(_path(payload["path"]), payload["sha256"])
        return {"schema": 1, "binding": asdict(binding), "known_target_absent": True,
                "next_create_authorized": False}
    first = (reader.read_registered(run_id=binding.run_id, template_id=binding.template_id, registered_ids=previous_ids)
             if previous_ids else reader.read_empty(run_id=binding.run_id, template_id=binding.template_id))
    identity = verify_identity(first, pins)
    script = _path(config["supervisor_script"])
    _pinned(script, config["supervisor_sha256"])
    # Same process group as the enclosing attach worker; no nested detached child.
    result = subprocess.run(  # noqa: S603 -- trusted pinned supervisor script, sealed ID
        [sys.executable, "-I", str(script), "--manifest", str(supervisor_dir / "manifest.json"),
         "--alarm-file", str(supervisor_dir / "alarm"), "--register-sandbox", receipt.sandbox_id],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={}, timeout=3, umask=0o077)
    require(result.returncode == 0, Code.UNKNOWN)
    manifest = _decode(config_bytes(supervisor_dir / "manifest.json"))
    registered_at = manifest["sandboxes"][receipt.sandbox_id]["registered_at"]
    # The reader requires registration timestamps not newer than the heartbeat.
    # Wait only for a fresh real heartbeat; never rewrite it or retry creation.
    deadline = time.monotonic() + 3
    while True:
        beat = _decode(config_bytes(supervisor_dir / "manifest.json.heartbeat"))
        if beat["ts"] >= registered_at:
            break
        require(time.monotonic() < deadline, Code.UNKNOWN)
        time.sleep(0.05)
    final = reader.read_registered(run_id=binding.run_id, template_id=binding.template_id,
                                   registered_ids=(*previous_ids, receipt.sandbox_id))
    current_identity = verify_identity(final, pins)
    require((identity.pid, identity.start_ticks) == (current_identity.pid, current_identity.start_ticks), Code.UNKNOWN)
    observed = track_pending(platform, fence, old, _path(config["tracking_directory"]))
    require(observed.state == "exact_running_observed" and observed.sandbox_id == receipt.sandbox_id, Code.UNKNOWN)
    _pinned(_path(payload["path"]), payload["sha256"])
    return {"schema": 1, "binding": asdict(binding), "registered_and_observed": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--attach", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--closeout", action="store_true", help="Read-only: pinned config and binding via stdin")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--previous-config", type=Path)
    parser.add_argument("--previous-sha256")
    parser.add_argument("--binding-file", type=Path, help="Private prior launcher result, pinned separately")
    parser.add_argument("--binding-sha256")
    args = parser.parse_args()
    try:
        require(sys.flags.isolated and sys.platform == "linux", Code.DENIED)
        if args.attach or args.closeout:
            result = attach(_decode(sys.stdin.buffer.read(16385)), closeout=args.closeout)
        else:
            require(args.config is not None and args.sha256 is not None, Code.DENIED)
            prior = (args.previous_config, args.previous_sha256, args.binding_file, args.binding_sha256)
            require(all(item is None for item in prior) or all(item is not None for item in prior), Code.DENIED)
            if args.previous_config is not None:
                saved = _decode(_pinned(_path(str(args.binding_file)), args.binding_sha256))
                require(set(saved) == {"schema", "binding", "registered_and_observed"}
                        and saved["schema"] == 1 and saved["registered_and_observed"] is True, Code.DENIED)
                result = launch_successor(args.config, args.sha256, args.previous_config, args.previous_sha256,
                                          CreateBinding(**saved["binding"]))
            else:
                result = launch(args.config, args.sha256)
        sys.stdout.write(canonical(result))
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
