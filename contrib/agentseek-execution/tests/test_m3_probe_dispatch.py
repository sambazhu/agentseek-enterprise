import hashlib
import json
import multiprocessing
import os
import time
from dataclasses import replace

import pytest
from agentseek_execution import m3_probe_dispatch as module
from agentseek_execution import m3_probe_process as worker
from agentseek_execution.models import ContractError


def observation():
    return {
        "status": 403,
        "body_size": 0,
        "body_sha256": hashlib.sha256(b"").hexdigest(),
        "complete": True,
        "reason": "response",
        "protocol": {"kind": "http_denial_signal", "activity": False, "exit_code": None},
    }


def _claim_then_exit(root, binding):
    fd = module.DispatchDirectory(root)._open()
    module.DispatchDirectory._claim(fd, binding)
    os._exit(23)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "private"
    root.mkdir(mode=0o700)
    config = root / "config.json"
    config.write_text(
        json.dumps({
            "schema": 1,
            "endpoint": "E1",
            "host": "vm.example.invalid",
            "proxy_port": 13080,
            "credentials": {"api_key": "synthetic", "traffic": "traffic", "envd": "envd"},
            "layer": "traffic",
            "state": "correct",
            "replacement": None,
        })
    )
    config.chmod(0o600)
    binding = module.Binding(
        "run",
        "create",
        "vm",
        "tpl",
        "a" * 64,
        "E1-traffic-correct",
        hashlib.sha256(config.read_bytes()).hexdigest(),
        "approval",
        "b" * 64,
        "boot",
    )
    now = time.monotonic()
    evidence = module.Evidence(binding, binding, now, now, now + 120, now + 120, "run", "boot", True, True, True, 1)
    calls = []
    monkeypatch.setattr(module, "observe_isolated", lambda *args, **kwargs: calls.append(kwargs) or observation())
    return root, config, binding, evidence, calls


def test_success_reserved_durably_and_reopen_cannot_repeat(setup):
    root, config, binding, evidence, calls = setup
    assert module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence) == observation()
    assert module.DispatchDirectory(root).inspect(binding) == {"state": "observed", "observation": observation()}
    with pytest.raises(ContractError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    assert len(calls) == 1 and calls[0]["expected_digest"] == binding.config_sha256
    intent = next(root.glob("*.intent"))
    assert intent.stat().st_mode & 0o777 == 0o600
    assert json.loads(intent.read_text())["binding"]["case_id"] == binding.case_id


def test_real_process_exit_after_claim_never_resends(setup):
    root, config, binding, evidence, calls = setup
    child = multiprocessing.get_context("spawn").Process(target=_claim_then_exit, args=(root, binding))
    child.start()
    try:
        child.join(timeout=3)
        assert child.exitcode == 23
        with pytest.raises(ContractError):
            module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
        assert not calls
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=1)


@pytest.mark.parametrize(
    "change",
    [
        {"alarms_clear": False},
        {"running": False},
        {"capacity_one": False},
        {"platform_count": 2},
        {"platform_count": True},
        {"boot_id": "other"},
        {"supervisor_run_id": "other"},
        {"observed_mono": 0},
        {"heartbeat_mono": 0},
        {"approval_deadline": 0},
        {"guest_deadline": float("nan")},
    ],
)
def test_gate_failure_before_claim_or_network(setup, change):
    root, config, binding, evidence, calls = setup
    with pytest.raises(ContractError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: replace(evidence, **change))
    assert not calls and not list(root.glob("*.intent"))


def test_wrong_receipt_binding_rejected(setup):
    root, config, binding, evidence, calls = setup
    with pytest.raises(ContractError):
        module.DispatchDirectory(root).dispatch(
            binding, config, collect=lambda: replace(evidence, receipt=replace(binding, sandbox_id="other"))
        )
    assert not calls


def test_revocation_after_claim_burns_slot(setup):
    root, config, binding, evidence, calls = setup
    snapshots = iter([evidence, replace(evidence, alarms_clear=False)])
    with pytest.raises(ContractError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: next(snapshots))
    with pytest.raises(ContractError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    assert not calls and len(list(root.glob("*.intent"))) == 1


def test_response_lost_no_retry_on_reopen(setup, monkeypatch):
    root, config, binding, evidence, calls = setup

    def lost(*args, **kwargs):
        calls.append(1)
        raise TimeoutError

    monkeypatch.setattr(module, "observe_isolated", lost)
    with pytest.raises(TimeoutError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    with pytest.raises(ContractError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    assert len(calls) == 1


def test_changed_approval_does_not_reset_same_case(setup):
    root, config, binding, evidence, calls = setup
    module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    changed = replace(binding, approval_ref="new-approval")
    with pytest.raises(ContractError):
        module.DispatchDirectory(root).dispatch(
            changed, config, collect=lambda: replace(evidence, approval=changed, receipt=changed)
        )
    assert len(calls) == 1


def test_fsync_failure_burns_slot_before_send(setup, monkeypatch):
    root, config, binding, evidence, calls = setup
    original = module.os.fsync

    def fail(_):
        raise OSError

    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(OSError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    monkeypatch.setattr(module.os, "fsync", original)
    with pytest.raises(ContractError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    assert not calls


def test_child_rejects_config_changed_after_claim(setup, monkeypatch):
    root, config, binding, evidence, _calls = setup

    def changed(path, *, verified_deadline, expected_digest):
        config.write_text(config.read_text().replace('"correct"', '"missing"'))
        return worker.perform({"path": str(path), "deadline": verified_deadline, "digest": expected_digest})

    monkeypatch.setattr(module, "observe_isolated", changed)
    monkeypatch.setattr(worker, "observe", lambda *a, **k: pytest.fail("changed config reached network"))
    with pytest.raises(ContractError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    assert len(list(root.glob("*.intent"))) == 1


def test_concurrent_other_case_blocked_by_directory_lock(setup, monkeypatch):
    root, config, binding, evidence, _calls = setup

    def held(*args, **kwargs):
        other = replace(binding, case_id="other")
        with pytest.raises(BlockingIOError):
            module.DispatchDirectory(root).dispatch(
                other, config, collect=lambda: replace(evidence, approval=other, receipt=other)
            )
        return observation()

    monkeypatch.setattr(module, "observe_isolated", held)
    module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    assert len(list(root.glob("*.intent"))) == 1


def test_readonly_unrecorded_is_not_send_authority(setup):
    root, _config, binding, _evidence, calls = setup
    assert module.DispatchDirectory(root).inspect(binding) == {"state": "unrecorded"}
    assert not calls and not list(root.glob("*.intent"))


@pytest.mark.parametrize("kind", ["missing", "partial", "foreign", "secret", "bad_status"])
def test_bad_result_only_unknown_never_resends(setup, kind):
    root, config, binding, evidence, calls = setup
    module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    path = next(root.glob("*.result"))
    if kind == "missing":
        path.unlink()
    elif kind == "partial":
        path.write_text('{"schema":')
    else:
        record = json.loads(path.read_text())
        if kind == "foreign":
            record["binding"]["sandbox_id"] = "other"
        elif kind == "secret":
            record["observation"]["reason"] = "synthetic-secret"
        else:
            record["observation"]["status"] = True
        path.write_text(json.dumps(record))
    assert module.DispatchDirectory(root).inspect(binding) == {"state": "unknown"}
    with pytest.raises(ContractError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    assert len(calls) == 1


def test_result_save_failure_keeps_claim(setup, monkeypatch):
    root, config, binding, evidence, calls = setup

    def failed(*args):
        raise OSError

    monkeypatch.setattr(module.DispatchDirectory, "_result", failed)
    with pytest.raises(OSError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    assert module.DispatchDirectory(root).inspect(binding) == {"state": "unknown"}
    with pytest.raises(ContractError):
        module.DispatchDirectory(root).dispatch(binding, config, collect=lambda: evidence)
    assert len(calls) == 1
