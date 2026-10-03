import hashlib
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from agentseek_execution import m3_create_fence as module
from agentseek_execution import m3_successor
from agentseek_execution.m3_closeout_evidence import CloseoutEvidence
from agentseek_execution.m3_create_worker import CreatePlan
from agentseek_execution.models import ContractError, canonical


@pytest.mark.parametrize("mode", ["success", "unknown", "stale", "revoked", "write", "replace", "final_sync"])
def test_handoff_preserves_fence_and_never_repeats(tmp_path, monkeypatch, mode):  # noqa: C901 -- fault matrix
    root = tmp_path.resolve()
    root.chmod(0o700)
    a = CreatePlan("run", "a", "tpl", "boot", "a" * 64, "b" * 64,
                   "example.invalid", True, "https://example.invalid")
    b = replace(a, create_token="b")
    old = a.binding("c" * 64)
    previous = replace(old, intent_sha256="d" * 64)
    fence = module.CreateFence(root)
    fence.claim(old)
    original = (root / "create.pending").read_bytes()
    receipt = SimpleNamespace(sandbox_id="vm")

    def read(binding):
        if mode == "unknown":
            raise OSError
        return receipt

    vault = SimpleNamespace(read=read, read_intent=lambda binding: {})
    calls = 0

    def approve(*args, **kwargs):
        nonlocal calls
        calls += 1
        if mode == "revoked" and calls == 3:
            raise OSError
        return time.monotonic() + 60

    monkeypatch.setattr(m3_successor, "read_create_approval", approve)
    digest = hashlib.sha256(canonical(asdict(old)).encode()).hexdigest()

    def closeout():
        return CloseoutEvidence("vm", "known_target_absent",
                                time.monotonic() - (3 if mode == "stale" else 0), 42, 1, digest)

    kwargs = {"approval_digest": "e" * 64, "sequence": (a, b), "collect_closeout": closeout}
    original_sync = module.os.fsync
    syncs = 0

    def sync(fd):
        nonlocal syncs
        syncs += 1
        if syncs == 4:
            raise OSError
        original_sync(fd)

    def fail(*args, **kwargs):
        raise OSError

    with monkeypatch.context() as patch:
        if mode == "write":
            patch.setattr(module.os, "write", lambda *args: 0)
        elif mode == "replace":
            patch.setattr(module.os, "replace", fail)
        elif mode == "final_sync":
            patch.setattr(module.os, "fsync", sync)
        if mode == "success":
            assert fence.claim_successor(previous, vault, b, None, **kwargs) == b.binding("e" * 64)
        else:
            with pytest.raises(ContractError):
                fence.claim_successor(previous, vault, b, None, **kwargs)
    assert (root / "create.pending").exists()
    if mode in {"success", "final_sync"}:
        fence.verify_pending(b.binding("e" * 64))
        assert next(root.glob("create.closed-*")).read_bytes() == original
    else:
        assert (root / "create.pending").read_bytes() == original
    if mode not in {"unknown", "stale"}:
        with pytest.raises(ContractError):
            fence.claim_successor(previous, vault, b, None, **kwargs)
    with pytest.raises(ContractError):
        fence.claim(b.binding("e" * 64))
