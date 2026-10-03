import os
from dataclasses import asdict
from uuid import uuid4

import pytest
from agentseek_execution.broker_access import CredentialRegistry, Principal
from agentseek_execution.broker_clock import BrokerClock
from agentseek_execution.broker_lifecycle import Lifecycle
from agentseek_execution.cube_journal import CubeJournal
from agentseek_execution.models import ContractError, Scope
from agentseek_execution.secure_ledger import SecureLedger

KEY_A, KEY_B = "a" * 40, "b" * 40


@pytest.fixture
def system(tmp_path):
    key = os.urandom(32)
    ledger = SecureLedger(tmp_path / "runtime", key)
    journal = CubeJournal(tmp_path / "runtime", key)
    credentials = CredentialRegistry()
    for label, client_key in (("a", KEY_A), ("b", KEY_B)):
        credentials.register(client_key, Principal(label, frozenset({Scope("tenant", "de", label, label)})))
    now = [1000.0]
    clock = BrokerClock(ledger, wall=lambda: now[0], monotonic=lambda: now[0])
    calls = []

    def worker(resource_id, operation):
        calls.append((resource_id, operation))
        if operation == "create":
            journal.patch(resource_id, receipt={"sandboxID": "private-cube-id", "trafficAccessToken": "private-token"})
            return {"status": "created"}
        if operation == "delete":
            return {"status": "stopped"}
        return {"status": "executed", "stdout": "M2_OK"}

    controller = Lifecycle(
        credentials, journal, ledger, clock, worker, lambda: None, deployment="test-run", template_id="test-template"
    )
    yield controller, calls, now
    journal.close()
    ledger.close()


def test_create_replay_capacity_and_direct_has_no_work(system):
    controller, calls, _ = system
    request_id = uuid4().hex
    result = controller.create(KEY_A, request_id, "direct")
    assert controller.create(KEY_A, request_id, "direct") == result
    job = controller.journal.require_job(result["resource_id"])
    assert "work_id" not in job and "phase" not in job
    assert controller._execute_record(job).template_version == "test-template"
    assert len(calls) == 1
    assert "private-token" not in str(result) and "private-cube-id" not in str(result)
    with pytest.raises(ContractError):
        controller.create(KEY_B, uuid4().hex, "direct")
    assert len(calls) == 1
    controller.operate(KEY_A, result["resource_id"], "delete")
    work = controller.create(KEY_B, uuid4().hex, "work")
    assert controller.journal.require_job(work["resource_id"])["work_id"].startswith("m2-synthetic-")


@pytest.mark.parametrize("operation", ["inspect", "connect", "execute", "slow", "read", "write", "cancel", "delete"])
def test_foreign_resource_denied_before_worker(system, operation):
    controller, calls, _ = system
    resource = controller.create(KEY_A, uuid4().hex, "direct")["resource_id"]
    before = list(calls)
    with pytest.raises(ContractError):
        controller.operate(KEY_B, resource, operation)
    assert calls == before
    assert controller({"key": KEY_B, "method": "list"}) == {"resources": []}


@pytest.mark.parametrize("field", ["scope", "template_id", "now", "owner", "lease", "volume_mounts", "command"])
def test_client_cannot_supply_trusted_fields(system, field):
    controller, calls, _ = system
    with pytest.raises(ContractError):
        controller({"key": KEY_A, "method": "create", "request_id": uuid4().hex, "kind": "direct", field: "forged"})
    assert calls == [] and controller.journal.all() == ()


def test_gate_failure_has_no_intent_or_create(system):
    controller, calls, _ = system

    def fail():
        raise RuntimeError("gate-down")

    controller.gate = fail
    with pytest.raises(RuntimeError):
        controller.create(KEY_A, uuid4().hex, "direct")
    assert calls == [] and controller.journal.all() == ()


@pytest.mark.parametrize("receipt", [True, False])
def test_uncertain_create_never_recreates_and_restart_reconciles(system, receipt):
    controller, calls, _ = system
    original = controller.worker
    request_id = uuid4().hex

    def uncertain(resource_id, operation):
        if operation == "create":
            if receipt:
                original(resource_id, operation)
            raise TimeoutError
        return original(resource_id, operation)

    controller.worker = uncertain
    with pytest.raises(ContractError):
        controller.create(KEY_A, request_id, "direct")
    result = controller.create(KEY_A, request_id, "direct")
    assert result["state"] == "reconciling"
    controller.recover()
    assert controller.journal.require_job(result["resource_id"])["state"] == "stopped"
    assert [op for _, op in calls].count("create") == int(receipt)
    assert controller.ledger.state(result["resource_id"]) == "cancelled"


def test_unknown_stop_retains_capacity_and_recovers_later(system):
    controller, calls, _ = system
    resource = controller.create(KEY_A, uuid4().hex, "direct")["resource_id"]
    original = controller.worker

    def unavailable(*_):
        raise TimeoutError

    controller.worker = unavailable
    controller.recover()
    assert controller.tick_error
    with pytest.raises(ContractError):
        controller.create(KEY_B, uuid4().hex, "direct")
    assert controller.ledger.state(resource) == "running"
    controller.worker = original
    controller.tick()
    assert not controller.tick_error
    assert controller.journal.require_job(resource)["state"] == "stopped"
    assert calls[-1] == (resource, "delete")


def test_deadline_and_restart_do_not_reexecute_commands(system):
    controller, calls, now = system
    resource = controller.create(KEY_A, uuid4().hex, "direct")["resource_id"]
    controller.operate(KEY_A, resource, "execute")
    now[0] += 121
    with pytest.raises(ContractError):
        controller.operate(KEY_A, resource, "execute")
    controller.tick()
    controller.recover()
    assert [op for _, op in calls] == ["create", "direct", "delete"]


def test_reserved_intent_recovered_without_provider(system):
    controller, calls, _ = system
    resource = uuid4().hex
    controller.journal.insert(resource, {"resource_id": resource, "state": "reserved", "deployment": "test-run"})
    controller.recover()
    assert controller.journal.require_job(resource)["state"] == "stopped"
    assert calls == []


def test_current_scope_checked_on_idempotent_replay(system):
    controller, calls, _ = system
    request_id = uuid4().hex
    controller.create(KEY_A, request_id, "direct")
    controller.credentials.revoke(KEY_A)
    controller.credentials.register(KEY_A, Principal("a", frozenset({Scope("tenant", "de", "new", "new")})))
    with pytest.raises(ContractError):
        controller.create(KEY_A, request_id, "direct")
    assert len(calls) == 1


def test_journal_encrypted_receipt_tamper_and_wrong_key(system, tmp_path):
    controller, _, _ = system
    resource = controller.create(KEY_A, uuid4().hex, "direct")["resource_id"]
    raw = (tmp_path / "runtime" / "cube-journal.sqlite").read_bytes()
    for text in ("private-token", "private-cube-id", "test-run", KEY_A):
        assert text.encode() not in raw
    with pytest.raises(ContractError):
        CubeJournal(tmp_path / "runtime", os.urandom(32))
    controller.journal.db.execute("UPDATE jobs SET sealed=? WHERE id=?", (os.urandom(48), resource))
    with pytest.raises(ContractError):
        controller.journal.require_job(resource)


def test_lease_recovery_preserves_five_fields(system):
    controller, _, _ = system
    resource = controller.create(KEY_A, uuid4().hex, "direct")["resource_id"]
    assert asdict(controller.ledger.recover_lease(resource)) == controller.journal.require_job(resource)["lease"]
