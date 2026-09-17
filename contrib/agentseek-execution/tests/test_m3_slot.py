import hashlib
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from agentseek_execution import m3_slot as module
from agentseek_execution.m3_probe_dispatch import Evidence
from agentseek_execution.models import Code, ContractError, canonical
from test_m3_receipt_probe import setup


@pytest.fixture
def slot(setup, monkeypatch):
    s = setup
    clock = [100.0]
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    # config_bytes uses the actual process owner on macOS; only entry privilege
    # checks are substituted, not ownership checks in file readers.
    monkeypatch.setattr(module, "os", SimpleNamespace(**{name: getattr(module.os, name) for name in dir(module.os)
                                                        if not name.startswith("__")}))
    monkeypatch.setattr(module.os, "getuid", lambda: 0)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    files = {}
    rows = []
    for index, (endpoint, state) in enumerate(module.MATRIX):
        binding = replace(s.binding, case_id=f"case-{index}")
        probe = str(s.root / f"probe-{index}")
        installed = str(s.root / f"install-{index}")
        files[probe] = dict(endpoint=endpoint, state=state, traffic_alias="e2b")
        files[installed] = dict(schema=4, binding=asdict(binding), receipt_source=s.source.wire(), probe_config=probe)
        rows.append(dict(installation=installed, sha256="a" * 64, approval_sha256="b" * 64))
    config = dict(schema=1, slot="A", reserve_seconds=3, directory=str(s.dispatch), rows=rows)
    path = s.root / "slot"
    files[str(path)] = config
    monkeypatch.setattr(module, "_pinned", lambda path, digest: json.dumps(files[str(path)]).encode())
    def evidence(path, **kwargs):
        binding = kwargs["binding"]
        return Evidence(binding, binding, clock[0], clock[0], 220, 220, binding.run_id,
                        binding.boot_id, True, True, True, 1)
    monkeypatch.setattr(module, "collect_gate_isolated", evidence)
    calls = []
    def dispatch(path, directory, **kwargs):
        index = len(calls)
        calls.append(kwargs)
        clock[0] += 1
        endpoint, state = module.MATRIX[index]
        return dict(status=200 if state == "correct" else 403, body_size=1, body_sha256="a" * 64,
                    complete=True, reason="response", protocol=dict(
                        kind=({"E1": "command_success", "E2": "file_payload", "E3": "stat_payload"}[endpoint]
                              if state == "correct" else "http_denial_signal"),
                        activity=endpoint == "E1" and state == "correct", exit_code=0 if endpoint == "E1" else None))
    monkeypatch.setattr(module, "dispatch_isolated", dispatch)
    return SimpleNamespace(path=path, config=config, calls=calls, clock=clock, files=files,
                           directory=s.dispatch, dispatch=dispatch)


def test_slot_calls_real_entry_interface_with_one_cutoff_and_no_b(slot):
    result = module.run(slot.path, "c" * 64)
    assert result["rows_observed"] == 5 and result["next_create_authorized"] is False
    assert len(slot.calls) == 5
    assert {item["deadline"] for item in slot.calls} == {217}
    assert len(list(slot.directory.glob("*.slot-result"))) == 1
    with pytest.raises(FileExistsError):
        module.run(slot.path, "c" * 64)
    assert len(slot.calls) == 5


@pytest.mark.parametrize("mode", ["unknown", "positive_failure", "setup_elapsed", "mid_elapsed"])
def test_slot_stops_without_resume(slot, monkeypatch, mode):
    if mode == "setup_elapsed":
        slot.clock[0] = 138  # 82 remaining < 80+3; zero probes.
    else:
        def dispatch(*args, **kwargs):
            result = slot.dispatch(*args, **kwargs)
            if mode == "unknown":
                raise ContractError(Code.UNKNOWN)
            if mode == "positive_failure":
                result["status"] = 403
            if mode == "mid_elapsed":
                slot.clock[0] = 160
            return result
        monkeypatch.setattr(module, "dispatch_isolated", dispatch)
    with pytest.raises(ContractError):
        module.run(slot.path, "c" * 64)
    assert len(slot.calls) == (0 if mode == "setup_elapsed" else 1)
    assert not list(slot.directory.glob("*.slot-result"))
    with pytest.raises(FileExistsError):
        module.run(slot.path, "c" * 64)


@pytest.mark.parametrize("reserve", [0, -1, True, float("nan"), float("inf"), 120])
def test_reserve_is_explicit_not_defaulted(slot, reserve):
    slot.config["reserve_seconds"] = reserve
    with pytest.raises(ContractError):
        module.run(slot.path, "c" * 64)
    assert not slot.calls


@pytest.mark.parametrize("mode", ["success", "wrong_a", "x1_failure"])
def test_b_six_rows_require_matching_a_and_stop_on_x1_failure(slot, monkeypatch, mode):
    s = slot
    s.config["slot"] = "B"
    final = dict(s.config["rows"][0], installation=str(s.path.parent / "x1"))
    first = s.files[s.config["rows"][0]["installation"]]
    installed = dict(first, binding=dict(first["binding"], case_id="x1"),
                     receipt_source=dict(first["receipt_source"], donor={"create": {"create_token": "A"}}),
                     probe_config=str(s.path.parent / "x1-probe"))
    s.files[final["installation"]] = installed
    s.files[installed["probe_config"]] = dict(endpoint="E1", state="cross_guest", traffic_alias="e2b")
    s.config["rows"].append(final)
    previous = str(s.path.parent / "a-result")
    s.config["previous_slot"] = dict(path=previous, sha256="d" * 64)
    s.files[previous] = dict(schema=1, slot="A", rows_observed=5, next_create_authorized=False,
                            config_sha256="d" * 64, create={"create_token": "bad" if mode == "wrong_a" else "A"})
    def dispatch(*args, **kwargs):
        if len(s.calls) < 5:
            return s.dispatch(*args, **kwargs)
        s.calls.append(kwargs)
        return dict(status=403 if mode == "success" else 200, body_size=1, body_sha256="a" * 64,
                    complete=True, reason="response", protocol=dict(kind="http_denial_signal", activity=False, exit_code=None))
    monkeypatch.setattr(module, "dispatch_isolated", dispatch)
    if mode == "success":
        assert module.run(s.path, "c" * 64)["rows_observed"] == 6
    else:
        with pytest.raises(ContractError):
            module.run(s.path, "c" * 64)
        assert len(s.calls) == (0 if mode == "wrong_a" else 6)
        assert not list(s.directory.glob("*.slot-result"))
