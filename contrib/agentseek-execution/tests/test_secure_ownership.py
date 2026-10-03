import os
import sqlite3
from dataclasses import replace

import pytest
from agentseek_execution.broker_access import AccessGate, CredentialRegistry, Operation, Principal, Resource
from agentseek_execution.models import ContractError, Scope
from agentseek_execution.secure_ownership import SecureOwnership, load_key


@pytest.fixture
def resource():
    return Resource(
        "resource-private-name",
        "owner-private-name",
        Scope("tenant-private", "de-private", "chat-private", "requester-private"),
        "cube-private-handle",
    )


def test_restart_private_storage_and_immutable_mapping(tmp_path, resource):
    key = os.urandom(32)
    directory = tmp_path / "secure"
    store = SecureOwnership(directory, key)
    store.register(resource)
    store.register(resource)
    with pytest.raises(ContractError):
        store.register(replace(resource, backend_handle="replacement"))
    store.close()
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "ownership.sqlite").stat().st_mode & 0o777 == 0o600
    for path in directory.iterdir():
        raw = path.read_bytes()
        for marker in (resource.resource_id, resource.principal_id, resource.backend_handle, resource.scope.tenant):
            assert marker.encode() not in raw
        assert key not in raw
    reopened = SecureOwnership(directory, key)
    try:
        assert reopened.get(resource.resource_id) == resource
        assert reopened.owned(resource.principal_id) == (resource,)
        assert reopened.owned("other") == ()
    finally:
        reopened.close()


def test_wrong_key_fails_and_original_key_still_works(tmp_path, resource):
    key = os.urandom(32)
    directory = tmp_path / "secure"
    store = SecureOwnership(directory, key)
    store.register(resource)
    store.close()
    with pytest.raises(ContractError):
        SecureOwnership(directory, os.urandom(32))
    reopened = SecureOwnership(directory, key)
    assert reopened.get(resource.resource_id) == resource
    reopened.close()


@pytest.mark.parametrize("key", [b"", b"x" * 31, b"x" * 33, None])
def test_invalid_key_does_not_create_directory(tmp_path, key):
    directory = tmp_path / "secure"
    with pytest.raises(ContractError):
        SecureOwnership(directory, key)
    assert not directory.exists()


@pytest.mark.parametrize("attack", ["ciphertext", "owner", "swap"])
def test_tampered_or_swapped_records_fail_closed(tmp_path, resource, attack):
    directory = tmp_path / "secure"
    store = SecureOwnership(directory, os.urandom(32))
    store.register(resource)
    store.register(replace(resource, resource_id="second"))
    db = sqlite3.connect(directory / "ownership.sqlite")
    rows = db.execute("SELECT id,sealed FROM resources ORDER BY id").fetchall()
    if attack == "ciphertext":
        db.execute("UPDATE resources SET sealed=?", (b"broken",))
    elif attack == "owner":
        db.execute("UPDATE resources SET owner='forged'")
    else:
        db.execute("UPDATE resources SET sealed=? WHERE id=?", (rows[0][1], rows[1][0]))
        db.execute("UPDATE resources SET sealed=? WHERE id=?", (rows[1][1], rows[0][0]))
    db.commit()
    db.close()
    try:
        with pytest.raises(ContractError):
            store.get(resource.resource_id)
    finally:
        store.close()


def test_bad_permissions_and_symlink_refused(tmp_path):
    key = os.urandom(32)
    directory = tmp_path / "secure"
    directory.mkdir(mode=0o755)
    with pytest.raises(ContractError):
        SecureOwnership(directory, key)
    directory.chmod(0o700)
    target = tmp_path / "outside"
    target.touch(mode=0o600)
    (directory / "ownership.sqlite").symlink_to(target)
    with pytest.raises(OSError):
        SecureOwnership(directory, key)
    assert target.read_bytes() == b""


def test_key_file_exact_bytes_permissions_and_no_symlink(tmp_path):
    path = tmp_path / "key"
    path.touch(mode=0o600)
    key = os.urandom(32)
    path.write_bytes(key)
    assert load_key(path) == key
    path.chmod(0o644)
    with pytest.raises(ContractError):
        load_key(path)
    path.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        load_key(alias)


def test_access_gate_uses_persistent_ownership(tmp_path, resource):
    store = SecureOwnership(tmp_path / "secure", os.urandom(32))
    store.register(resource)
    credentials = CredentialRegistry()
    key = os.urandom(32).hex()
    credentials.register(key, Principal(resource.principal_id, frozenset({resource.scope})))
    try:
        gate = AccessGate(credentials, store)
        assert gate.resource(key, resource.resource_id, Operation.READ) == resource
        assert gate.list_ids(key) == (resource.resource_id,)
    finally:
        store.close()
