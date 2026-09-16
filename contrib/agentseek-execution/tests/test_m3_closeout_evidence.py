from types import SimpleNamespace

import pytest
from agentseek_execution import m3_closeout_evidence as module
from agentseek_execution.m3_create_receipt import CreateBinding
from agentseek_execution.models import ContractError


@pytest.mark.parametrize("state", ["known_target_absent", "exact_terminal_observed"])
def test_closeout_does_not_claim_supervisor_receipt_or_release(monkeypatch, state):
    snapshot = SimpleNamespace(boot_id="boot", observed_mono=100)
    reads = []
    supervisor = SimpleNamespace(read=lambda **kw: reads.append(kw) or snapshot,
                                 _sample=lambda: SimpleNamespace(monotonic=100))
    monkeypatch.setattr(module, "track_pending", lambda *a: SimpleNamespace(sandbox_id="vm", state=state, observed_mono=100))
    monkeypatch.setattr(module, "verify_identity", lambda *a: SimpleNamespace(pid=42, start_ticks=10))
    binding = CreateBinding("run", "create", "tpl", "boot", *("a" * 64 for _ in range(3)), "0" * 64, "example.invalid", True)
    fence = SimpleNamespace(verify_pending=lambda b: None)
    result = module.collect_closeout(None, fence, binding, None, supervisor, None)
    assert result.state == state and result.sandbox_id == "vm"
    assert not result.supervisor_target_confirmation and not result.fence_release_allowed
    assert len(reads) == 2 and all(item["sandbox_id"] == "vm" for item in reads)


@pytest.mark.parametrize("failure", ["unknown", "running", "reappeared", "restarted", "stale", "boot"])
def test_closeout_rejects_uncertainty(monkeypatch, failure):
    observations = iter(["known_target_absent", "exact_running_observed" if failure == "reappeared" else "known_target_absent"])
    def track(*args):
        state = next(observations)
        if failure == "unknown":
            return SimpleNamespace(sandbox_id=None, state="empty_unresolved", observed_mono=100)
        return SimpleNamespace(sandbox_id="vm", state="exact_running_observed" if failure == "running" else state, observed_mono=100)
    monkeypatch.setattr(module, "track_pending", track)
    starts = iter([10, 11 if failure == "restarted" else 10])
    monkeypatch.setattr(module, "verify_identity", lambda *a: SimpleNamespace(pid=42, start_ticks=next(starts)))
    supervisor = SimpleNamespace(
        read=lambda **kw: SimpleNamespace(boot_id="other" if failure == "boot" else "boot", observed_mono=100),
        _sample=lambda: SimpleNamespace(monotonic=103 if failure == "stale" else 100),
    )
    with pytest.raises(ContractError):
        module.collect_closeout(None, SimpleNamespace(verify_pending=lambda b: None),
                                SimpleNamespace(run_id="run", template_id="tpl", boot_id="boot"), None, supervisor, None)


@pytest.mark.parametrize("mode", ["absent", "unknown", "terminal", "reappeared", "nonempty"])
def test_empty_manifest_requires_known_target_absent_twice(monkeypatch, mode):
    observations = iter([
        "empty_unresolved" if mode == "unknown" else
        "exact_terminal_observed" if mode == "terminal" else "known_target_absent",
        "exact_terminal_observed" if mode == "reappeared" else "known_target_absent",
    ])
    monkeypatch.setattr(module, "track_pending", lambda *a: SimpleNamespace(
        sandbox_id=None if mode == "unknown" else "vm", state=next(observations), observed_mono=100))
    monkeypatch.setattr(module, "verify_identity", lambda *a: SimpleNamespace(pid=42, start_ticks=10))
    reads = []

    def read_empty(**kwargs):
        assert "sandbox_id" not in kwargs
        reads.append(kwargs)
        if mode == "nonempty":
            raise ContractError(module.Code.DENIED)
        return SimpleNamespace(boot_id="boot", observed_mono=100)

    supervisor = SimpleNamespace(read_empty=read_empty, _sample=lambda: SimpleNamespace(monotonic=100))
    binding = CreateBinding("run", "create", "tpl", "boot", *("a" * 64 for _ in range(3)), "0" * 64, "example.invalid", True)
    args = (None, SimpleNamespace(verify_pending=lambda b: None), binding, None, supervisor, None)
    if mode == "absent":
        result = module.collect_closeout(*args, empty_manifest=True)
        assert result.sandbox_id == "vm" and len(reads) == 2
        assert not result.supervisor_target_confirmation
    else:
        with pytest.raises(ContractError):
            module.collect_closeout(*args, empty_manifest=True)
