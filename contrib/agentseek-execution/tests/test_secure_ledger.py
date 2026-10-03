import os
import sqlite3
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest
from agentseek_execution.fakes import FakeAuthority, FakeContentStore, FakeProvider
from agentseek_execution.ledger import Ledger
from agentseek_execution.models import ContractError, Execution, FileEntry, InputManifest, OutputManifest, Scope, Task
from agentseek_execution.secure_ledger import SecureLedger
from agentseek_execution.service import Service


def execution():
    return Execution(
        uuid4().hex,
        Task(uuid4().hex, Scope("private-tenant", "private-de", "private-chat", "private-user")),
        InputManifest(),
        "template",
        "policy",
    )


def test_encrypted_commit_restart_and_no_plaintext(tmp_path):
    key = os.urandom(32)
    directory = tmp_path / "ledger"
    ledger = SecureLedger(directory, key)
    item = execution()
    authority = FakeAuthority(tasks={item.task})
    provider = FakeProvider()
    content = FakeContentStore()
    service = Service(ledger, authority, provider, content)
    lease = service.start(item.task.scope, item, "private-worker", 100, 30)
    provider.finished.add(lease.create_token)
    entry = FileEntry("private-file", "version", "private-report.txt", sha256(b"hello").hexdigest(), 5, "text/plain")
    manifest = OutputManifest((entry,))
    result = service.submit(item.task.scope, item, lease, manifest, {entry.path: b"hello"}, "private-commit", 101)
    ledger.close()
    for path in directory.iterdir():
        raw = path.read_bytes()
        for secret in (
            "private-worker",
            "private-file",
            "private-report",
            "private-user",
            "private-commit",
            lease.create_token,
        ):
            assert secret.encode() not in raw
        assert key not in raw
    reopened = SecureLedger(directory, key)
    try:
        service.ledger = reopened
        assert (
            service.submit(item.task.scope, item, lease, manifest, {entry.path: b"hello"}, "private-commit", 999)
            == result
        )
        with pytest.raises(ContractError):
            service.submit(
                item.task.scope,
                item,
                replace(lease, owner="other"),
                manifest,
                {entry.path: b"hello"},
                "private-commit",
                999,
            )
    finally:
        reopened.close()


def test_wrong_key_and_plaintext_database_rejected(tmp_path):
    key = os.urandom(32)
    directory = tmp_path / "ledger"
    ledger = SecureLedger(directory, key)
    ledger.close()
    with pytest.raises(ContractError):
        SecureLedger(directory, os.urandom(32))
    reopened = SecureLedger(directory, key)
    reopened.close()
    other = tmp_path / "plain"
    other.mkdir(mode=0o700)
    plain = Ledger(other / "execution.sqlite")
    plain.begin(execution(), "worker", 100, 10)
    plain.close()
    (other / "execution.sqlite").chmod(0o600)
    with pytest.raises(ContractError):
        SecureLedger(other, key)


def test_attempt_history_encrypted_and_expiry_blocks_new_writer(tmp_path):
    ledger = SecureLedger(tmp_path / "ledger", os.urandom(32))
    item = execution()
    try:
        lease = ledger.begin(item, "private-owner", 100, 10)
        with pytest.raises(ContractError):
            ledger.begin(replace(item, execution_id=uuid4().hex), "other", 200, 10)
        ledger.reconcile_stopped(lease, confirmed=True)
        renewed = ledger.begin(item, "new-owner", 201, 10)
        assert renewed.attempt == 2
        snapshot = ledger.db.execute("SELECT snapshot FROM attempt_history").fetchone()[0]
        assert "private-owner" not in snapshot and not snapshot.startswith("{")
        with pytest.raises(ContractError):
            ledger.transition(lease, "running", 202)
    finally:
        ledger.close()


def test_owner_ciphertext_cannot_be_moved_between_executions(tmp_path):
    ledger = SecureLedger(tmp_path / "ledger", os.urandom(32))
    try:
        a, b = execution(), execution()
        ledger.begin(a, "owner", 100, 10)
        lease = ledger.begin(b, "owner", 100, 10)
        cipher = ledger.db.execute("SELECT owner FROM executions WHERE id=?", (a.execution_id,)).fetchone()[0]
        ledger.db.execute("UPDATE executions SET owner=? WHERE id=?", (cipher, b.execution_id))
        with pytest.raises(ContractError):
            ledger.transition(lease, "running", 101)
    finally:
        ledger.close()


def test_encrypted_commit_rolls_back_and_replays(tmp_path):
    ledger = SecureLedger(tmp_path / "ledger", os.urandom(32))
    item = execution()
    try:
        lease = ledger.begin(item, "owner", 100, 10)
        for state in ("running", "exporting", "validating", "committing"):
            ledger.transition(lease, state, 101)
        ledger.db.execute("""CREATE TRIGGER fail_publish BEFORE UPDATE OF revision ON tasks
            BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        with pytest.raises(sqlite3.IntegrityError):
            ledger.commit(lease, OutputManifest(), "private-commit", 102)
        assert ledger.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert ledger.state(item.execution_id) == "committing"
        ledger.db.execute("DROP TRIGGER fail_publish")
        result = ledger.commit(lease, OutputManifest(), "private-commit", 102)
        assert ledger.commit(lease, OutputManifest(), "private-commit", 999) == result
    finally:
        ledger.close()
