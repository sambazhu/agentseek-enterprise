import hashlib
import json
from dataclasses import replace

import pytest
from agentseek_execution import m3_create_receipt as module
from agentseek_execution.models import ContractError


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path.resolve() / "vault"
    root.mkdir(mode=0o700)
    intent = tmp_path.resolve() / "intent"
    # private intent is held separately from the encrypted vault
    intent.parent.chmod(0o700)
    intent.write_text(
        json.dumps({
            "schema": 1,
            "run_id": "run",
            "create_token": "create",
            "boot_id": "boot",
            "sent_epoch": 1000,
            "sent_mono": 100,
        })
    )
    intent.chmod(0o600)
    binding = module.CreateBinding(
        "run",
        "create",
        "tpl",
        "boot",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        hashlib.sha256(intent.read_bytes()).hexdigest(),
        "example.invalid",
        True,
    )
    response = {
        "sandboxID": "vm",
        "templateID": "tpl",
        "domain": "example.invalid",
        "trafficAccessToken": "synthetic-traffic-secret",
        "envdAccessToken": None,
        "unrelated": "synthetic-unrelated-secret",
    }
    return root, intent, binding, response


def test_sealed_roundtrip_after_reopen(fixture):
    root, intent, binding, response = fixture
    vault = module.CreateReceiptVault(root, b"k" * 32)
    vault.reserve(binding, intent)
    raw = json.dumps(response).encode()
    vault.seal_response(binding, raw)
    restored = module.CreateReceiptVault(root, b"k" * 32).read(binding)
    assert restored.traffic_token == response["trafficAccessToken"]
    assert restored.response_sha256 == hashlib.sha256(raw).hexdigest()
    assert restored.envd_state == "null" and "synthetic" not in repr(restored)
    for path in root.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert b"synthetic" not in path.read_bytes()
    with pytest.raises(FileExistsError):
        vault.reserve(binding, intent)
    with pytest.raises(FileExistsError):
        vault.seal_response(binding, raw)
    # A different key must not reopen the same create slot.
    with pytest.raises(FileExistsError):
        module.CreateReceiptVault(root, b"x" * 32).reserve(binding, intent)


def test_no_response_before_reservation(fixture):
    root, _, binding, response = fixture
    with pytest.raises(ContractError):
        module.CreateReceiptVault(root, b"k" * 32).seal_response(binding, json.dumps(response).encode())
    assert not list(root.iterdir())


@pytest.mark.parametrize(
    "field,value",
    [
        ("templateID", "other"),
        ("domain", "other.invalid"),
        ("trafficAccessToken", None),
        ("envdAccessToken", "unexpected"),
        ("sandboxID", "../../bad"),
        ("trafficAccessToken", "bad\nheader"),
    ],
)
def test_invalid_response_never_sealed(fixture, field, value):
    root, intent, binding, response = fixture
    vault = module.CreateReceiptVault(root, b"k" * 32)
    vault.reserve(binding, intent)
    response[field] = value
    with pytest.raises(ContractError):
        vault.seal_response(binding, json.dumps(response).encode())
    assert len(list(root.iterdir())) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "other"),
        ("create_token", "other"),
        ("template_id", "other"),
        ("approval_sha256", "d" * 64),
        ("candidate_sha256", "d" * 64),
        ("request_sha256", "d" * 64),
        ("boot_id", "other"),
        ("restricted", False),
    ],
)
def test_binding_changes_cannot_read_receipt(fixture, field, value):
    root, intent, binding, response = fixture
    vault = module.CreateReceiptVault(root, b"k" * 32)
    vault.reserve(binding, intent)
    vault.seal_response(binding, json.dumps(response).encode())
    with pytest.raises(ContractError):
        vault.read(replace(binding, **{field: value}))


@pytest.mark.parametrize("mode", ["wrong_key", "tamper", "truncate", "missing_intent", "symlink"])
def test_corruption_or_missing_provenance_blocks(fixture, mode):
    root, intent, binding, response = fixture
    vault = module.CreateReceiptVault(root, b"k" * 32)
    vault.reserve(binding, intent)
    vault.seal_response(binding, json.dumps(response).encode())
    path = next(root.glob("*.receipt"))
    if mode == "wrong_key":
        vault = module.CreateReceiptVault(root, b"x" * 32)
    elif mode == "missing_intent":
        next(root.glob("*.intent")).unlink()
    elif mode == "symlink":
        path.unlink()
        path.symlink_to(intent)
    elif mode == "truncate":
        path.write_bytes(b"short")
    else:
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 1
        path.write_bytes(raw)
    with pytest.raises(ContractError):
        vault.read(binding)


def test_public_absent_tokens_recorded_not_fabricated(fixture):
    root, intent, binding, response = fixture
    binding = replace(binding, restricted=False)
    response.pop("envdAccessToken")
    response.pop("trafficAccessToken")
    vault = module.CreateReceiptVault(root, b"k" * 32)
    vault.reserve(binding, intent)
    vault.seal_response(binding, json.dumps(response).encode())
    restored = vault.read(binding)
    assert restored.traffic_token is None and restored.envd_state == "absent"


def test_failed_reservation_burns_slot(fixture, monkeypatch):
    root, intent, binding, _ = fixture
    vault = module.CreateReceiptVault(root, b"k" * 32)

    def fail(_):
        raise OSError

    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(OSError):
        vault.reserve(binding, intent)
    with pytest.raises(FileExistsError):
        vault.reserve(binding, intent)
    assert len(list(root.iterdir())) == 1
