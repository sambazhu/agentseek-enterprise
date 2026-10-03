import os
from dataclasses import asdict
from hashlib import sha256
from uuid import uuid4

import pytest
from agentseek_execution.broker_reconcile import adjudicate, binding
from agentseek_execution.cube_journal import CubeJournal
from agentseek_execution.models import ContractError, Execution, InputManifest, Scope, Task
from agentseek_execution.secure_ledger import SecureLedger


@pytest.fixture
def case(tmp_path):
    key = os.urandom(32)
    ledger = SecureLedger(tmp_path / "runtime", key)
    journal = CubeJournal(tmp_path / "runtime", key)
    execution = Execution(
        uuid4().hex, Task(uuid4().hex, Scope("tenant", "de", "chat", "user")), InputManifest(), "template", "policy"
    )
    lease = ledger.begin(execution, "deployment", 1000, 120)
    job = {
        "resource_id": execution.execution_id,
        "task_id": execution.task.task_id,
        "principal": "a",
        "scope": asdict(execution.task.scope),
        "deployment": "deployment",
        "lease": asdict(lease),
        "deadline": 1120,
        "state": "reconciling",
    }
    journal.insert(job["resource_id"], job)
    approval = {
        "schema": 1,
        "resource_id": job["resource_id"],
        "binding": binding(job, ledger),
        "verdict": "never_admitted",
        "reviewer": "synthetic-operator",
        "approval_reference": "TEST-ONLY",
        "observed_at": 1300,
    }
    for prefix in ("provider", "worker"):
        path = tmp_path / prefix
        path.write_bytes(b"SYNTHETIC TEST EVIDENCE, NOT A REAL PLATFORM ATTESTATION")
        path.chmod(0o600)
        approval[prefix + "_evidence"] = str(path)
        approval[prefix + "_sha256"] = sha256(path.read_bytes()).hexdigest()
    yield journal, ledger, job["resource_id"], approval
    journal.close()
    ledger.close()


def test_adjudication_audit_release_and_exact_replay(case):
    journal, ledger, resource, approval = case
    result = adjudicate(journal, ledger, resource, approval, now=1301)
    assert result == {"status": "adjudicated", "resource_id": resource}
    assert ledger.state(resource) == "cancelled"
    assert ledger.db.execute("SELECT active FROM tasks").fetchone()[0] is None
    record = journal.require_job(resource)
    assert record["state"] == "stopped"
    assert record["adjudication"]["verdict"] == "never_admitted"
    assert record["adjudication"]["state"] == "applied"
    assert adjudicate(journal, ledger, resource, approval, now=99999) == result
    with pytest.raises(ContractError):
        adjudicate(journal, ledger, resource, {**approval, "approval_reference": "changed"}, now=1301)


@pytest.mark.parametrize(
    "field,value",
    [
        ("resource_id", "other"),
        ("binding", "0" * 64),
        ("verdict", "zero_matches"),
        ("verdict", "ttl_expired"),
        ("reviewer", ""),
        ("observed_at", 1400),
        ("observed_at", -9999),
        ("provider_sha256", "0" * 64),
    ],
)
def test_invalid_approval_never_releases_writer(case, field, value):
    journal, ledger, resource, approval = case
    with pytest.raises(ContractError):
        adjudicate(journal, ledger, resource, {**approval, field: value}, now=1301)
    assert journal.require_job(resource)["state"] == "reconciling"
    assert ledger.db.execute("SELECT active FROM tasks").fetchone()[0] == resource
    assert "adjudication" not in journal.require_job(resource)


@pytest.mark.parametrize(
    "field,value",
    [("owner", "forged"), ("attempt", 2), ("fencing", 999), ("base_revision", 9), ("create_token", "forged")],
)
def test_lease_binding_rejected(case, field, value):
    journal, ledger, resource, approval = case
    job = journal.require_job(resource)
    journal.patch(resource, lease={**job["lease"], field: value})
    with pytest.raises(ContractError):
        adjudicate(journal, ledger, resource, approval, now=1301)
    assert ledger.db.execute("SELECT active FROM tasks").fetchone()[0] == resource


@pytest.mark.parametrize("after_commit", [False, True])
def test_interrupted_adjudication_resumes_only_identical_approval(case, monkeypatch, after_commit):
    journal, ledger, resource, approval = case
    original_reconcile = ledger.reconcile_stopped
    original_patch = journal.patch

    def fail_reconcile(*args, **kwargs):
        raise OSError

    def fail_finalize(resource_id, **updates):
        if updates.get("state") == "stopped":
            raise OSError
        return original_patch(resource_id, **updates)

    monkeypatch.setattr(journal, "patch", fail_finalize)
    if not after_commit:
        monkeypatch.setattr(ledger, "reconcile_stopped", fail_reconcile)
    with pytest.raises(OSError):
        adjudicate(journal, ledger, resource, approval, now=1301)
    assert journal.require_job(resource)["adjudication"]["state"] == "pending"
    with pytest.raises(ContractError):
        adjudicate(journal, ledger, resource, {**approval, "reviewer": "someone-else"}, now=1301)
    monkeypatch.setattr(ledger, "reconcile_stopped", original_reconcile)
    monkeypatch.setattr(journal, "patch", original_patch)
    assert adjudicate(journal, ledger, resource, approval, now=99999)["status"] == "adjudicated"
