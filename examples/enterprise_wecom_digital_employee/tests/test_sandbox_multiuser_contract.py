"""Synthetic multi-user contracts; no live model, HTTP, guest or message send."""

import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from agentseek_execution.business_execution import BusinessRequest
from agentseek_execution.csv_business import BusinessStore
from agentseek_files.models import FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore
from agentseek_wecom.file_delivery import (
    DeliveryDenied,
    DeliveryLedger,
    FileDeliveryBinding,
    file_ref,
    issue_capability,
    resolve_capability,
)
from enterprise_wecom_digital_employee.sandbox_authorization import (
    ApprovedGrantCatalog,
    SandboxGrant,
    SandboxRequestResolver,
    instruction_digest,
    scoped_owner,
)

SCOPE = tuple("sha256-" + char * 64 for char in "abc")
DATA = b"group,total\nA,1\n"


def runtime(scope):
    return SimpleNamespace(context={"enterprise": dict(zip(
        ("tenant_key", "user_key", "session_key"), scope, strict=True))}, state={})


def other_scope(axis):
    changed = list(SCOPE)
    changed[axis] = "sha256-" + "d" * 64
    return tuple(changed)


def grant(scope, request="synthetic-request"):
    return SandboxGrant(request, *scope, "file_same", instruction_digest("sum"), 4102444800)


@pytest.mark.parametrize("axis", [0, 1, 2], ids=["tenant", "user", "session"])
def test_foreign_grant_rejected_before_file_access(axis):
    def forbidden(*args):
        pytest.fail("foreign grant must not reach file access")
    resolver = SandboxRequestResolver(grant_for=lambda _: grant(SCOPE), file_allowed=forbidden)
    with pytest.raises(ValueError, match="scope mismatch"):
        resolver(runtime(other_scope(axis)), "file_same", "sum")
    assert scoped_owner(SCOPE) != scoped_owner(other_scope(axis))


@pytest.mark.parametrize("ambiguous", [False, True])
def test_catalog_selects_scope_and_rejects_two_live_grants(tmp_path, ambiguous):
    first, second = grant(SCOPE), grant(other_scope(1))
    values = [asdict(first), asdict(second)]
    if ambiguous:
        values.append(asdict(replace(first, request_id="second-action")))
    path = tmp_path / "grants.json"
    raw = json.dumps({"schema": 1, "approved": True, "grants": values}).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    catalog = ApprovedGrantCatalog(path, hashlib.sha256(raw).hexdigest())
    assert catalog(runtime(other_scope(1))) == second
    if ambiguous:
        with pytest.raises(ValueError, match="exactly one"):
            catalog(runtime(SCOPE))
    else:
        assert catalog(runtime(SCOPE)) == first


@pytest.fixture
def delivery_pair(tmp_path):
    store = LocalFileStore(FilesSettings(root_dir=tmp_path / "files"))
    private = tmp_path / "ledger"
    private.mkdir(mode=0o700)
    ledger, calls = DeliveryLedger(private), []

    def make(scope, recipient):
        record = store.store_bytes(scope=FileScope(*scope), filename="summary.csv",
                                   data=DATA, direction="outbound")

        async def upload(**kwargs):
            calls.append((recipient, "upload"))
            assert kwargs["content"] == DATA
            await asyncio.sleep(0)
            return "synthetic-media-" + recipient

        async def send(**kwargs):
            assert kwargs["media_id"] == "synthetic-media-" + recipient
            calls.append((recipient, "send"))
            return "succeeded"

        binding = FileDeliveryBinding(scope, "same-message-id", "把 summary.csv 发给我",
                                      recipient, store, ledger, upload, send)
        return binding, record

    return make, calls


@pytest.mark.parametrize("axis", [0, 1, 2], ids=["tenant", "user", "session"])
def test_same_bytes_foreign_ref_and_capability_denied(delivery_pair, axis):
    make, calls = delivery_pair
    a, ar = make(SCOPE, "recipient-a")
    b, br = make(other_scope(axis), "recipient-b")
    assert ar.file_id == br.file_id  # Content addressing is not authority.
    assert file_ref(ar) != file_ref(br)
    assert [r["file_ref"] for r in a.list_files()] == [file_ref(ar)]
    assert [r["file_ref"] for r in b.list_files()] == [file_ref(br)]
    token = issue_capability(a, clock=lambda: 0)
    with pytest.raises(DeliveryDenied):
        resolve_capability(token, b.scope, clock=lambda: 1)
    with pytest.raises(DeliveryDenied):
        asyncio.run(b.deliver(file_ref(ar)))
    assert calls == []
    with a.ledger.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0


def test_two_users_concurrent_duplicate_delivery_reservations_are_independent(delivery_pair):
    make, calls = delivery_pair
    a, ar = make(SCOPE, "recipient-a")
    b, br = make(other_scope(1), "recipient-b")

    async def run():
        return await asyncio.gather(a.deliver(file_ref(ar)), a.deliver(file_ref(ar)),
                                    b.deliver(file_ref(br)), b.deliver(file_ref(br)))

    results = asyncio.run(run())
    assert results[0]["delivery_id"] == results[1]["delivery_id"]
    assert results[2]["delivery_id"] == results[3]["delivery_id"]
    assert results[0]["delivery_id"] != results[2]["delivery_id"]
    for offset, recipient in ((0, "recipient-a"), (2, "recipient-b")):
        assert sorted(r["status"] for r in results[offset:offset+2]) == ["api_accepted", "uncertain"]
        assert calls.count((recipient, "upload")) == calls.count((recipient, "send")) == 1


def test_one_users_uncertain_send_does_not_retry_or_block_other_user(delivery_pair):
    make, calls = delivery_pair
    a, ar = make(SCOPE, "recipient-a")
    b, br = make(other_scope(1), "recipient-b")

    async def timeout(**kwargs):
        calls.append(("recipient-a", "timeout"))
        raise TimeoutError("synthetic-secret")

    a.send = timeout

    async def run():
        first = await a.deliver(file_ref(ar))
        duplicate = await a.deliver(file_ref(ar))
        other = await b.deliver(file_ref(br))
        return first, duplicate, other

    first, duplicate, other = asyncio.run(run())
    assert duplicate == dict(first, reused_receipt=True, delivery_notice="已有投递记录，本次未再次发送。")
    assert first["status"] == "uncertain"
    assert other["status"] == "api_accepted"
    assert calls.count(("recipient-a", "timeout")) == 1
    assert "synthetic-secret" not in json.dumps(first)


@pytest.mark.parametrize("axis", [0, 1, 2], ids=["tenant", "user", "session"])
def test_shared_store_artifact_access_requires_full_owner_scope(tmp_path, axis):
    tmp_path.chmod(0o700)
    store = BusinessStore(tmp_path)
    a = BusinessRequest("same-request", scoped_owner(SCOPE), "file_same", "sum")
    b = replace(a, owner_id=scoped_owner(other_scope(axis)))
    attempt_a = store.reserve(a)
    artifact_a = store.persist(attempt_a, a.owner_id, DATA)
    store.record(attempt_a, "succeeded", artifact_a)
    attempt_b = store.reserve(b)
    artifact_b = store.persist(attempt_b, b.owner_id, DATA)
    store.record(attempt_b, "succeeded", artifact_b)
    assert attempt_a != attempt_b and artifact_a != artifact_b
    assert store.read(b.owner_id, artifact_b) == DATA
    with pytest.raises(ValueError, match="unavailable"):
        store.read(b.owner_id, artifact_a)


def test_unresolved_store_is_global_serial_gate_not_per_user_capacity(tmp_path):
    tmp_path.chmod(0o700)
    store = BusinessStore(tmp_path)
    a = BusinessRequest("request-a", scoped_owner(SCOPE), "file_same", "sum")
    b = replace(a, request_id="request-b", owner_id=scoped_owner(other_scope(1)))
    attempt = store.reserve(a)
    with pytest.raises(ValueError, match="unresolved"):
        store.reserve(b)
    assert store.snapshot(b) is None
    store.record(attempt, "failed", None)
    assert store.reserve(b) != attempt
