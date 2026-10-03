"""C/D installation history: real private records and collectors, mocked host/HTTP."""

import hashlib
import json
import os
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agentseek_execution import m3_closeout_evidence as closeout_module
from agentseek_execution import m3_create_process as module
from agentseek_execution import m3_evidence_process as evidence_module
from agentseek_execution.m3_batch_quota import BatchQuota
from agentseek_execution.m3_create_fence import CreateFence
from agentseek_execution.m3_create_receipt import CreateReceiptVault
from agentseek_execution.m3_target_tracking import _write as record_target
from agentseek_execution.m3_target_tracking import track_pending
from agentseek_execution.models import ContractError, canonical
from test_m3_create_process import (  # noqa: F401 -- transitive fixtures
    admission_fixture,
    installed_fixture,
    precreate_fixture,
    template_fixture,
)
from test_m3_create_process import setup as launch_fixture  # noqa: F401 -- shared fixture


@pytest.mark.parametrize("slot", [2, 3])
@pytest.mark.parametrize("mode", ["valid", "missing", "swapped", "foreign", "receipt_missing", "observed", "wrong_marker",
                                  "supervised", "termination_pending"])
def test_public_slot_requires_complete_ordered_history(request, monkeypatch, slot, mode):
    s = request.getfixturevalue("launch_fixture")
    body = json.loads(s.body)
    body["network"]["allowPublicTraffic"] = True
    s.body = canonical(body).encode()
    plan = replace(s.plan, restricted=False, request_sha256=hashlib.sha256(s.body).hexdigest())
    sequence = tuple(plan if i == slot else replace(plan, create_token=f"slot-{i}", restricted=i < 2)
                     for i in range(4))
    for name in ("quota", "tracking"):
        (s.root / name).mkdir(mode=0o700)
    quota = BatchQuota(s.root / "quota", sequence)
    vault = CreateReceiptVault(s.root / "vault", b"k" * 32)
    history = []
    for i in range(slot):
        quota.reserve(sequence[i])
        binding = vault.reserve_now(sequence[i].binding("a" * 64))
        if not (mode == "receipt_missing" and i == 0):
            vault.seal_response(binding, json.dumps({"sandboxID": f"old-{i}", "templateID": "tpl",
                "domain": "example.invalid", "trafficAccessToken": "synthetic-token", "envdAccessToken": None}).encode())
        history.append(binding)
    old = replace(history[-1], intent_sha256="0" * 64)
    CreateFence(s.root / "fence").claim(old)
    if mode in {"observed", "wrong_marker", "supervised", "termination_pending"}:
        _observe_running(s, old, slot, monkeypatch, wrong_marker=mode == "wrong_marker")
    else:
        record_target(s.root / "tracking", hashlib.sha256(canonical(asdict(old)).encode()).hexdigest(), f"old-{slot - 1}")
    manifest = json.loads((s.root / "manifest.json").read_bytes())
    manifest["sandboxes"] = {f"old-{i}": {"registered_at": manifest["run_started_at"]} for i in range(slot)}
    (s.root / "manifest.json").write_text(json.dumps(manifest))
    if mode in {"supervised", "termination_pending"}:
        _supervise_disappearance(s, slot, monkeypatch, disappear=mode == "supervised")
    precreate = json.loads((s.root / "installation").read_bytes())
    precreate["plan"] = asdict(plan)
    (s.root / "installation").write_text(json.dumps(precreate))
    approval = json.loads((s.root / "approval").read_bytes())
    approval["plan"] = asdict(plan)
    (s.root / "approval").write_text(json.dumps(approval))
    (s.root / "request").write_bytes(s.body)
    earlier = [asdict(b) for b in history[:-1]]
    if mode == "missing":
        earlier.pop()
    elif mode == "swapped":
        earlier[0] = asdict(history[-1])
    elif mode == "foreign":
        earlier[0] = asdict(replace(history[0], run_id="foreign"))
    launch = json.loads((s.root / "launch").read_bytes())
    launch.update(schema=3, batch_sequence=[asdict(p) for p in sequence], earlier_creates=earlier,
                  previous_create=asdict(history[-1]), previous_tracking_directory=str(s.root / "tracking"),
                  quota_directory=str(s.root / "quota"),
                  precreate_sha256=hashlib.sha256((s.root / "installation").read_bytes()).hexdigest(),
                  approval_sha256=hashlib.sha256((s.root / "approval").read_bytes()).hexdigest())
    (s.root / "launch").write_text(json.dumps(launch))
    s.launch["installation_digest"] = hashlib.sha256((s.root / "launch").read_bytes()).hexdigest()
    monkeypatch.setattr(module, "SupervisorReader", evidence_module.SupervisorReader)
    monkeypatch.setattr(closeout_module, "verify_identity", evidence_module.verify_identity)
    if mode in {"valid", "observed", "supervised"}:
        module.perform(s.launch)
        assert sum(req.method == "POST" for req in s.network) == 1
        assert json.loads((s.root / "manifest.json").read_bytes()) == manifest
        with pytest.raises(ContractError):
            module.perform(s.launch)
        assert sum(req.method == "POST" for req in s.network) == 1
    else:
        with pytest.raises(ContractError):
            module.perform(s.launch)
        assert not any(req.method == "POST" for req in s.network)
        assert (s.root / "quota" / f"create-slot-{slot}").exists() is (mode == "wrong_marker")


def _observe_running(s, binding, slot, monkeypatch, *, wrong_marker):
    """Persist only an exact observed running target, then restore empty HTTP fixture."""
    sid = f"old-{slot - 1}"
    paths = []

    def handle(req):
        assert req.method == "GET"
        paths.append(req.url.path)
        value = [{"sandboxID": sid}] if req.url.path == "/sandboxes" else {
            "sandboxID": sid, "templateID": binding.template_id, "state": "running",
            "domain": binding.domain, "metadata": {
                "agentseek_run_id": binding.run_id,
                "agentseek_create_token": "foreign" if wrong_marker else binding.create_token,
            },
        }
        return httpx.Response(200, headers={"Content-Type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(value).encode()))

    with monkeypatch.context() as patch:
        patch.setattr(httpx, "Client", lambda **kw: httpx._client.Client(transport=httpx.MockTransport(handle)))
        reader = module.PlatformReader(endpoint=s.plan.endpoint, api_key="synthetic-api-key",
                                       ca_file=s.root / "ca", domain=s.plan.domain, proxy_port=13080)
        result = track_pending(reader, CreateFence(s.root / "fence"), binding, s.root / "tracking")
    assert paths == ["/sandboxes", f"/sandboxes/{sid}"]
    assert result.state == ("identity_conflict" if wrong_marker else "exact_running_observed")
    assert (s.root / "tracking" / "create.target").exists() is (not wrong_marker)


def _supervise_disappearance(s, slot, monkeypatch, *, disappear):
    """Actual supervisor poll/deadline/manifest code; no real client or waiting."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "examples" / "enterprise_wecom_digital_employee"))
    from sandbox_poc.node_supervisor import ManifestStore, NodeSupervisor, SandboxRecord

    now = [time.time() - 121, 100.0]
    sid = f"old-{slot - 1}"
    records = [SandboxRecord(sid, "tpl", "running", now[0], s.plan.run_id)]
    killed = []
    client = SimpleNamespace(list=lambda: list(records), kill=killed.append)
    path = s.root / "manifest.json"
    before = path.read_bytes()
    supervisor = NodeSupervisor(client, ManifestStore(path), alarm_file=s.root / "alarm",
                                epoch_clock=lambda: now[0], mono_clock=lambda: now[1])
    # Supervisor inherits its service umask; do not chmod results after the fact.
    previous_umask = os.umask(0o077)
    try:
        assert supervisor.poll_once() == [] and killed == []
        now[0] += 121
        now[1] += 121
        assert supervisor.poll_once() == [sid] and killed == [sid]
        assert (s.root / "alarm").exists()
        if disappear:
            records.clear()
            assert supervisor.poll_once() == []
            assert not supervisor._targets and not (s.root / "alarm").exists()
    finally:
        os.umask(previous_umask)
    assert (s.root / "manifest.json.heartbeat").stat().st_mode & 0o777 == 0o600
    assert path.read_bytes() == before
