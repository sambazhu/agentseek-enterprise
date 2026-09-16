import hashlib
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from agentseek_execution import m3_successor as module
from agentseek_execution.m3_closeout_evidence import CloseoutEvidence
from agentseek_execution.m3_create_worker import CreatePlan
from agentseek_execution.models import Code, ContractError, canonical


@pytest.mark.parametrize("mode", ["valid", "unknown", "wrong_target", "wrong_binding", "stale", "skip", "revoked"])
def test_successor_requires_adjacent_receipted_closeout(monkeypatch, mode):
    a = CreatePlan("run", "a", "tpl", "boot", "a" * 64, "b" * 64, "example.invalid", True, "https://example.invalid")
    b = replace(a, create_token="b")
    previous = replace(a.binding("c" * 64), intent_sha256="d" * 64)
    receipt = SimpleNamespace(sandbox_id="vm")
    def read(binding):
        if mode == "unknown":
            raise ContractError(Code.UNKNOWN)
        return receipt
    vault = SimpleNamespace(read=read, read_intent=lambda b: {})
    def approve(*args, **kwargs):
        if mode == "revoked":
            raise ContractError(Code.DENIED)
        return time.monotonic() + 60
    monkeypatch.setattr(module, "read_create_approval", approve)
    digest = hashlib.sha256(canonical(asdict(a.binding("c" * 64))).encode()).hexdigest()
    closed = CloseoutEvidence("other" if mode == "wrong_target" else "vm", "known_target_absent",
                              time.monotonic() - (3 if mode == "stale" else 0), 42, 1,
                              "f" * 64 if mode == "wrong_binding" else digest)
    kwargs = {"approval_digest": "e" * 64, "sequence": (a, b), "collect_closeout": lambda: closed}
    if mode == "skip":
        previous = replace(previous, create_token="foreign")
    if mode != "valid":
        with pytest.raises(ContractError):
            module.check_successor(previous, vault, b, None, **kwargs)
    else:
        result = module.check_successor(previous, vault, b, None, **kwargs)
        assert result.remaining_planned_slots == 1
        assert not result.create_allowed and not result.fence_release_allowed
