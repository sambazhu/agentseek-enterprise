from dataclasses import replace

import pytest
from agentseek_execution import m3_target_tracking as module
from agentseek_execution.models import ContractError
from test_m3_create_reconciliation import setup as reconciliation_setup  # noqa: F401
from test_m3_platform_evidence import client_mock
from test_m3_platform_evidence import fixture as platform_fixture  # noqa: F401


def test_empty_before_discovery_stays_unresolved_then_exact_history_survives_disappearance(request, monkeypatch):
    reader, info, fence, binding, pending = request.getfixturevalue("reconciliation_setup")
    directory = pending.parent
    with monkeypatch.context() as patch:
        client_mock(patch, info, items=[])
        assert module.track_pending(reader, fence, binding, directory).state == "empty_unresolved"
    assert not (directory / "create.target").exists()
    info["state"] = "running"
    with monkeypatch.context() as patch:
        client_mock(patch, info)
        assert module.track_pending(reader, fence, binding, directory).sandbox_id == "vm"
    record = (directory / "create.target").read_bytes()
    with monkeypatch.context() as patch:
        client_mock(patch, info, items=[])
        outcome = module.track_pending(reader, fence, binding, directory)
    assert outcome.state == "known_target_absent" and outcome.sandbox_id == "vm"
    assert not outcome.fence_release_allowed and not outcome.node_termination_proven
    assert (directory / "create.target").read_bytes() == record
    with pytest.raises(ContractError):
        fence.claim(replace(binding, create_token="next"))


@pytest.mark.parametrize("change", ["corrupt", "removed_during_read", "other_target"])
def test_target_history_cannot_be_replaced_or_lost(request, monkeypatch, change):
    reader, info, fence, binding, pending = request.getfixturevalue("reconciliation_setup")
    directory = pending.parent
    info["state"] = "running"
    client_mock(monkeypatch, info)
    module.track_pending(reader, fence, binding, directory)
    path = directory / "create.target"
    if change == "corrupt":
        path.write_text("{}")
    elif change == "other_target":
        original = module.observe_pending
        monkeypatch.setattr(module, "observe_pending", lambda *a: replace(original(*a), sandbox_id="other"))
    else:
        original = module.observe_pending
        def observe(*args):
            outcome = original(*args)
            path.unlink()
            return outcome
        monkeypatch.setattr(module, "observe_pending", observe)
    with pytest.raises(ContractError):
        module.track_pending(reader, fence, binding, directory)
    assert pending.exists()


@pytest.mark.parametrize("stage", ["before_write", "after_write"])
def test_expired_observation_never_returns_fresh_target(request, monkeypatch, stage):
    reader, info, fence, binding, pending = request.getfixturevalue("reconciliation_setup")
    info["state"] = "running"
    client_mock(monkeypatch, info)
    observe = module.observe_pending
    captured = []
    def observed(*args):
        result = observe(*args)
        captured.append(result.observed_mono)
        if stage == "before_write":
            return replace(result, observed_mono=result.observed_mono - 3)
        return result
    monkeypatch.setattr(module, "observe_pending", observed)
    if stage == "after_write":
        original = module._write
        def write(*args):
            original(*args)
            # Substitute only this module's clock, without sleeping or changing
            # the shared time module used by the HTTP fixture.
            from types import SimpleNamespace
            monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: captured[0] + 3))
        monkeypatch.setattr(module, "_write", write)
    with pytest.raises(ContractError):
        module.track_pending(reader, fence, binding, pending.parent)
    assert (pending.parent / "create.target").exists() == (stage == "after_write")
    assert pending.exists()


@pytest.mark.parametrize("same", [True, False])
def test_competing_target_record_is_never_overwritten(request, same):
    import hashlib
    from dataclasses import asdict

    from agentseek_execution.models import canonical

    _, _, _, binding, pending = request.getfixturevalue("reconciliation_setup")
    digest = hashlib.sha256(canonical(asdict(binding)).encode()).hexdigest()
    module._write(pending.parent, digest, "vm")
    before = (pending.parent / "create.target").read_bytes()
    if same:
        module._write(pending.parent, digest, "vm")
    else:
        with pytest.raises(ContractError):
            module._write(pending.parent, digest, "other")
    assert (pending.parent / "create.target").read_bytes() == before
