"""Launcher phase ordering and real sealed receipt → registration CLI attachment."""

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agentseek_execution import m3_closeout_evidence as closeout_module
from agentseek_execution import m3_create_process as create
from agentseek_execution import m3_evidence_process as evidence
from agentseek_execution import m3_launcher as module
from agentseek_execution.models import Code, ContractError, canonical
from test_m3_create_process import (  # noqa: F401 -- fixture dependency chain
    admission_fixture,
    installed_fixture,
    precreate_fixture,
    template_fixture,
)
from test_m3_create_process import setup as launch_fixture  # noqa: F401


@pytest.fixture
def prepared(request):
    s = request.getfixturevalue("launch_fixture")
    source = Path(__file__).resolve().parents[3] / "examples/enterprise_wecom_digital_employee/sandbox_poc/node_supervisor.py"
    script = s.root / "supervisor.py"
    script.write_bytes(source.read_bytes())
    script.chmod(0o600)
    (s.root / "tracking").mkdir(mode=0o700)
    config = {"schema": 1, "create_file": str(s.root / "launch"),
              "create_sha256": s.launch["installation_digest"], "candidate_sha256": s.plan.candidate_sha256,
              "supervisor_script": str(script), "supervisor_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
              "tracking_directory": str(s.root / "tracking")}
    path = s.root / "launcher"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    s.launcher = path
    s.launcher_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return s


@pytest.mark.parametrize("mode", ["success", "receipt_corrupt", "registration_failed", "supervisor_failed",
                                  "target_missing", "heartbeat_timeout", "identity_changed",
                                  "closed", "still_running", "closeout_alarm"])
def test_attach_actual_receipt_and_cli(prepared, monkeypatch, mode):  # noqa: C901 -- phase fault matrix
    s = prepared
    binding = create.perform(s.launch)["binding"]
    fence_before = (s.root / "fence/create.pending").read_bytes()
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux", executable=__import__("sys").executable))
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0))
    monkeypatch.setattr(module, "SupervisorReader", evidence.SupervisorReader)
    monkeypatch.setattr(module, "verify_identity", evidence.verify_identity)
    monkeypatch.setattr(closeout_module, "verify_identity", evidence.verify_identity)
    gone = [False]
    def next_heartbeat(delay):
        path = s.root / "manifest.json.heartbeat"
        beat = json.loads(path.read_bytes())
        beat["ts"] = time.time()
        path.write_text(json.dumps(beat))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=time.monotonic, sleep=next_heartbeat))
    if mode == "heartbeat_timeout":
        elapsed = [0.0]
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: elapsed[0],
                            sleep=lambda delay: elapsed.__setitem__(0, elapsed[0] + delay)))
    if mode == "identity_changed":
        checks = []
        def changed(*args):
            identity = evidence.verify_identity(*args)
            checks.append(identity)
            return SimpleNamespace(pid=identity.pid + len(checks) - 1, start_ticks=identity.start_ticks)
        monkeypatch.setattr(module, "verify_identity", changed)
    if mode == "receipt_corrupt":
        next((s.root / "vault").glob("*.receipt")).write_bytes(b"corrupt")
    if mode == "registration_failed":
        monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=2))
    if mode == "supervisor_failed":
        monkeypatch.setattr(module, "verify_identity", lambda *a: (_ for _ in ()).throw(ContractError(Code.DENIED)))

    def handle(req):
        assert req.method == "GET"
        value = ([] if mode == "target_missing" or gone[0] else [{"sandboxID": "vm"}]) if req.url.path == "/sandboxes" else {
            "sandboxID": "vm", "templateID": "tpl", "state": "running", "domain": s.plan.domain,
            "metadata": {"agentseek_run_id": s.plan.run_id, "agentseek_create_token": s.plan.create_token}}
        return httpx.Response(200, headers={"Content-Type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(value).encode()))

    monkeypatch.setattr(httpx, "Client", lambda **kw: httpx._client.Client(transport=httpx.MockTransport(handle)))
    payload = {"path": str(s.launcher), "sha256": s.launcher_digest, "binding": binding}
    if mode in {"success", "closed", "still_running", "closeout_alarm"}:
        assert module.attach(payload)["registered_and_observed"] is True
        assert (s.root / "tracking/create.target").exists()
        if mode != "success":
            before = {p: p.read_bytes() for p in s.root.rglob("*") if p.is_file()}
            gone[0] = mode != "still_running"
            if mode == "closeout_alarm":
                (s.root / "alarm").write_text("synthetic alarm")
            if mode == "closed":
                result = module.attach(payload, closeout=True)
                assert result["known_target_absent"] is True
                assert result["next_create_authorized"] is False
            else:
                with pytest.raises(ContractError):
                    module.attach(payload, closeout=True)
            assert all(p.read_bytes() == raw for p, raw in before.items())
    else:
        with pytest.raises(ContractError):
            module.attach(payload)
    assert (s.root / "fence/create.pending").read_bytes() == fence_before
    assert len(list((s.root / "vault").glob("*.intent"))) == 1
    if mode in {"receipt_corrupt", "registration_failed", "supervisor_failed"}:
        assert json.loads((s.root / "manifest.json").read_bytes())["sandboxes"] == {}


@pytest.mark.parametrize("failure", ["create", "attach"])
def test_launcher_never_retries_or_advances_after_failure(prepared, monkeypatch, failure, caplog):
    calls = []

    def creating(*args, **kwargs):
        calls.append("create")
        if failure == "create":
            raise ContractError(Code.UNKNOWN)
        from agentseek_execution.m3_create_receipt import CreateBinding
        return CreateBinding(**create.perform(prepared.launch)["binding"])

    def attaching(*args, **kwargs):
        calls.append("attach")
        raise ContractError(Code.UNKNOWN)

    monkeypatch.setattr(module, "launch_isolated", creating)
    monkeypatch.setattr(module, "run_worker", attaching)
    with pytest.raises(ContractError):
        module.launch(prepared.launcher, prepared.launcher_digest)
    assert calls == (["create"] if failure == "create" else ["create", "attach"])
    records = [json.loads(r.getMessage().split("execution_stage ", 1)[1])
               for r in caplog.records if r.getMessage().startswith("execution_stage ")]
    assert any(r["stage"] == failure + "_worker" and r["event"] == "failed" for r in records)
    if failure == "create":
        assert not any(r["stage"] == "attach_worker" for r in records)


@pytest.mark.parametrize("fault", ["script", "tracking_mode", "tracking_nonempty", "create_pin"])
def test_bad_attach_installation_rejected_before_create(prepared, monkeypatch, fault):
    s = prepared
    if fault == "script":
        (s.root / "supervisor.py").write_bytes(b"changed")
    elif fault == "tracking_mode":
        (s.root / "tracking").chmod(0o755)
    elif fault == "tracking_nonempty":
        (s.root / "tracking/old").touch()
    else:
        (s.root / "launch").write_bytes(b"changed")
    calls = []
    monkeypatch.setattr(module, "launch_isolated", lambda *a, **k: calls.append("create"))
    with pytest.raises(ContractError):
        module.launch(s.launcher, s.launcher_digest)
    assert calls == []


def test_launcher_calls_fixed_bounded_attach_with_binding(prepared, monkeypatch, caplog):
    from agentseek_execution.m3_create_receipt import CreateBinding
    s = prepared
    binding = CreateBinding(**create.perform(s.launch)["binding"])
    calls = []
    monkeypatch.setattr(module, "launch_isolated", lambda *a, **k: binding)

    def bounded(argv, raw, **kwargs):
        calls.append(argv)
        assert argv[1:] == ["-I", "-m", "agentseek_execution.m3_launcher", "--attach"]
        assert kwargs == {"budget": 10, "environment": {}}
        payload = json.loads(raw)
        assert payload["binding"]["intent_sha256"] == binding.intent_sha256
        return canonical({"schema": 1, "binding": payload["binding"], "registered_and_observed": True}).encode()

    monkeypatch.setattr(module, "run_worker", bounded)
    assert module.launch(s.launcher, s.launcher_digest)["registered_and_observed"] is True
    assert len(calls) == 1
    records = [json.loads(r.getMessage().split("execution_stage ", 1)[1])
               for r in caplog.records if r.getMessage().startswith("execution_stage ")]
    assert [(r["stage"], r["event"]) for r in records] == [
        ("launcher", "started"), ("create_worker", "started"), ("create_worker", "complete"),
        ("attach_worker", "started"), ("attach_worker", "complete"), ("launcher", "complete")]


@pytest.mark.parametrize("mode", ["success", "not_closed", "wrong_binding", "wrong_tracking", "wrong_fence"])
def test_successor_checks_closeout_before_launch(prepared, monkeypatch, mode):
    from agentseek_execution.m3_create_receipt import CreateBinding
    s = prepared
    binding = CreateBinding(**create.perform(s.launch)["binding"])
    old_config = json.loads(s.launcher.read_bytes())
    installed = json.loads((s.root / "launch").read_bytes())
    installed.update(schema=2, quota_directory=str(s.root / "quota"), batch_sequence=["synthetic"])
    def write(name, value):
        path = s.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path, hashlib.sha256(path.read_bytes()).hexdigest()
    old_create, old_hash = write("old-create", installed)
    old_config.update(create_file=str(old_create), create_sha256=old_hash)
    old_path, old_digest = write("old-launcher", old_config)
    installed.update(schema=3, previous_create=asdict(binding), previous_tracking_directory=old_config["tracking_directory"])
    if mode == "wrong_binding":
        installed["previous_create"]["create_token"] = "foreign"  # noqa: S105 -- synthetic identity marker
    elif mode == "wrong_tracking":
        installed["previous_tracking_directory"] += "-other"
    elif mode == "wrong_fence":
        installed["fence_directory"] += "-other"
    new_create, new_hash = write("new-create", installed)
    new_path, new_digest = write("new-launcher", {**old_config, "create_file": str(new_create), "create_sha256": new_hash})
    calls = []
    def closed(*args):
        calls.append("closeout")
        if mode == "not_closed":
            raise ContractError(Code.DENIED)
    monkeypatch.setattr(module, "check_closed", closed)
    monkeypatch.setattr(module, "launch", lambda *args: calls.append("launch") or {"ok": True})
    if mode == "success":
        assert module.launch_successor(new_path, new_digest, old_path, old_digest, binding) == {"ok": True}
        assert calls == ["closeout", "launch"]
    else:
        with pytest.raises(ContractError):
            module.launch_successor(new_path, new_digest, old_path, old_digest, binding)
        assert calls == (["closeout"] if mode == "not_closed" else [])
