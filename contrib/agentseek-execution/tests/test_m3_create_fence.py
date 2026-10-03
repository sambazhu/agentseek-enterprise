import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from agentseek_execution import m3_create_fence as module
from agentseek_execution.m3_create_receipt import CreateBinding
from agentseek_execution.models import ContractError


@pytest.fixture
def setup(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    binding = CreateBinding(
        "run", "secret-create-token", "tpl", "boot", *(["a" * 64] * 3), "0" * 64, "example.invalid", True
    )
    return root, binding


def test_claim_is_private_durable_and_never_reopened_for_new_identity(setup):
    root, binding = setup
    module.CreateFence(root).claim(binding)
    path = root / "create.pending"
    raw = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert set(json.loads(raw)) == {"schema", "state", "binding_sha256"}
    assert b"secret-create-token" not in raw and b"example.invalid" not in raw
    for candidate in (
        binding,
        replace(binding, create_token="new"),
        replace(binding, run_id="new"),
        replace(binding, approval_sha256="b" * 64),
    ):
        with pytest.raises(ContractError):
            module.CreateFence(root).claim(candidate)
    assert path.read_bytes() == raw


def test_competing_creators_have_one_winner(setup):
    root, binding = setup

    def claim(index):
        try:
            module.CreateFence(root).claim(replace(binding, create_token=f"token-{index}"))
        except ContractError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, range(16))) == 1


@pytest.mark.parametrize("mode", ["write", "file_sync", "directory_sync"])
def test_partial_failure_burns_fence(setup, monkeypatch, mode):
    root, binding = setup
    original = os.fsync
    calls = 0

    def sync(fd):
        nonlocal calls
        calls += 1
        if calls == (1 if mode == "file_sync" else 2):
            raise OSError
        original(fd)

    with monkeypatch.context() as patch:
        if mode == "write":
            patch.setattr(module.os, "write", lambda *args: 0)
        else:
            patch.setattr(module.os, "fsync", sync)
        with pytest.raises(ContractError):
            module.CreateFence(root).claim(binding)
    assert (root / "create.pending").exists()
    with pytest.raises(ContractError):
        module.CreateFence(root).claim(replace(binding, create_token="replacement"))


@pytest.mark.parametrize("mode", ["empty", "symlink", "directory"])
def test_any_existing_marker_blocks_without_reading_or_repair(setup, mode):
    root, binding = setup
    path = root / "create.pending"
    if mode == "empty":
        path.touch()
    elif mode == "symlink":
        path.symlink_to(root / "missing")
    else:
        path.mkdir()
    with pytest.raises(ContractError):
        module.CreateFence(root).claim(binding)
    assert not (root / "missing").exists()


def test_unsafe_directory_denied_without_provisioning(setup):
    root, binding = setup
    root.chmod(0o755)
    with pytest.raises(ContractError):
        module.CreateFence(root).claim(binding)
    assert not (root / "create.pending").exists()
    missing = root / "absent"
    with pytest.raises(ContractError):
        module.CreateFence(missing).claim(binding)
    assert not missing.exists()


@pytest.mark.parametrize("mode", ["changed", "truncated", "public", "hardlink", "symlink"])
def test_pending_verification_rejects_unsafe_record(setup, mode):
    root, binding = setup
    fence = module.CreateFence(root)
    fence.claim(binding)
    fence.verify_pending(binding)
    path = root / "create.pending"
    if mode == "changed":
        binding = replace(binding, create_token="other")
    elif mode == "truncated":
        path.write_bytes(b"")
    elif mode == "public":
        path.chmod(0o644)
    elif mode == "hardlink":
        os.link(path, root / "linked")
    else:
        path.rename(root / "original")
        path.symlink_to(root / "original")
    with pytest.raises(ContractError):
        fence.verify_pending(binding)
