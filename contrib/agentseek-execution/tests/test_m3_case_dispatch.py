import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agentseek_execution import m3_case_dispatch as module
from agentseek_execution.m3_probe_dispatch import Binding, Evidence
from agentseek_execution.models import Code, ContractError


@pytest.fixture
def setup(monkeypatch, tmp_path):
    binding = Binding("run", "create", "vm", "tpl", "a" * 64, "case", "b" * 64, "approval", "c" * 64, "boot")
    now = time.monotonic()
    approved = SimpleNamespace(binding=binding, approval_deadline_mono=now + 60)
    evidence = Evidence(binding, binding, now, now, now + 120, now + 120, "run", "boot", True, True, True, 1)
    calls = []
    monkeypatch.setattr(module, "load_approved_case", lambda *a, **kw: approved)

    class Directory:
        def dispatch_receipted(self, actual, path, *, source, collect):
            assert actual == binding
            calls.append(collect())
            calls.append("reserved")
            calls.append(collect())
            calls.append("sent")
            return {"test_observation": True}

    kwargs = {
        "directory": Directory(), "config_path": tmp_path / "config", "approval_path": tmp_path / "approval",
        "approval_digest": "d" * 64, "approval_ref": "approval", "case_id": "case", "source": object(),
        "template_pins": object(), "collect": lambda b: evidence,
    }
    return kwargs, approved, evidence, calls


def test_live_deadline_is_capped_by_initial_independent_approval(setup):
    kwargs, approved, evidence, calls = setup
    assert module.dispatch_approved_case(**kwargs) == {"test_observation": True}
    assert calls[-1] == "sent"
    assert calls[0].approval_deadline == calls[2].approval_deadline == approved.approval_deadline_mono
    assert calls[0].observed_mono == evidence.observed_mono


@pytest.mark.parametrize("change", ["stale", "binding", "expired", "capacity", "not_evidence"])
def test_invalid_live_collection_never_reserves_or_sends(setup, change):
    kwargs, _approved, evidence, calls = setup
    alternatives = {
        "stale": replace(evidence, observed_mono=evidence.observed_mono - 3),
        "binding": replace(evidence, receipt=replace(evidence.receipt, sandbox_id="other")),
        "expired": replace(evidence, guest_deadline=time.monotonic() + 1),
        "capacity": replace(evidence, capacity_one=False),
        "not_evidence": {"aggregate_ready": True},
    }
    kwargs["collect"] = lambda b: alternatives[change]
    with pytest.raises(ContractError):
        module.dispatch_approved_case(**kwargs)
    assert calls == []


def test_revocation_after_reservation_never_sends(setup, monkeypatch):
    kwargs, approved, _evidence, calls = setup

    def approval(*args, **kw):
        if "reserved" in calls:
            raise ContractError(Code.DENIED)
        return approved

    monkeypatch.setattr(module, "load_approved_case", approval)
    with pytest.raises(ContractError):
        module.dispatch_approved_case(**kwargs)
    assert calls[-1] == "reserved"
    assert "sent" not in calls


def test_change_during_live_collection_rejected_before_reservation(setup):
    kwargs, approved, evidence, calls = setup

    def collect(binding):
        approved.binding = replace(binding, sandbox_id="other")
        return evidence

    kwargs["collect"] = collect
    with pytest.raises(ContractError):
        module.dispatch_approved_case(**kwargs)
    assert calls == []
