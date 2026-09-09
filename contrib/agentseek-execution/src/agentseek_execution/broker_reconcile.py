"""Offline trusted-operator adjudication, NOT a Broker/client endpoint.

Evidence truth is attested by an authorized operator outside this program.
Hashes establish binding/integrity, not truth. Zero list results, age and TTL
are never accepted as automatic cessation evidence. No force/requeue option.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from .broker_clock import BrokerClock
from .broker_daemon import read_config
from .broker_wire import _object
from .cube_journal import CubeJournal
from .models import Code, ContractError, canonical, require
from .secure_ledger import SecureLedger
from .secure_ownership import load_key, private_file


def digest(value: dict) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def read_private(path: Path, limit: int = 1048576) -> bytes:
    require(path.is_absolute() and path.resolve() == path, Code.DENIED)
    fd = private_file(path, readonly=True)
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        require(0 < len(data) <= limit, Code.DENIED)
        return data
    finally:
        os.close(fd)


def binding(job: dict, ledger: SecureLedger) -> str:
    lease = ledger.recover_lease(job["resource_id"])
    require(lease is not None and asdict(lease) == job.get("lease"), Code.CONFLICT)
    return digest({
        name: job.get(name)
        for name in ("resource_id", "task_id", "principal", "scope", "deployment", "lease", "deadline", "receipt")
    })


def adjudicate(journal: CubeJournal, ledger: SecureLedger, resource_id: str, approval: dict, *, now: float) -> dict:
    """Trusted caller must hold daemon lock and have independently approved evidence."""
    require(
        set(approval)
        == {
            "schema",
            "resource_id",
            "binding",
            "verdict",
            "reviewer",
            "approval_reference",
            "observed_at",
            "provider_evidence",
            "provider_sha256",
            "worker_evidence",
            "worker_sha256",
        }
    )
    job = journal.require_job(resource_id)
    require(
        type(approval["schema"]) is int and approval["schema"] == 1 and approval["resource_id"] == resource_id,
        Code.DENIED,
    )
    require(approval["binding"] == binding(job, ledger), Code.CONFLICT)
    approval_digest = digest(approval)
    audit = job.get("adjudication")
    if audit is not None:
        require(audit["approval_digest"] == approval_digest, Code.CONFLICT)
        if audit["state"] == "applied":
            require(job["state"] == "stopped" and ledger.state(resource_id) == "cancelled", Code.CONFLICT)
            return {"status": "adjudicated", "resource_id": resource_id}
    require(job["state"] == "reconciling", Code.CONFLICT)
    require(approval["verdict"] in {"never_admitted", "terminated"}, Code.DENIED)
    for name in ("reviewer", "approval_reference"):
        require(
            type(approval[name]) is str and bool(approval[name].strip()) and len(approval[name]) <= 256, Code.DENIED
        )
    observed = approval["observed_at"]
    require(type(observed) in {int, float} and math.isfinite(observed), Code.DENIED)
    # A durable pending audit already admitted this exact approval while fresh.
    # Resume it after a long outage; do not require a conflicting new approval.
    if audit is None:
        require(0 <= now - observed <= 3600, Code.DENIED)
    for prefix in ("provider", "worker"):
        evidence = read_private(Path(approval[prefix + "_evidence"]))
        require(sha256(evidence).hexdigest() == approval[prefix + "_sha256"], Code.DENIED)
    audit = {
        "approval_digest": approval_digest,
        "verdict": approval["verdict"],
        "reviewer": approval["reviewer"],
        "approval_reference": approval["approval_reference"],
        "observed_at": observed,
        "provider_sha256": approval["provider_sha256"],
        "worker_sha256": approval["worker_sha256"],
        "state": "pending",
    }
    # Write intent before releasing the ledger writer. A crash can be resumed only
    # with the identical approval; never roll back or lose the evidence reference.
    journal.patch(resource_id, adjudication=audit)
    lease = ledger.recover_lease(resource_id)
    if lease is None:
        raise ContractError(Code.CONFLICT)
    if ledger.state(resource_id) != "cancelled":
        ledger.reconcile_stopped(lease, confirmed=True, cancelled=True)
    journal.patch(resource_id, state="stopped", adjudication={**audit, "state": "applied"})
    return {"status": "adjudicated", "resource_id": resource_id}


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline operator adjudication; stop Broker/workers first; no force")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument(
        "--approval", type=Path, help="0600 reviewed evidence manifest; omit for binding-only inspection"
    )
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        runtime = Path(config["runtime"])
        lock = private_file(runtime / "broker.lock")  # must already exist
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            key = load_key(Path(config["encryption_key_file"]))
            ledger = SecureLedger(runtime, key)
            try:
                journal = CubeJournal(runtime, key)
                try:
                    job = journal.require_job(args.resource_id)
                    require(job["deployment"] == config["run_id"], Code.DENIED)
                    result = {"resource_id": args.resource_id, "state": job["state"], "binding": binding(job, ledger)}
                    if args.approval is not None:
                        approval = json.loads(read_private(args.approval, 65536), object_pairs_hook=_object)
                        require(type(approval) is dict)
                        result = adjudicate(journal, ledger, args.resource_id, approval, now=BrokerClock(ledger).now())
                    print(canonical(result))
                finally:
                    journal.close()
            finally:
                ledger.close()
        finally:
            os.close(lock)
    except Exception:
        print("RECONCILIATION_REFUSED", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
