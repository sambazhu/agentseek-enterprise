import hashlib
import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import httpx
import pytest
from agentseek_execution import m3_closeout_evidence as closeout_module
from agentseek_execution import m3_create_process as module
from agentseek_execution import m3_evidence_process as evidence_module
from agentseek_execution.m3_batch_quota import BatchQuota
from agentseek_execution.m3_closeout_evidence import CloseoutEvidence
from agentseek_execution.m3_create_fence import CreateFence
from agentseek_execution.m3_create_receipt import CreateBinding, CreateReceiptVault
from agentseek_execution.m3_target_tracking import _write as record_target
from agentseek_execution.models import ContractError, canonical
from test_m3_create_admission import (  # noqa: F401 -- transitive fixtures
    installed_fixture,
    precreate_fixture,
    template_fixture,
)
from test_m3_create_admission import setup as admission_fixture  # noqa: F401 -- actual private installation fixture


@pytest.fixture
def setup(request, monkeypatch):
    s = request.getfixturevalue("admission_fixture")
    for name in ("vault", "fence"):
        (s.root / name).mkdir(mode=0o700)
    (s.root / "request").write_bytes(s.body)
    (s.root / "receipt-key").write_bytes(b"k" * 32)
    for name in ("request", "receipt-key"):
        (s.root / name).chmod(0o600)

    def digest(name):
        return hashlib.sha256((s.root / name).read_bytes()).hexdigest()

    config = {
        "schema": 1,
        "precreate_file": str(s.root / "installation"),
        "precreate_sha256": s.payload["installation_digest"],
        "approval_sha256": s.payload["approval_digest"],
        "request_file": str(s.root / "request"),
        "vault_directory": str(s.root / "vault"),
        "fence_directory": str(s.root / "fence"),
        "receipt_key_file": str(s.root / "receipt-key"),
        "receipt_key_sha256": digest("receipt-key"),
        "api_key_sha256": digest("api-key"),
        "ca_sha256": digest("ca"),
    }
    (s.root / "launch").write_text(json.dumps(config))
    (s.root / "launch").chmod(0o600)
    s.launch = {
        "path": str(s.root / "launch"),
        "installation_digest": digest("launch"),
        "candidate_sha256": s.plan.candidate_sha256,
    }
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0))
    monkeypatch.setattr(module, "system_clock", lambda: SimpleNamespace(boot_id=s.plan.boot_id))
    s.network = []
    s.hook = None

    def handle(req):
        s.network.append(req)
        if s.hook:
            s.hook(req)
        if req.method == "POST":
            assert req.content == s.body and req.headers["X-API-Key"] == "synthetic-api-key"
            value = {
                "sandboxID": "vm",
                "templateID": "tpl",
                "domain": "example.invalid",
                "trafficAccessToken": "synthetic-private-token",
                "envdAccessToken": None,
            }
            status = 201
        else:
            value = s.template if req.url.path.startswith("/templates/") else []
            status = 200
        return httpx.Response(
            status, headers={"Content-Type": "application/json"}, stream=httpx.ByteStream(json.dumps(value).encode())
        )

    monkeypatch.setattr(httpx, "Client", lambda **kw: httpx._client.Client(transport=httpx.MockTransport(handle)))
    return s


def test_pinned_loader_composes_actual_live_create_and_seals(setup):
    s = setup
    result = module.perform(s.launch)
    binding = CreateBinding(**result["binding"])
    vault = CreateReceiptVault(s.root / "vault", b"k" * 32)
    assert vault.read(binding).sandbox_id == "vm"
    assert result["installation_digest"] == s.launch["installation_digest"]
    assert "synthetic" not in json.dumps(result)
    assert [r.method for r in s.network] == ["GET"] * 4 + ["POST"]
    with pytest.raises(ContractError):
        module.perform(s.launch)
    assert sum(r.method == "POST" for r in s.network) == 1


@pytest.mark.parametrize("mode", ["success", "second_gate", "successor", "same_directory", "untrusted_sequence"])
def test_batch_installation_first_slot_only(setup, mode):
    s = setup
    path = s.root / "launch"
    config = json.loads(path.read_bytes())
    quota = s.root / "quota"
    quota.mkdir(mode=0o700)
    sequence = [asdict(s.plan), asdict(replace(s.plan, create_token="next"))]
    if mode == "successor":
        sequence = [asdict(replace(s.plan, create_token="previous")), asdict(s.plan)]
    config.update(schema=2, batch_sequence=sequence, quota_directory=str(quota))
    if mode == "same_directory":
        config["quota_directory"] = str(s.root / "fence")
    path.write_text(json.dumps(config))
    s.launch["installation_digest"] = hashlib.sha256(path.read_bytes()).hexdigest()
    if mode == "untrusted_sequence":
        config["batch_sequence"][1]["create_token"] = "changed"  # noqa: S105 -- synthetic idempotency marker
        path.write_text(json.dumps(config))
    if mode == "second_gate":
        def change(req):
            if len(s.network) == 4:
                path.write_bytes(b"changed")

        s.hook = change
    if mode == "success":
        module.perform(s.launch)
        assert (quota / "create-slot-0").is_file()
        with pytest.raises(ContractError):
            module.perform(s.launch)
        assert sum(r.method == "POST" for r in s.network) == 1
    else:
        with pytest.raises(ContractError):
            module.perform(s.launch)
        assert not any(r.method == "POST" for r in s.network)
        assert (quota / "create-slot-0").exists() is (mode == "second_gate")
        if mode != "second_gate":
            assert not s.network


@pytest.mark.parametrize("mode", ["success", "absent_receipt", "closeout_denied", "changed_pin",
                                  "real_collect", "missing_target", "late_alarm", "foreign_registration", "lost_registration"])
def test_successor_installation_composes_pinned_prior_and_live_collector(setup, monkeypatch, mode):  # noqa: C901 -- fault matrix
    s = setup
    a = replace(s.plan, create_token="previous")
    old = a.binding("a" * 64)
    vault = CreateReceiptVault(s.root / "vault", b"k" * 32)
    previous = vault.reserve_now(old)
    manifest_path = s.root / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["sandboxes"] = {"old-vm": {"registered_at": manifest["run_started_at"]}}
    if mode == "foreign_registration":
        manifest["sandboxes"]["foreign"] = {"registered_at": manifest["run_started_at"]}
    elif mode == "lost_registration":
        manifest["sandboxes"] = {}
    manifest_path.write_text(json.dumps(manifest))
    if mode != "absent_receipt":
        vault.seal_response(previous, json.dumps({
            "sandboxID": "old-vm", "templateID": "tpl", "domain": "example.invalid",
            "trafficAccessToken": "synthetic-private-token", "envdAccessToken": None,
        }).encode())
    CreateFence(s.root / "fence").claim(old)
    for name in ("quota", "tracking"):
        (s.root / name).mkdir(mode=0o700)
    BatchQuota(s.root / "quota", (a, s.plan)).reserve(a)
    path = s.root / "launch"
    config = json.loads(path.read_bytes())
    config.update(schema=3, batch_sequence=[asdict(a), asdict(s.plan)],
                  quota_directory=str(s.root / "quota"), previous_create=asdict(previous),
                  previous_tracking_directory=str(s.root / "tracking"))
    path.write_text(json.dumps(config))
    s.launch["installation_digest"] = hashlib.sha256(path.read_bytes()).hexdigest()
    calls = []

    def collect(reader, fence, binding, tracking, supervisor, pins, *, empty_manifest, registered_ids):
        assert registered_ids == ("old-vm",)
        assert binding == old and tracking == s.root / "tracking" and empty_manifest
        assert (s.root / "quota" / "create-slot-1").exists()
        fence.verify_pending(old)
        calls.append(binding)
        if mode == "closeout_denied":
            raise ContractError(module.Code.DENIED)
        if mode == "changed_pin":
            path.write_bytes(b"changed")
        return CloseoutEvidence("old-vm", "known_target_absent", time.monotonic(), 42, 1,
                                hashlib.sha256(canonical(asdict(old)).encode()).hexdigest())

    if mode in {"real_collect", "missing_target", "late_alarm"}:
        # Real collector, target journal, supervisor files and platform parser;
        # only HTTP, host clock and /proc process identity remain simulated.
        monkeypatch.setattr(module, "SupervisorReader", evidence_module.SupervisorReader)
        monkeypatch.setattr(closeout_module, "verify_identity", evidence_module.verify_identity)
        if mode != "missing_target":
            record_target(s.root / "tracking", hashlib.sha256(canonical(asdict(old)).encode()).hexdigest(), "old-vm")
        if mode == "late_alarm":
            def alarm(req):
                if len(s.network) == 3:
                    (s.root / "alarm").touch(mode=0o600)

            s.hook = alarm
    else:
        monkeypatch.setattr(module, "SupervisorReader", lambda path: SimpleNamespace())
        monkeypatch.setattr(module, "collect_closeout", collect)
    if mode in {"success", "real_collect"}:
        result = module.perform(s.launch)
        assert vault.read(CreateBinding(**result["binding"])).sandbox_id == "vm"
        assert len(calls) == (2 if mode == "success" else 0)
        assert json.loads(manifest_path.read_bytes()) == manifest
        if mode == "real_collect":
            assert sum(req.method == "GET" for req in s.network) == 8
    else:
        with pytest.raises(ContractError):
            module.perform(s.launch)
    count = int(mode in {"success", "real_collect"})
    assert sum(req.method == "POST" for req in s.network) == count
    with pytest.raises(ContractError):
        module.perform(s.launch)
    assert sum(req.method == "POST" for req in s.network) == count


@pytest.mark.parametrize("name", ["launch", "installation", "request", "receipt-key", "api-key", "ca"])
def test_changed_pinned_file_blocks_without_network_or_fence(setup, name):
    s = setup
    (s.root / name).write_bytes(b"changed")
    with pytest.raises(ContractError):
        module.perform(s.launch)
    assert not s.network and not list((s.root / "fence").iterdir())


@pytest.mark.parametrize("mode", ["candidate", "boot", "platform", "root"])
def test_trust_anchor_or_host_mismatch_denied_before_network(setup, monkeypatch, mode):
    s = setup
    if mode == "candidate":
        s.launch["candidate_sha256"] = "0" * 64
    elif mode == "boot":
        monkeypatch.setattr(module, "system_clock", lambda: SimpleNamespace(boot_id="other"))
    elif mode == "platform":
        monkeypatch.setattr(module, "sys", SimpleNamespace(platform="darwin"))
    else:
        monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 1000, geteuid=lambda: 1000))
    with pytest.raises(ContractError):
        module.perform(s.launch)
    assert not s.network and not list((s.root / "fence").iterdir())


@pytest.mark.parametrize("name", ["launch", "receipt-key", "ca"])
def test_file_change_during_second_gate_stops_before_post(setup, name):
    s = setup

    def change(req):
        if len(s.network) == 4:
            (s.root / name).write_bytes(b"changed")

    s.hook = change
    with pytest.raises(ContractError):
        module.perform(s.launch)
    assert [r.method for r in s.network] == ["GET"] * 4
    assert (s.root / "fence" / "create.pending").is_file()
    assert len(list((s.root / "vault").glob("*.intent"))) == 1


def test_launcher_fixed_budget_pins_only_and_validated_result(setup, monkeypatch):
    s = setup
    result = module.perform(s.launch)
    monkeypatch.setattr(module, "sys", SimpleNamespace(executable="/trusted/python"))

    def run(command, raw, *, budget, environment):
        assert command == ["/trusted/python", "-I", "-m", "agentseek_execution.m3_create_process"]
        assert json.loads(raw) == s.launch and b"synthetic" not in raw
        assert budget == 10 and environment == {}
        return json.dumps(result).encode()

    monkeypatch.setattr(module, "run_worker", run)
    binding = module.launch_isolated(
        s.root / "launch",
        installation_digest=s.launch["installation_digest"],
        candidate_sha256=s.launch["candidate_sha256"],
    )
    assert asdict(binding) == result["binding"]
    result["installation_digest"] = "0" * 64
    with pytest.raises(ContractError):
        module.launch_isolated(
            s.root / "launch",
            installation_digest=s.launch["installation_digest"],
            candidate_sha256=s.launch["candidate_sha256"],
        )


def test_real_isolated_module_rejects_missing_installation_without_writes(tmp_path):
    # Safe on any host: no installation exists, so no network or create is possible.
    with pytest.raises(ContractError):
        module.launch_isolated(tmp_path.resolve() / "absent", installation_digest="0" * 64, candidate_sha256="0" * 64)
    assert not list(tmp_path.iterdir())
