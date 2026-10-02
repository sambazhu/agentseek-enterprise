"""Synthetic publication and read-only query contracts; no live guest or sends."""

# ruff: noqa: F811
import asyncio
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from agentseek_execution.business_execution import BusinessRequest
from agentseek_execution.csv_business import BusinessStore
from agentseek_files.models import FileScope
from agentseek_wecom.file_delivery import file_ref
from enterprise_wecom_digital_employee.sandbox_authorization import scoped_owner
from enterprise_wecom_digital_employee.sandbox_remote import remote_csv_tools
from enterprise_wecom_digital_employee.sandbox_workspace_binding import WorkspaceBindings, association
from test_sandbox_artifact_delivery_semantics import case  # noqa: F401
from test_sandbox_remote import remote  # noqa: F401


def successful(s, request=None):
    request = request or s.request
    attempt = s.gateway.reserve(request)
    artifact = s.gateway.persist(attempt, request.owner_id, s.output)
    s.gateway.record(attempt, "succeeded", artifact)
    return s.gateway.snapshot(request)


def test_run_publishes_association_query_never_rewrites(remote, monkeypatch):
    s = remote
    tools = remote_csv_tools(grant_for=lambda _: s.grant, file_store=s.files, runner=s.runner)
    first = asyncio.run(tools[0].coroutine(s.record.file_id, s.request.instruction, s.runtime))
    linked = first["workspace"]["association"]
    assert linked["state"] == "verified" and linked["request_id"] == s.request.request_id
    assert linked["attempt"] == first["attempt"] and linked["artifact_ref"] == first["artifact_ref"]
    bindings = WorkspaceBindings(s.runner, s.files)
    before = bindings.path.read_bytes(), bindings.path.stat().st_mtime_ns
    monkeypatch.setattr(s.files, "store_bytes", lambda **kw: pytest.fail("query must not write"))
    for _ in range(2):
        assert asyncio.run(tools[1].coroutine(s.runtime))["workspace"] == first["workspace"]
    repeat = asyncio.run(tools[0].coroutine(s.record.file_id, s.request.instruction, s.runtime))
    assert repeat["workspace"] == first["workspace"]
    assert (bindings.path.read_bytes(), bindings.path.stat().st_mtime_ns) == before
    assert bindings.path.stat().st_mode & 0o777 == 0o600
    assert s.events == ["create", "execute", "destroy"]


def test_two_attempts_share_file_without_mutating_metadata(remote):
    s = remote
    now = datetime.now(UTC).replace(hour=10)
    bindings = WorkspaceBindings(s.runner, s.files)
    one = successful(s)
    first = bindings.publish(s.scope, s.request, one, now=now)
    request2 = replace(s.request, request_id="second-approved")
    two = successful(s, request2)
    second = bindings.publish(s.scope, request2, two, now=now + timedelta(minutes=1))
    assert first.to_dict() == second.to_dict()
    a, b = association(first, s.request, one), association(second, request2, two)
    assert a["file_ref"] == b["file_ref"] and a["artifact_ref"] != b["artifact_ref"]
    assert a["attempt"] != b["attempt"] and a["request_id"] != b["request_id"]
    assert bindings.read(s.scope, s.request, one, now=now + timedelta(days=1)).to_dict() == first.to_dict()
    with sqlite3.connect(bindings.path) as db:
        assert db.execute("SELECT count(*) FROM bindings WHERE status='ready'").fetchone()[0] == 2


def test_legacy_success_has_no_backfilled_mapping(remote, monkeypatch):
    s = remote
    successful(s)
    bindings = WorkspaceBindings(s.runner, s.files)
    monkeypatch.setattr(s.files, "store_bytes", lambda **kw: pytest.fail("legacy backfill"))
    tools = remote_csv_tools(grant_for=lambda _: s.grant, file_store=s.files, runner=s.runner)
    result = asyncio.run(tools[1].coroutine(s.runtime))
    assert result["state"] == "workspace_pending" and result["execution_state"] == "succeeded"
    assert not bindings.path.exists()
    assert s.events == [] and s.calls == []


@pytest.mark.parametrize("damage", ["expired", "missing", "bytes", "metadata", "symlink", "fifo"])
def test_read_rejects_damage_without_regenerating(remote, monkeypatch, damage, tmp_path):
    s = remote
    now = datetime.now(UTC)
    bindings = WorkspaceBindings(s.runner, s.files)
    outcome = successful(s)
    record = bindings.publish(s.scope, s.request, outcome, now=now)
    original = s.files.original_path(record)
    if damage == "expired":
        now = datetime.fromisoformat(record.expires_at)
    elif damage == "missing":
        original.unlink()  # Test-owned temporary fixture only.
    elif damage == "bytes":
        original.write_bytes(b"tampered")
    elif damage == "metadata":
        s.files.save_record(replace(record, expires_at=(now + timedelta(days=99)).isoformat()))
    elif damage == "fifo":
        original.unlink()
        os.mkfifo(original)
    else:
        target = tmp_path / "target"
        target.write_bytes(s.output)
        original.unlink()
        original.symlink_to(target)
    snapshot = bindings.path.read_bytes()
    monkeypatch.setattr(s.files, "store_bytes", lambda **kw: pytest.fail("no regeneration"))
    with pytest.raises((ValueError, OSError)):
        bindings.read(s.scope, s.request, outcome, now=now)
    assert bindings.path.read_bytes() == snapshot


def test_existing_expired_file_is_not_renewed_by_new_task(remote, monkeypatch):
    s = remote
    bindings = WorkspaceBindings(s.runner, s.files)
    now = datetime.now(UTC)
    old = s.files.store_bytes(scope=FileScope(*s.scope),
        filename="summary.csv", direction="outbound", data=s.output, now=now)
    old.expires_at = (now - timedelta(seconds=1)).isoformat()
    s.files.save_record(old)
    outcome = successful(s)
    monkeypatch.setattr(s.files, "store_bytes", lambda **kw: pytest.fail("expired file overwrite"))
    with pytest.raises(ValueError):
        bindings.publish(s.scope, s.request, outcome, now=now)
    assert s.files.load_record(old.relative_dir).to_dict() == old.to_dict()


def test_interrupted_publication_retains_pending_no_retry(remote, monkeypatch):
    s = remote
    outcome = successful(s)
    bindings = WorkspaceBindings(s.runner, s.files)
    calls = []
    def fail(**kwargs):
        calls.append("write")
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(s.files, "store_bytes", fail)
    with pytest.raises(OSError):
        bindings.publish(s.scope, s.request, outcome)
    with pytest.raises(ValueError):
        bindings.publish(s.scope, s.request, outcome)
    with pytest.raises(ValueError):
        bindings.read(s.scope, s.request, outcome)
    with sqlite3.connect(bindings.path) as db:
        assert db.execute("SELECT status,record FROM bindings").fetchall() == [("pending", None)]
    assert calls == ["write"]


def test_parallel_attempts_publish_once_to_shared_file(remote, monkeypatch):
    s = remote
    now = datetime.now(UTC)
    requests = [replace(s.request, request_id=f"approved-{i}") for i in range(8)]
    outcomes = [successful(s, r) for r in requests]
    original, calls = s.files.store_bytes, []
    def save(**kwargs):
        calls.append("write")
        return original(**kwargs)
    monkeypatch.setattr(s.files, "store_bytes", save)
    def publish(pair):
        request, outcome = pair
        return WorkspaceBindings(s.runner, s.files).publish(s.scope, request, outcome, now=now)
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(publish, zip(requests, outcomes, strict=True)))
    assert calls == ["write"]
    assert all(r.to_dict() == records[0].to_dict() for r in records)


def test_publication_to_delivery_keeps_file_dedup_and_explicit_resend(case, tmp_path):
    _, delivery, calls, now = case
    directory = tmp_path / "gateway"
    directory.mkdir(mode=0o700)
    store = BusinessStore(directory.resolve())
    scope = delivery.scope
    request = BusinessRequest("one", scoped_owner(scope), "input", "sum")
    s = SimpleNamespace(gateway=store, request=request, output=b"group,total\nA,1\n")
    bindings = WorkspaceBindings(SimpleNamespace(store=store), delivery.store)
    one = successful(s)
    record = bindings.publish(scope, request, one, now=now)
    request2 = replace(request, request_id="two")
    two = successful(s, request2)
    second = bindings.publish(scope, request2, two, now=now + timedelta(minutes=1))
    async def run():
        first = await delivery.deliver(file_ref(record))
        duplicate = await replace(delivery, message_id="new-task-request").deliver(file_ref(second))
        assert duplicate == dict(first, reused_receipt=True, delivery_notice="已有投递记录，本次未再次发送。")
        assert calls == ["upload", "send"]
        resent = await replace(delivery, message_id="explicit-resend", user_text="把 summary.csv 再发给我").deliver(file_ref(second))
        assert resent["delivery_id"] != first["delivery_id"] and not resent["reused_receipt"]
        assert calls == ["upload", "send", "upload", "send"]
    asyncio.run(run())


def test_concurrent_same_attempt_never_overwrites_or_duplicates(remote, monkeypatch):
    s = remote
    outcome = successful(s)
    bindings = WorkspaceBindings(s.runner, s.files)
    original, calls = s.files.store_bytes, []
    def save(**kwargs):
        calls.append("write")
        return original(**kwargs)
    monkeypatch.setattr(s.files, "store_bytes", save)
    def publish(_):
        try:
            return bindings.publish(s.scope, s.request, outcome)
        except ValueError:
            return None  # Concurrent readers may observe committed pending; never retry writes.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, range(16)))
    assert calls == ["write"] and any(r is not None for r in results)
    with sqlite3.connect(bindings.path) as db:
        assert db.execute("SELECT count(*) FROM bindings WHERE status='ready'").fetchone()[0] == 1


@pytest.mark.parametrize("damage", ["db_permissions", "db_symlink", "parent_symlink"])
def test_private_storage_and_path_guards(remote, damage, tmp_path):
    s = remote
    bindings = WorkspaceBindings(s.runner, s.files)
    outcome = successful(s)
    if damage == "parent_symlink":
        (s.files.root_dir / s.scope[0]).rename(s.files.root_dir / "original-tenant")
        (s.files.root_dir / s.scope[0]).symlink_to(s.files.root_dir / "original-tenant")
        with pytest.raises(ValueError):
            bindings.publish(s.scope, s.request, outcome)
        assert not list(s.files.root_dir.rglob("outbound"))
        return
    bindings.publish(s.scope, s.request, outcome)
    if damage == "db_permissions":
        bindings.path.chmod(0o644)
    else:
        target = tmp_path / "original.sqlite"
        bindings.path.rename(target)
        bindings.path.symlink_to(target)
    with pytest.raises(ValueError):
        bindings.read(s.scope, s.request, outcome)


@pytest.mark.parametrize("mismatch", ["scope", "request", "artifact", "identity"])
def test_binding_identity_tamper_rejected(remote, mismatch):
    s = remote
    bindings = WorkspaceBindings(s.runner, s.files)
    outcome = successful(s)
    bindings.publish(s.scope, s.request, outcome)
    scope, request = s.scope, s.request
    if mismatch == "scope":
        scope = (s.scope[0], "other-user", s.scope[2])
    elif mismatch == "request":
        request = replace(request, instruction="different")
    elif mismatch == "artifact":
        outcome = replace(outcome, artifact_ref="other-artifact")
    else:
        with sqlite3.connect(bindings.path) as db:
            db.execute("UPDATE bindings SET identity=?", (json.dumps({"forged": True}),))
    with pytest.raises(ValueError):
        bindings.read(scope, request, outcome)
