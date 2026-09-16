"""Sequential installed entry → sealed receipt → CLI registration → next entry.

Platform and host identity are simulated. Supervisor poll logic is in-process;
independent supervisor HTTP wiring has its own test, not claimed by this suite.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agentseek_execution import m3_closeout_evidence as closeout_module
from agentseek_execution import m3_create_process as module
from agentseek_execution import m3_evidence_process as evidence_module
from agentseek_execution import m3_launcher as launcher
from agentseek_execution.m3_create_receipt import CreateBinding, CreateReceiptVault
from agentseek_execution.models import ContractError, canonical
from test_m3_history_installation import (  # noqa: F401 -- shared fixture dependency chain
    _observe_running,
    _supervise_disappearance,
    admission_fixture,
    installed_fixture,
    launch_fixture,
    precreate_fixture,
    template_fixture,
)


@pytest.mark.parametrize("size", [2, 4])
@pytest.mark.parametrize("corrupt_receipt", [False, True])
@pytest.mark.parametrize("launcher_flow", [False, True])
def test_complete_installation_sequence_uses_sealed_ids(request, monkeypatch, size, corrupt_receipt, launcher_flow):  # noqa: C901 -- sequence/fault matrix
    s = request.getfixturevalue("launch_fixture")
    original_plan = s.plan
    bodies = []
    for index in range(size):
        body = json.loads(s.body)
        body["metadata"]["agentseek_create_token"] = f"slot-{index}"
        body["network"]["allowPublicTraffic"] = index >= 2
        bodies.append(canonical(body).encode())
    sequence = tuple(replace(original_plan, create_token=f"slot-{i}", restricted=i < 2,
                             request_sha256=hashlib.sha256(body).hexdigest()) for i, body in enumerate(bodies))
    (s.root / "quota").mkdir(mode=0o700)
    vault = CreateReceiptVault(s.root / "vault", b"k" * 32)
    base_launch = json.loads((s.root / "launch").read_bytes())
    history = []
    posts = []
    active = []
    launchers = []

    def handle(req):
        if req.method == "POST":
            assert req.content == bodies[len(posts)]
            value = {"sandboxID": f"old-{len(posts)}", "templateID": "tpl", "domain": "example.invalid",
                     "trafficAccessToken": "synthetic-token", "envdAccessToken": None}
            posts.append(req.content)
            if launcher_flow:
                active[:] = [{"sandboxID": value["sandboxID"], "templateID": "tpl", "state": "running",
                              "domain": s.plan.domain, "metadata": {"agentseek_run_id": s.plan.run_id,
                                                                     "agentseek_create_token": s.plan.create_token}}]
        else:
            value = s.template if req.url.path.startswith("/templates/") else (
                active if req.url.path == "/sandboxes" else active[0])
        return httpx.Response(201 if req.method == "POST" else 200,
                              headers={"Content-Type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(value).encode()))

    monkeypatch.setattr(httpx, "Client", lambda **kw: httpx._client.Client(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(module, "SupervisorReader", evidence_module.SupervisorReader)
    monkeypatch.setattr(closeout_module, "verify_identity", evidence_module.verify_identity)
    supervisor = (Path(__file__).resolve().parents[3] / "examples" /
                  "enterprise_wecom_digital_employee/sandbox_poc/node_supervisor.py")
    if launcher_flow:
        script = s.root / "supervisor.py"
        script.write_bytes(supervisor.read_bytes())
        script.chmod(0o600)
        monkeypatch.setattr(launcher, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
        monkeypatch.setattr(launcher, "os", SimpleNamespace(getuid=lambda: __import__("os").getuid(), geteuid=lambda: 0))
        # Host authorization is simulated without modifying file-owner checks.
        real_attach = launcher.attach
        def bounded(argv, raw, **kwargs):
            payload = json.loads(raw)
            with monkeypatch.context() as patch:
                patch.setattr(launcher, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0))
                return canonical(real_attach(payload, closeout=argv[-1] == "--closeout")).encode()
        def creating(path, *, installation_digest, candidate_sha256):
            return CreateBinding(**module.perform({"path": str(path), "installation_digest": installation_digest,
                                                   "candidate_sha256": candidate_sha256})["binding"])
        def heartbeat(delay):
            path = s.root / "manifest.json.heartbeat"
            value = json.loads(path.read_bytes())
            value["ts"] = time.time()
            path.write_text(json.dumps(value))
        monkeypatch.setattr(launcher, "run_worker", bounded)
        monkeypatch.setattr(launcher, "launch_isolated", creating)
        monkeypatch.setattr(launcher, "SupervisorReader", evidence_module.SupervisorReader)
        monkeypatch.setattr(launcher, "verify_identity", evidence_module.verify_identity)
        monkeypatch.setattr(launcher, "time", SimpleNamespace(monotonic=time.monotonic, sleep=heartbeat))

    def private(name, data):
        path = s.root / name
        path.write_bytes(data)
        path.chmod(0o600)
        return str(path), hashlib.sha256(data).hexdigest()
    for index, plan in enumerate(sequence):
        s.plan, s.body = plan, bodies[index]
        (s.root / "request").write_bytes(s.body)
        installation = json.loads((s.root / "installation").read_bytes())
        installation["plan"] = asdict(plan)
        (s.root / "installation").write_text(json.dumps(installation))
        approval = json.loads((s.root / "approval").read_bytes())
        approval["plan"] = asdict(plan)
        (s.root / "approval").write_text(json.dumps(approval))
        config = {**base_launch, "schema": 3 if index else 2,
                  "batch_sequence": [asdict(p) for p in sequence], "quota_directory": str(s.root / "quota"),
                  "precreate_sha256": hashlib.sha256((s.root / "installation").read_bytes()).hexdigest(),
                  "approval_sha256": hashlib.sha256((s.root / "approval").read_bytes()).hexdigest()}
        if index:
            config.update(previous_create=asdict(history[-1]), earlier_creates=[asdict(b) for b in history[:-1]],
                          previous_tracking_directory=str(s.root / f"tracking-{index - 1}"))
        (s.root / "launch").write_text(json.dumps(config))
        s.launch["installation_digest"] = hashlib.sha256((s.root / "launch").read_bytes()).hexdigest()
        if launcher_flow:
            # Each slot retains its pinned installation; never overwrite A to prepare B.
            approval_path, _ = private(f"approval-{index}", (s.root / "approval").read_bytes())
            installation["approval_file"] = approval_path
            pre_path, pre_hash = private(f"precreate-{index}", json.dumps(installation).encode())
            config.update(precreate_file=pre_path, precreate_sha256=pre_hash)
            request_path, _ = private(f"request-{index}", s.body)
            config["request_file"] = request_path
            create_path, create_hash = private(f"create-{index}", json.dumps(config).encode())
            tracking = s.root / f"tracking-{index}"
            tracking.mkdir(mode=0o700)
            launch_path, launch_hash = private(f"launcher-{index}", canonical({
                "schema": 1, "create_file": create_path, "create_sha256": create_hash,
                "candidate_sha256": plan.candidate_sha256, "supervisor_script": str(script),
                "supervisor_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "tracking_directory": str(tracking)}).encode())
            if index:
                result = launcher.launch_successor(Path(launch_path), launch_hash, *launchers[-1], history[-1])
            else:
                result = launcher.launch(Path(launch_path), launch_hash)
            launchers.append((Path(launch_path), launch_hash))
        else:
            result = module.perform(s.launch)
        binding = CreateBinding(**result["binding"])
        if corrupt_receipt and index == 0:
            before_corruption = (s.root / "manifest.json").read_bytes()
            receipt_path = next((s.root / "vault").glob("*.receipt"))
            receipt_path.write_bytes(b"corrupted synthetic receipt")
            with pytest.raises(ContractError):
                vault.read(binding)
            assert (s.root / "manifest.json").read_bytes() == before_corruption
            assert len(posts) == 1
            assert not (s.root / "quota" / "create-slot-1").exists()
            return
        receipt = vault.read(binding)
        assert receipt.sandbox_id == f"old-{index}"
        before = json.loads((s.root / "manifest.json").read_bytes())["sandboxes"]
        subprocess.run(  # noqa: S603 -- fixed local CLI, ID from synthetic sealed receipt
            [sys.executable, "-I", str(supervisor), "--manifest", str(s.root / "manifest.json"),
             "--alarm-file", str(s.root / "alarm"), "--register-sandbox", receipt.sandbox_id],
            env={k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}},
            umask=0o077, capture_output=True, check=True, timeout=5)
        after = json.loads((s.root / "manifest.json").read_bytes())["sandboxes"]
        assert all(after[k] == v for k, v in before.items())
        assert set(after) == {vault.read(b).sandbox_id for b in [*history, binding]}
        # Helpers use the fixed temporary name; retain each target directory after observation.
        if not launcher_flow:
            (s.root / "tracking").mkdir(mode=0o700)
            _observe_running(s, replace(binding, intent_sha256="0" * 64), index + 1, monkeypatch, wrong_marker=False)
            (s.root / "tracking").rename(s.root / f"tracking-{index}")
        _supervise_disappearance(s, index + 1, monkeypatch, disappear=True)
        active.clear()
        history.append(binding)
        with pytest.raises(ContractError):
            module.perform(s.launch)
        assert len(posts) == index + 1
    assert len(list((s.root / "vault").glob("*.receipt"))) == size
    assert len(list((s.root / "fence").glob("create.closed-*"))) == size - 1
