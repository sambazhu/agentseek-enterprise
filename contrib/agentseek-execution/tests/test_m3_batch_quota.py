from dataclasses import replace

import pytest
from agentseek_execution.m3_batch_quota import BatchQuota
from agentseek_execution.m3_create_worker import CreatePlan
from agentseek_execution.models import ContractError


@pytest.fixture
def setup(tmp_path):
    root = tmp_path.resolve() / "quota"
    root.mkdir(mode=0o700)
    a = CreatePlan("run", "a", "tpl", "boot", "a" * 64, "b" * 64, "example.invalid", True, "https://example.invalid")
    return root, (a, replace(a, create_token="b"))


def test_sequential_slots_persist_across_reopen(setup):
    root, plans = setup
    BatchQuota(root, plans).reserve(plans[0])
    with pytest.raises(ContractError):
        BatchQuota(root, plans).reserve(plans[0])
    BatchQuota(root, plans).reserve(plans[1])
    with pytest.raises(ContractError):
        BatchQuota(root, plans).reserve(plans[1])
    assert len(list(root.iterdir())) == 2


@pytest.mark.parametrize("mode", ["skip", "partial", "changed_sequence", "future"])
def test_invalid_history_blocks_reservation(setup, mode):
    root, plans = setup
    if mode == "partial":
        (root / "create-slot-0").write_bytes(b"{")
        (root / "create-slot-0").chmod(0o600)
    elif mode == "changed_sequence":
        BatchQuota(root, plans).reserve(plans[0])
        plans = (replace(plans[0], create_token="new"), plans[1])
    elif mode == "future":
        (root / "create-slot-3").touch(mode=0o600)
    with pytest.raises(ContractError):
        BatchQuota(root, plans).reserve(plans[1] if mode != "future" else plans[0])


def test_failed_fsync_never_refunds_slot(setup, monkeypatch):
    from agentseek_execution import m3_batch_quota as module
    root, plans = setup
    def fail(fd):
        raise OSError("simulated")
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fsync", fail)
        with pytest.raises(ContractError):
            BatchQuota(root, plans).reserve(plans[0])
    assert (root / "create-slot-0").exists()
    with pytest.raises(ContractError):
        BatchQuota(root, plans).reserve(plans[0])
