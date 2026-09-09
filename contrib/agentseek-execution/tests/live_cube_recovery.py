"""Opt-in Linux B7 fixture; NEVER installed as a daemon/provider override.

Only fixed synthetic operations. Holds the existing Broker lock; operator must
stop its entire unit first and ensure no other PoC client is creating resources.
The fault runs in a bounded private child and interrupts receipt persistence.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from agentseek_execution.broker_access import CredentialRegistry, Principal
from agentseek_execution.broker_clock import BrokerClock
from agentseek_execution.broker_daemon import read_config
from agentseek_execution.broker_lifecycle import Lifecycle
from agentseek_execution.cube_journal import CubeJournal
from agentseek_execution.cube_worker import perform
from agentseek_execution.models import Code, ContractError, Scope, canonical, require
from agentseek_execution.secure_ledger import SecureLedger
from agentseek_execution.secure_ownership import load_key, load_service_key, private_file
from agentseek_execution.worker_process import run_worker


def fault_worker(request: dict) -> dict:
    require(request["operation"] == "create", Code.DENIED)
    original = CubeJournal.patch

    def drop_receipt(self, resource_id, **updates):
        if "receipt" in updates:
            # SDK create returned; simulate death BEFORE durable receipt write.
            raise ContractError(Code.UNKNOWN)
        return original(self, resource_id, **updates)

    with patch.object(CubeJournal, "patch", drop_receipt):
        return perform(request)


def inventory_worker(request: dict) -> dict:
    from cubesandbox import Config, Sandbox

    config = request["config"]
    cfg = Config(api_url=config["api_url"], api_key=load_service_key(Path(config["cube_key_file"])))
    require(Sandbox.list(config=cfg) == [], Code.CONFLICT)
    return {"status": "empty"}


def exercise(controller: Lifecycle, key: str) -> None:
    failed = False
    try:
        controller.create(key, uuid4().hex, "direct")
    except ContractError as error:
        require(error.code == Code.UNKNOWN)
        failed = True
    require(failed)
    jobs = controller.journal.all()
    require(len(jobs) == 1 and jobs[0]["state"] == "reconciling" and jobs[0].get("receipt") is None)
    controller.recover()  # production path: unique marker hydrate -> DELETE -> 404
    jobs = controller.journal.all()
    require(jobs[0]["state"] == "stopped" and bool(jobs[0].get("receipt")), Code.UNKNOWN)
    require(controller.ledger.state(jobs[0]["resource_id"]) == "cancelled", Code.UNKNOWN)


def run(config: dict, directory: Path, *, recover_only: bool) -> None:
    require(directory.is_absolute() and directory.resolve() == directory)
    require(directory != Path(config["runtime"]).resolve(), Code.DENIED)
    lock = private_file(Path(config["runtime"]) / "broker.lock")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not recover_only:
            directory.mkdir(mode=0o700)  # existing paths refused; never clear data
        else:
            require((directory / "cube-journal.sqlite").is_file() and (directory / "execution.sqlite").is_file())
        config = {**config, "runtime": str(directory)}
        key = load_key(Path(config["encryption_key_file"]))
        ledger = SecureLedger(directory, key)
        try:
            journal = CubeJournal(directory, key)
            try:
                _run_controller(config, journal, ledger, recover_only=recover_only)
            finally:
                journal.close()
        finally:
            ledger.close()
    finally:
        os.close(lock)


def _run_controller(config: dict, journal: CubeJournal, ledger: SecureLedger, *, recover_only: bool) -> None:
    env = {"REQUESTS_CA_BUNDLE": config["ca_file"], "SSL_CERT_FILE": config["ca_file"]}
    calls = []

    def invoke(resource_id, operation):
        payload = canonical({"config": config, "resource_id": resource_id, "operation": operation}).encode()
        if operation in {"create", "inventory"}:
            command = [sys.executable, "-I", str(Path(__file__).resolve()), "--child", operation]
        else:
            command = [sys.executable, "-I", "-m", "agentseek_execution.cube_worker"]
        calls.append(operation)
        return json.loads(run_worker(command, payload, budget=10, environment=env))

    def gate():
        require(
            run_worker(config["preflight_command"], b"", budget=10, environment={"PATH": "/usr/bin:/bin"}).strip()
            == b"READY",
            Code.DENIED,
        )

    credentials = CredentialRegistry()
    client_key = uuid4().hex
    credentials.register(client_key, Principal("b7-fixture", frozenset({Scope("m2", "fixture", "fixture", "fixture")})))
    controller = Lifecycle(
        credentials,
        journal,
        ledger,
        BrokerClock(ledger),
        invoke,
        gate,
        deployment=config["run_id"],
        template_id=config["template_id"],
    )
    if recover_only:
        require(len(journal.all()) == 1, Code.CONFLICT)
        controller.recover()
        require(all(job["state"] == "stopped" for job in journal.all()), Code.UNKNOWN)
        print("B7_FIXTURE_RECOVERED")
    else:
        gate()
        require(invoke("unused", "inventory") == {"status": "empty"})
        exercise(controller, client_key)
        require(calls.count("create") == 1 and calls.count("delete") == 1)
        print("B7_NO_RECEIPT_RECOVERY_PASS")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument("--child", choices=("create", "inventory"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.child:
            request = json.loads(sys.stdin.buffer.read(65537))
            result = fault_worker(request) if args.child == "create" else inventory_worker(request)
            print(canonical(result))
        else:
            require(args.config is not None and args.fixture_dir is not None)
            run(read_config(args.config), args.fixture_dir, recover_only=args.recover_only)
    except Exception:
        print("B7_FIXTURE_BLOCKED", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
