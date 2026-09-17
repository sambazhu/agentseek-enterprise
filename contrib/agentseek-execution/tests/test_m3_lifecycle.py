import hashlib
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from agentseek_execution import m3_lifecycle as module
from agentseek_execution.models import Code, ContractError
from test_m3_receipt_probe import setup


@pytest.fixture
def lifecycle(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0,
                                                     close=module.os.close))
    def write(name, value):
        path = s.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path, hashlib.sha256(path.read_bytes()).hexdigest()
    install, install_sha = write("installation", {"schema": 2})
    launcher, launcher_sha = write("launcher", {"create_file": str(install), "create_sha256": install_sha})
    config = dict(schema=1, slot="A", launcher=str(launcher), launcher_sha256=launcher_sha,
                  directory=str(s.dispatch))
    path, digest = write("lifecycle", config)
    calls = []
    result = dict(schema=1, registered_and_observed=True, binding=asdict(s.source.create))
    monkeypatch.setattr(module, "launch", lambda *a: calls.append("create") or result)
    monkeypatch.setattr(module, "check_closed", lambda *a: calls.append("closeout") or
                        dict(known_target_absent=True, next_create_authorized=False))
    return SimpleNamespace(path=path, digest=digest, calls=calls, root=s.dispatch, config=config,
                           write=write, result=result)


def test_create_saved_then_explicit_closeout_no_automatic_b(lifecycle):
    s = lifecycle
    assert module.execute(s.path, s.digest, "create")["saved"] is True
    assert s.calls == ["create"]
    assert json.loads((s.root / "create-result.json").read_bytes())["result"] == s.result
    assert module.execute(s.path, s.digest, "closeout")["next_create_authorized"] is False
    assert s.calls == ["create", "closeout"]
    with pytest.raises(FileExistsError):
        module.execute(s.path, s.digest, "create")
    assert s.calls == ["create", "closeout"]


@pytest.mark.parametrize("mode", ["unknown", "save_failure"])
def test_uncertain_create_never_retries(lifecycle, monkeypatch, mode):
    s = lifecycle
    if mode == "unknown":
        def failed(*args):
            s.calls.append("create")
            raise ContractError(Code.UNKNOWN)
        monkeypatch.setattr(module, "launch", failed)
    else:
        original = module._save
        def failed(fd, name, value):
            if name == "create-result.json":
                raise OSError("synthetic failure")
            return original(fd, name, value)
        monkeypatch.setattr(module, "_save", failed)
    with pytest.raises((ContractError, OSError)):
        module.execute(s.path, s.digest, "create")
    with pytest.raises(FileExistsError):
        module.execute(s.path, s.digest, "create")
    assert s.calls == ["create"]


@pytest.mark.parametrize("mode", ["success", "failed_a", "wrong_binding", "not_closed"])
def test_b_requires_a_success_and_calls_successor_not_plain_launch(lifecycle, monkeypatch, mode):
    s = lifecycle
    install, install_sha = s.write("installation-b", {"schema": 3})
    launch, launch_sha = s.write("launcher-b", {"create_file": str(install), "create_sha256": install_sha})
    previous, previous_sha = s.write("previous-create", {"result": s.result})
    completed = dict(slot="A", rows_observed=5, next_create_authorized=False, create=s.result["binding"])
    if mode == "failed_a":
        completed["rows_observed"] = 4
    if mode == "wrong_binding":
        completed["create"] = dict(completed["create"], create_token="foreign")
    report, report_sha = s.write("previous-slot", completed)
    s.config.update(slot="B", previous=dict(launcher=s.config["launcher"],
        launcher_sha256=s.config["launcher_sha256"], create_result=str(previous),
        create_result_sha256=previous_sha, slot_result=str(report), slot_result_sha256=report_sha))
    s.config.update(launcher=str(launch), launcher_sha256=launch_sha)
    path, digest = s.write("lifecycle", s.config)
    def successor(*args):
        s.calls.append("successor")
        if mode == "not_closed":
            raise ContractError(Code.DENIED)
        return s.result
    monkeypatch.setattr(module, "launch_successor", successor)
    if mode == "success":
        assert module.execute(path, digest, "create")["saved"] is True
        assert s.calls == ["successor"]
    else:
        with pytest.raises(ContractError):
            module.execute(path, digest, "create")
        assert s.calls == (["successor"] if mode == "not_closed" else [])
