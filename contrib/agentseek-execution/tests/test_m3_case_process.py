import json
from pathlib import Path

import pytest
from agentseek_execution import m3_case_process as module
from agentseek_execution.m3_auth_probe import Observation
from agentseek_execution.models import Code, ContractError


def result():
    return {"status": None, "body_size": 0, "body_sha256": None, "complete": False,
            "reason": "transport_error", "protocol": None}


@pytest.mark.parametrize("deadline,budget", [(200, 15), (116, 15)])
def test_parent_has_single_total_budget_and_no_environment(monkeypatch, deadline, budget):
    calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    def worker(command, raw, **kwargs):
        calls.append((command, json.loads(raw), kwargs))
        return json.dumps(result()).encode()
    monkeypatch.setattr(module, "run_worker", worker)
    assert module.dispatch_isolated(Path("/installation"), Path("/records"), installation_digest="a" * 64,
                                    approval_digest="b" * 64, deadline=deadline) == result()
    command, payload, options = calls[0]
    assert command[-1] == "agentseek_execution.m3_case_process"
    assert options == {"budget": budget, "environment": {}}
    assert payload["deadline"] == 100 + budget
    assert set(payload) == {"installation", "directory", "installation_digest", "approval_digest", "deadline"}


@pytest.mark.parametrize("deadline", [111, 112, 115.999, float("nan"), float("inf"), True])
def test_insufficient_or_invalid_parent_deadline_never_launches(monkeypatch, deadline):
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    monkeypatch.setattr(module, "run_worker", lambda *a, **kw: pytest.fail("must not launch"))
    with pytest.raises(ContractError):
        module.dispatch_isolated(Path("/installation"), Path("/records"), installation_digest="a" * 64,
                                 approval_digest="b" * 64, deadline=deadline)


def test_timeout_propagates_unknown_without_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    def worker(*args, **kwargs):
        calls.append(1)
        raise ContractError(Code.UNKNOWN)
    monkeypatch.setattr(module, "run_worker", worker)
    with pytest.raises(ContractError):
        module.dispatch_isolated(Path("/installation"), Path("/records"), installation_digest="a" * 64,
                                 approval_digest="b" * 64, deadline=200)
    assert calls == [1]


def test_next_row_cannot_refresh_the_shared_cutoff(monkeypatch):
    from types import SimpleNamespace

    clock = [100.0]
    calls = []
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def worker(*args, **kwargs):
        calls.append(kwargs["budget"])
        clock[0] += 16
        return json.dumps(result()).encode()
    monkeypatch.setattr(module, "run_worker", worker)
    options = dict(installation_digest="a" * 64, approval_digest="b" * 64, deadline=131)
    module.dispatch_isolated(Path("/installation"), Path("/records"), **options)
    with pytest.raises(ContractError):
        module.dispatch_isolated(Path("/installation"), Path("/records"), **options)
    assert calls == [15]


@pytest.mark.parametrize("remaining", [10.99, 15.01, 30])
def test_child_rejects_insufficient_or_legacy_budget_before_reading(monkeypatch, remaining):
    monkeypatch.setattr(module.os, "getuid", lambda: 0)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    monkeypatch.setattr(module, "config_bytes", lambda *a: pytest.fail("must not read"))
    with pytest.raises(ContractError):
        module.perform(dict(installation="/installation", directory="/records",
                            installation_digest="a" * 64, approval_digest="b" * 64,
                            deadline=100 + remaining))


@pytest.mark.parametrize("remaining", [9, 20])
def test_inline_request_rechecks_budget_after_preparation(monkeypatch, remaining):
    calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    monkeypatch.setattr(module, "prepare_receipted", lambda *a, **kw: "request")
    def observe(request, **kwargs):
        calls.append(kwargs)
        return Observation(None, 0, None, False, "transport_error")
    monkeypatch.setattr(module, "observe", observe)
    if remaining < 10:
        with pytest.raises(ContractError):
            module._inline(Path("/config"), binding=None, source=None, verified_deadline=100 + remaining)
        assert not calls
    else:
        assert module._inline(Path("/config"), binding=None, source=None, verified_deadline=120) == result()
        assert calls == [{"remaining_seconds": 10}]
