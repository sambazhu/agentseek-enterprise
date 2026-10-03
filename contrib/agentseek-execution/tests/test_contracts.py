import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from threading import Barrier

import pytest
from agentseek_execution.fakes import FakeAuthority, FakeContentStore, FakeProvider
from agentseek_execution.ledger import Ledger
from agentseek_execution.models import (
    Code,
    ContractError,
    Execution,
    FileEntry,
    InputFile,
    InputManifest,
    OutputManifest,
    Scope,
    Task,
)
from agentseek_execution.service import Service


@pytest.fixture
def kit(tmp_path):
    scope = Scope("tenant", "de", "conversation", "alice")
    task = Task("task", scope)
    execution = Execution("exec", task, InputManifest(), "template-v1", "policy-v1")
    authority = FakeAuthority(tasks={task})
    provider, content = FakeProvider(), FakeContentStore()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = Service(ledger, authority, provider, content)
    yield service, execution
    ledger.close()


def output(data=b"hello"):
    entry = FileEntry("file", "v1", "result.txt", sha256(data).hexdigest(), len(data), "text/plain")
    return OutputManifest((entry,)), {entry.path: data}


def start(kit):
    service, execution = kit
    return service.start(execution.task.scope, execution, "worker", 100, 10)


def finish(kit, lease, token=None, now=101, data=b"hello"):
    service, execution = kit
    service.provider.finished.add(lease.create_token)
    manifest, contents = output(data)
    return service.submit(execution.task.scope, execution, lease, manifest, contents, token or "commit", now)


@pytest.mark.parametrize("field", ["tenant", "digital_employee", "conversation", "requester"])
def test_forged_scope_never_calls_provider(kit, field):
    service, execution = kit
    actor = replace(execution.task.scope, **{field: "other"})
    with pytest.raises(ContractError) as error:
        service.start(actor, execution, "worker", 100, 10)
    assert error.value.code == Code.DENIED
    assert service.provider.create_calls == 0


@pytest.mark.parametrize("mode", ["missing", "expired", "other_owner", "wrong_version", "revoked"])
def test_file_grants_are_not_content_hashes(kit, mode):
    service, execution = kit
    entry = output()[0].files[0]
    execution = replace(execution, inputs=InputManifest((InputFile(entry, "grant"),)))
    owner = execution.task.scope
    granted = entry
    if mode == "other_owner":
        owner = replace(owner, requester="bob")
    if mode == "wrong_version":
        granted = replace(entry, version="v2")
    if mode not in {"missing", "revoked"}:
        service.authority.grants["grant"] = (owner, granted, 100 if mode == "expired" else 200)
    with pytest.raises(ContractError):
        service.start(execution.task.scope, execution, "worker", 100, 10)
    assert service.provider.create_calls == 0


def test_work_requires_explicit_permission(kit):
    service, execution = kit
    task = replace(execution.task, work_id="work", phase="draft")
    execution = replace(execution, task=task)
    service.authority.tasks.add(task)
    with pytest.raises(ContractError):
        start((service, execution))
    service.authority.works.add((task.scope, "work", "draft"))
    assert start((service, execution)).attempt == 1


def test_direct_turn_commits_without_work_and_reopens(kit, tmp_path):
    service, execution = kit
    lease = start(kit)
    revision = finish(kit, lease)
    assert execution.task.work_id is None
    assert revision.revision == 1
    assert service.authority.works == set()
    reopened = Ledger(tmp_path / "ledger.sqlite")
    try:
        service.ledger = reopened
        assert finish(kit, lease, now=999) == revision
    finally:
        reopened.close()


def test_single_writer_expiry_does_not_start_competing_vm(kit):
    service, execution = kit
    start(kit)
    with pytest.raises(ContractError):
        service.start(execution.task.scope, replace(execution, execution_id="other"), "other", 200, 10)
    assert service.provider.create_calls == 1


def test_expired_and_replaced_fencing_rejected(kit):
    service, execution = kit
    lease = start(kit)
    with pytest.raises(ContractError):
        finish(kit, lease, now=110)
    with pytest.raises(ContractError):
        service.ledger.reconcile_stopped(lease, confirmed=False)
    service.ledger.reconcile_stopped(lease, confirmed=True)
    new = service.start(execution.task.scope, execution, "new", 120, 10)
    assert new.attempt == 2 and new.fencing > lease.fencing
    with pytest.raises(ContractError):
        finish(kit, lease, now=121)
    assert finish(kit, new, now=121).revision == 1


def test_same_token_different_content_rejected(kit):
    lease = start(kit)
    first = finish(kit, lease)
    assert finish(kit, lease) == first
    with pytest.raises(ContractError):
        finish(kit, lease, data=b"different")


def test_content_write_response_loss_retries_without_reexecution(kit):
    service, execution = kit
    lease = start(kit)
    service.content.lose_write_response = True
    with pytest.raises(ContractError):
        finish(kit, lease)
    assert service.ledger.state(execution.execution_id) == "validating"
    assert len(service.content.objects) == 1
    service.content.lose_write_response = False
    assert finish(kit, lease).revision == 1
    assert service.provider.create_calls == 1


@pytest.mark.parametrize("operation", ["create", "cancel"])
def test_unknown_outcome_retains_active_writer(kit, operation):
    service, execution = kit
    if operation == "create":
        service.provider.lose_create_response = True
        with pytest.raises(ContractError):
            start(kit)
    else:
        lease = start(kit)
        service.provider.lose_cancel_response = True
        with pytest.raises(ContractError):
            service.cancel(execution.task.scope, execution, lease, 101)
    assert service.ledger.state(execution.execution_id) == "reconciling"
    with pytest.raises(ContractError):
        start(kit)
    assert service.provider.create_calls == 1


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../x", "a//b", "a/./b", "C:x", "a\\b", "a\x00b"])
def test_invalid_paths(path):
    with pytest.raises(ContractError):
        replace(output()[0].files[0], path=path)


def test_duplicate_and_ancestor_output_paths():
    entry = output()[0].files[0]
    for other in (entry, replace(entry, path="result.txt/child")):
        with pytest.raises(ContractError):
            OutputManifest((entry, other))


def test_digest_and_quota_reject_before_publication(kit):
    service, execution = kit
    lease = start(kit)
    manifest, _contents = output()
    service.provider.finished.add(lease.create_token)
    with pytest.raises(ContractError):
        service.submit(execution.task.scope, execution, lease, manifest, {"result.txt": b"wrong"}, "c", 101)
    service.max_bytes = 1
    with pytest.raises(ContractError):
        finish(kit, lease)
    assert service.content.objects == {}
    assert service.ledger.state(execution.execution_id) == "running"


def test_independent_tasks_same_filename_do_not_overwrite(kit):
    service, execution = kit
    a = start(kit)
    task = replace(execution.task, task_id="second")
    service.authority.tasks.add(task)
    second = replace(execution, task=task, execution_id="second-exec")
    b = start((service, second))
    first = finish(kit, a)
    other = finish((service, second), b, token="other-commit", data=b"second")
    assert first.task_id != other.task_id
    assert len(service.content.objects) == 2


def test_revocation_before_submit_denies(kit):
    service, _execution = kit
    lease = start(kit)
    service.authority.tasks.clear()
    with pytest.raises(ContractError):
        finish(kit, lease)
    assert service.content.objects == {}


def test_forged_execution_cannot_submit_other_lease(kit):
    service, execution = kit
    lease = start(kit)
    other = replace(execution, policy_version="forged")
    with pytest.raises(ContractError):
        finish((service, other), lease)


def test_renew_and_confirmed_cancel(kit):
    service, execution = kit
    lease = start(kit)
    renewed = service.ledger.renew(lease, 105, 10)
    assert renewed.expires_at == 115
    service.cancel(execution.task.scope, execution, renewed, 111)
    assert service.ledger.state(execution.execution_id) == "cancelled"


def test_two_connections_compete_transactionally(kit, tmp_path):
    _service, execution = kit
    barrier = Barrier(2)

    def acquire(index):
        ledger = Ledger(tmp_path / "ledger.sqlite")
        try:
            barrier.wait(timeout=5)
            return ledger.begin(replace(execution, execution_id=str(index)), str(index), 100, 10)
        except ContractError:
            return None
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, [1, 2]))
    assert sum(item is not None for item in results) == 1


def test_commit_rollback_after_objects_persisted(kit):
    service, execution = kit
    lease = start(kit)
    service.ledger.db.execute("""CREATE TRIGGER fail_publish BEFORE UPDATE OF revision ON tasks
        BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        finish(kit, lease)
    assert service.ledger.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
    assert service.ledger.state(execution.execution_id) == "committing"
    assert len(service.content.objects) == 1
    service.ledger.db.execute("DROP TRIGGER fail_publish")
    assert finish(kit, lease).revision == 1
    assert service.provider.create_calls == 1


@pytest.mark.parametrize("field,value", [("fencing", 999), ("base_revision", 9), ("owner", "other")])
def test_forged_lease_rejected_including_committed_replay(kit, field, value):
    lease = start(kit)
    forged = replace(lease, **{field: value})
    with pytest.raises(ContractError):
        finish(kit, forged)
    finish(kit, lease)
    with pytest.raises(ContractError):
        finish(kit, forged)


def test_live_writer_cannot_publish_even_if_exit_code_would_be_zero(kit):
    service, execution = kit
    lease = start(kit)
    manifest, contents = output()
    with pytest.raises(ContractError):
        service.submit(execution.task.scope, execution, lease, manifest, contents, "c", 101)
    assert service.ledger.state(execution.execution_id) == "running"


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan")])
def test_invalid_lease_budget(kit, ttl):
    service, execution = kit
    with pytest.raises(ContractError):
        service.start(execution.task.scope, execution, "owner", 100, ttl)
    assert service.provider.create_calls == 0


def test_next_execution_inherits_committed_revision(kit):
    service, execution = kit
    first = start(kit)
    finish(kit, first)
    second = replace(execution, execution_id="next")
    lease = start((service, second))
    assert lease.base_revision == 1 and lease.fencing == 2
    assert finish((service, second), lease, token="next-commit").revision == 2


def test_retry_preserves_previous_attempt_record(kit):
    service, _execution = kit
    lease = start(kit)
    service.ledger.reconcile_stopped(lease, confirmed=True)
    assert start(kit).attempt == 2
    row = service.ledger.db.execute("SELECT attempt,snapshot FROM attempt_history").fetchone()
    assert row["attempt"] == 1
    assert '"state":"failed"' in row["snapshot"]
