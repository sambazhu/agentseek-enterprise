import hashlib
import json
import os
import ssl
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agentseek_execution import m3_evidence_process as module
from agentseek_execution import m3_receipt_probe as receipt_probe
from agentseek_execution.m3_create_receipt import CreateBinding, CreateReceiptVault
from agentseek_execution.m3_lifetime_evidence import read_sealed_lifetime
from agentseek_execution.m3_probe_dispatch import Binding
from agentseek_execution.m3_supervisor_identity import ProcessIdentity
from agentseek_execution.m3_supervisor_snapshot import ClockSample, SupervisorReader
from agentseek_execution.models import ContractError


class Stream(httpx.SyncByteStream):
    def __init__(self, value):
        self.value = value

    def __iter__(self):
        yield json.dumps(self.value).encode()


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "private"
    root.mkdir(mode=0o700)

    def write(name, value):
        path = root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    boot = "00000000-0000-0000-0000-000000000001"
    probe = write(
        "probe",
        {
            "schema": 1,
            "endpoint": "E1",
            "host": "49983-vm.example.invalid",
            "proxy_port": 13080,
            "credentials": {"api_key": "synthetic-secret", "traffic": "traffic", "envd": "envd"},
            "layer": "traffic",
            "state": "missing",
            "replacement": None,
        },
    )
    binding = Binding(
        "run",
        "create",
        "vm",
        "tpl",
        "a" * 64,
        "case",
        hashlib.sha256(probe.read_bytes()).hexdigest(),
        "approval",
        "b" * 64,
        boot,
    )
    approval = write(
        "approval", {"schema": 1, "approved": True, "binding": asdict(binding), "expires_epoch": time.time() + 120}
    )
    write(
        "manifest.json",
        {
            "run_id": "run",
            "template_alias": "alias",
            "template_id": "tpl",
            "run_started_at": time.time() - 10,
            "sandboxes": {"vm": {"registered_at": time.time() - 5}},
        },
    )
    write("manifest.json.heartbeat", {"run_id": "run", "ts": time.time() - 1, "pid": 42})
    write("manifest.json.lock", {})
    ca = write("ca", "synthetic")
    intent = write(
        "create-intent",
        {
            "schema": 1,
            "run_id": "run",
            "create_token": "create",
            "boot_id": boot,
            "sent_epoch": time.time() - 10,
            "sent_mono": time.monotonic() - 10,
        },
    )
    installation = write(
        "installation",
        {
            "schema": 1,
            "binding": asdict(binding),
            "create_intent_file": str(intent),
            "create_intent_digest": hashlib.sha256(intent.read_bytes()).hexdigest(),
            "probe_config": str(probe),
            "supervisor_directory": str(root),
            "supervisor_identity": {
                "executable": str(root / "python"),
                "executable_sha256": "a" * 64,
                "script": str(root / "script"),
                "script_sha256": "b" * 64,
                "unit_file": str(root / "unit"),
                "unit_sha256": "c" * 64,
                "cmdline_sha256": "d" * 64,
            },
            "approval_file": str(approval),
            "control": {
                "endpoint": "https://control.example.invalid",
                "api_key": "synthetic-secret",
                "ca_file": str(ca),
                "domain": "example.invalid",
                "proxy_port": 13080,
            },
        },
    )
    payload = {
        "path": str(installation),
        "installation_digest": hashlib.sha256(installation.read_bytes()).hexdigest(),
        "approval_digest": hashlib.sha256(approval.read_bytes()).hexdigest(),
    }
    monkeypatch.setattr(module, "_require_root", lambda: None)
    monkeypatch.setattr(module, "verify_identity", lambda snapshot, pins: ProcessIdentity(42, 100, time.monotonic()))
    monkeypatch.setattr(
        module,
        "SupervisorReader",
        lambda path: SupervisorReader(
            path,
            owner_uid=os.getuid(),
            clock=lambda: ClockSample(boot, time.time(), time.monotonic(), time.monotonic()),
        ),
    )
    original_approval = module.read_approval
    monkeypatch.setattr(
        module, "read_approval", lambda *args, **kw: original_approval(*args, **kw, owner_uid=os.getuid())
    )
    monkeypatch.setattr(ssl, "create_default_context", lambda **kw: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    calls = []
    hooks = []

    def handle(request):
        calls.append(request)
        if hooks:
            hooks[0](request)
        value = (
            [{"sandboxID": "vm"}]
            if request.url.path == "/sandboxes"
            else {
                "sandboxID": "vm",
                "templateID": "tpl",
                "state": "running",
                "startedAt": time.time() - 9,
                "domain": "example.invalid",
                "metadata": {"agentseek_run_id": "run", "agentseek_create_token": "create"},
                "trafficAccessToken": "traffic",
                "envdAccessToken": "envd",
            }
        )
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Stream(value))

    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handle)))
    return root, payload, calls, hooks


@pytest.fixture
def receipt_fixture(fixture, monkeypatch):
    root, payload, calls, hooks = fixture
    installation = json.loads((root / "installation").read_bytes())
    binding = Binding(**installation["binding"])
    directory = root / "vault"
    directory.mkdir(mode=0o700)
    key_file = root / "receipt-key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    api_file = root / "api-key"
    api_file.write_text(installation["control"]["api_key"])
    api_file.chmod(0o600)
    vault = CreateReceiptVault(directory, b"k" * 32)
    create = vault.reserve_now(
        CreateBinding(
            binding.run_id,
            binding.create_token,
            binding.template_id,
            binding.boot_id,
            "e" * 64,
            binding.candidate_sha256,
            "f" * 64,
            "0" * 64,
            "example.invalid",
            True,
        )
    )
    vault.seal_response(
        create,
        json.dumps({
            "sandboxID": "vm",
            "templateID": "tpl",
            "domain": "example.invalid",
            "trafficAccessToken": "synthetic-traffic",
            "envdAccessToken": None,
        }).encode(),
    )
    source = receipt_probe.ReceiptProbeSource(create, directory, key_file, api_file, "example.invalid", 13080)
    (root / "probe").write_text(
        json.dumps({
            "schema": 3,
            "receipt_ref": source.reference(),
            "endpoint": "E1",
            "layer": "traffic",
            "state": "missing",
            "traffic_alias": "cube",
        })
    )
    binding = replace(binding, config_sha256=hashlib.sha256((root / "probe").read_bytes()).hexdigest())
    approval = json.loads((root / "approval").read_bytes())
    approval["binding"] = asdict(binding)
    (root / "approval").write_text(json.dumps(approval))
    payload["approval_digest"] = hashlib.sha256((root / "approval").read_bytes()).hexdigest()
    installation.update(schema=2, binding=asdict(binding), receipt_source=source.wire())
    del installation["create_intent_file"]
    del installation["create_intent_digest"]
    (root / "installation").write_text(json.dumps(installation))
    payload["installation_digest"] = hashlib.sha256((root / "installation").read_bytes()).hexdigest()
    monkeypatch.setattr(receipt_probe, "os", SimpleNamespace(getuid=lambda: 0))
    # Real v0.7.0 detail shape: no traffic token and null envd token.
    original = Stream.__iter__

    def actual_detail(stream):
        if isinstance(stream.value, dict) and "sandboxID" in stream.value:
            stream.value.pop("trafficAccessToken", None)
            stream.value["envdAccessToken"] = None
        return original(stream)

    monkeypatch.setattr(Stream, "__iter__", actual_detail)
    return root, payload, calls, hooks, vault, source, binding


def test_receipted_collector_reads_only_and_keeps_missing_gate(receipt_fixture):
    root, payload, calls, _, _, _, _ = receipt_fixture
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = module.perform(payload)
    assert result["schema"] == 2 and result["aggregate_ready"] is False
    assert result["checks"] == module.RECEIPT_CHECKS and result["missing"] == module.RECEIPT_MISSING
    assert "exclusive_window_policy" in result["missing"]
    assert "synthetic" not in json.dumps(result)
    assert [r.method for r in calls] == ["GET", "GET"]
    assert before == {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("mode", ["receipt", "key", "api", "approval", "probe"])
def test_receipted_source_changes_during_get_reject_report(receipt_fixture, mode):
    root, payload, calls, hooks, _, source, _ = receipt_fixture

    def change(request):
        if not request.url.path.endswith("/vm"):
            return
        if mode == "receipt":
            next(source.vault_directory.glob("*.receipt")).unlink()
        elif mode == "key":
            source.vault_key_file.write_bytes(b"x" * 32)
        elif mode == "api":
            source.api_key_file.write_text("other")
        else:
            (root / mode).write_text("{}")

    hooks.append(change)
    with pytest.raises(ContractError):
        module.perform(payload)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "end_shortens",
        "expired",
        "clock_jump",
        "boot",
        "future_start",
        "missing_receipt",
        "foreign_case",
        "intent_corrupt",
    ],
)
def test_sealed_lifetime_uses_original_send_time(receipt_fixture, mode):
    _, _, _, _, vault, source, binding = receipt_fixture
    intent = vault.read_intent(source.create)
    age = 110 if mode == "expired" else 10
    now = ClockSample(binding.boot_id, intent["sent_epoch"] + age, intent["sent_mono"] + age, intent["sent_mono"] + age)
    started = intent["sent_epoch"] + 1
    end = now.epoch + 20 if mode == "end_shortens" else None
    if mode == "clock_jump":
        now = replace(now, epoch=now.epoch + 2)
    elif mode == "boot":
        now = replace(now, boot_id="foreign")
    elif mode == "future_start":
        started = now.epoch + 1
    elif mode == "missing_receipt":
        next(source.vault_directory.glob("*.receipt")).unlink()
    elif mode == "foreign_case":
        binding = replace(binding, sandbox_id="other")
    elif mode == "intent_corrupt":
        next(source.vault_directory.glob("*.intent")).write_bytes(b"corrupt")
    if mode in {"normal", "end_shortens"}:
        result = read_sealed_lifetime(
            vault, create=source.create, binding=binding, now=now, started_epoch=started, end_epoch=end
        )
        assert result.remaining_seconds == pytest.approx(20 if mode == "end_shortens" else 110)
        assert not result.platform_hard_termination_proven
    else:
        with pytest.raises(ContractError):
            read_sealed_lifetime(
                vault, create=source.create, binding=binding, now=now, started_epoch=started, end_epoch=end
            )


def test_composed_real_files_and_protocol_reads_only(fixture):
    root, payload, calls, _ = fixture
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    result = module.perform(payload)
    assert result["aggregate_ready"] is False and result["missing"] == module.MISSING
    assert result["checks"] == module.CHECKS and "synthetic-secret" not in json.dumps(result)
    assert [r.method for r in calls] == ["GET", "GET"]
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}


@pytest.mark.parametrize("mode", ["installation", "approval", "extra", "alarm"])
def test_bad_preconditions_before_network(fixture, mode):
    root, payload, calls, _ = fixture
    if mode in {"installation", "approval"}:
        (root / mode).write_text((root / mode).read_text() + " ")
    elif mode == "extra":
        payload["allow"] = True
    else:
        (root / "alarm").touch()
    with pytest.raises(ContractError):
        module.perform(payload)
    assert not calls


@pytest.mark.parametrize("mode", ["approval", "installation", "probe", "alarm"])
def test_changed_during_platform_read_blocks_final_report(fixture, mode):
    root, payload, calls, hooks = fixture

    def change(request):
        if request.url.path.endswith("/vm"):
            if mode == "alarm":
                (root / "alarm").touch()
            else:
                (root / mode).write_text((root / mode).read_text() + " ")

    hooks.append(change)
    with pytest.raises(ContractError):
        module.perform(payload)
    assert len(calls) == 2


@pytest.mark.parametrize("version", [1, 2])
def test_launcher_whole_budget_and_no_secrets(fixture, monkeypatch, version):
    root, payload, _, _ = fixture
    expected = {
        "schema": version,
        "aggregate_ready": False,
        "binding_digest": "a" * 64,
        "checks": module.CHECKS if version == 1 else module.RECEIPT_CHECKS,
        "missing": module.MISSING if version == 1 else module.RECEIPT_MISSING,
    }

    def run(command, request, *, budget, environment):
        assert command == [sys.executable, "-I", "-m", "agentseek_execution.m3_evidence_process"]
        assert json.loads(request) == payload and b"synthetic-secret" not in request
        assert budget == 10 and environment == {}
        return json.dumps(expected).encode()

    monkeypatch.setattr(module, "run_worker", run)
    assert (
        module.collect_isolated(
            root / "installation",
            installation_digest=payload["installation_digest"],
            approval_digest=payload["approval_digest"],
        )
        == expected
    )


def test_ready_output_rejected(fixture, monkeypatch):
    root, payload, _, _ = fixture
    monkeypatch.setattr(
        module,
        "run_worker",
        lambda *a, **k: b'{"schema":1,"aggregate_ready":true,"binding_digest":"a","checks":[],"missing":[]}',
    )
    with pytest.raises(ContractError):
        module.collect_isolated(
            root / "installation",
            installation_digest=payload["installation_digest"],
            approval_digest=payload["approval_digest"],
        )


def test_timeout_never_retry(fixture, monkeypatch):
    root, payload, _, _ = fixture
    calls = []

    def timeout(*args, **kw):
        calls.append(1)
        raise TimeoutError

    monkeypatch.setattr(module, "run_worker", timeout)
    with pytest.raises(TimeoutError):
        module.collect_isolated(
            root / "installation",
            installation_digest=payload["installation_digest"],
            approval_digest=payload["approval_digest"],
        )
    assert len(calls) == 1


def test_supervisor_restarted_during_collection_rejected(fixture, monkeypatch):
    _, payload, calls, _ = fixture
    starts = iter([100, 101])
    monkeypatch.setattr(
        module, "verify_identity", lambda snapshot, pins: ProcessIdentity(42, next(starts), time.monotonic())
    )
    with pytest.raises(ContractError):
        module.perform(payload)
    assert len(calls) == 2


@pytest.fixture
def window_fixture(receipt_fixture, monkeypatch):
    root, payload, _calls, _hooks, _vault, source, binding = receipt_fixture
    installation = json.loads((root / "installation").read_bytes())
    plan = asdict(source.create)
    for key in ("approval_sha256", "intent_sha256"):
        plan.pop(key)
    plan["endpoint"] = installation["control"]["endpoint"]
    installation.update(schema=3, plan=plan, template_pins={
        "template_id": binding.template_id, "artifact_id": "artifact", "node_ip": "192.0.2.1",
        "artifact_sha256": binding.artifact_sha256,
    }, exclusive_window={"path": str(root / "window"), "digest": "a" * 64, "creator_id": "creator", "writers": ["other"]})
    (root / "installation").write_text(json.dumps(installation))
    payload["installation_digest"] = hashlib.sha256((root / "installation").read_bytes()).hexdigest()
    windows = []

    def read(*args, **kwargs):
        windows.append(kwargs)
        return SimpleNamespace(window_id="window", expires_mono=time.monotonic() + 60)

    monkeypatch.setattr(module, "read_window", read)
    monkeypatch.setattr(module.PlatformReader, "collect_template", lambda self, pins: (pins, time.monotonic()))
    return root, payload, windows


def test_window_and_template_checks_remain_partial_evidence(window_fixture):
    _, payload, windows = window_fixture
    result = module.perform(payload)
    assert result["schema"] == 3 and result["aggregate_ready"] is False
    assert result["checks"] == module.WINDOW_CHECKS
    assert "unified_dispatch_gate" in result["missing"]
    assert "network_mode" in result["missing"]
    assert "pinned_window_confirmations" in result["checks"]
    assert len(windows) == 2


@pytest.mark.parametrize("change", ["plan", "artifact", "stale", "revoked", "window_expired"])
def test_window_collection_rejects_drift(window_fixture, monkeypatch, change):
    root, payload, _windows = window_fixture
    config = json.loads((root / "installation").read_bytes())
    if change == "plan":
        config["plan"]["restricted"] = False
    elif change == "artifact":
        config["template_pins"]["artifact_sha256"] = "f" * 64
    elif change == "stale":
        monkeypatch.setattr(module.PlatformReader, "collect_template", lambda self, pins: (pins, time.monotonic() - 3))
    else:
        def read(*args, **kwargs):
            if change == "revoked":
                raise ContractError(module.Code.DENIED)
            return SimpleNamespace(window_id="window", expires_mono=time.monotonic() + 1)
        monkeypatch.setattr(module, "read_window", read)
    (root / "installation").write_text(json.dumps(config))
    payload["installation_digest"] = hashlib.sha256((root / "installation").read_bytes()).hexdigest()
    with pytest.raises(ContractError):
        module.perform(payload)


@pytest.mark.parametrize("change", ["none", "guest", "second_read"])
def test_network_collection_checks_twice_and_does_not_promote_readiness(window_fixture, monkeypatch, change):
    from agentseek_execution.m3_network_evidence import NetworkIntent

    root, payload, _windows = window_fixture
    config = json.loads((root / "installation").read_bytes())
    config.update(schema=4, create_request_file=str(root / "original-request"))
    (root / "installation").write_text(json.dumps(config))
    payload["installation_digest"] = hashlib.sha256((root / "installation").read_bytes()).hexdigest()
    reads = []

    def read(path, *, create, vault):
        reads.append(path)
        if change == "second_read" and len(reads) == 2:
            raise ContractError(module.Code.DENIED)
        return NetworkIntent("other" if change == "guest" else "vm", True, create.request_sha256)

    monkeypatch.setattr(module, "read_network_intent", read)
    if change != "none":
        with pytest.raises(ContractError):
            module.perform(payload)
        return
    result = module.perform(payload)
    assert len(reads) == 2
    assert result["checks"] == module.NETWORK_CHECKS
    assert result["aggregate_ready"] is False and "network_mode" in result["missing"]


@pytest.mark.parametrize("case", ["valid", "binding", "legacy", "expired_guest", "revoked_window"])
def test_live_dispatch_collector_uses_current_sources_not_report(window_fixture, monkeypatch, case):
    from agentseek_execution.m3_case_dispatch import installed_collector
    from agentseek_execution.m3_network_evidence import NetworkIntent

    root, payload, windows = window_fixture
    config = json.loads((root / "installation").read_bytes())
    binding = Binding(**config["binding"])
    config.update(schema=4, create_request_file=str(root / "request"))
    if case == "legacy":
        config["schema"] = 3
        del config["create_request_file"]
    (root / "installation").write_text(json.dumps(config))
    payload["installation_digest"] = hashlib.sha256((root / "installation").read_bytes()).hexdigest()
    monkeypatch.setattr(module, "read_network_intent", lambda *a, **kw: NetworkIntent("vm", True, "f" * 64))
    if case == "binding":
        binding = replace(binding, sandbox_id="other")
    elif case == "expired_guest":
        monkeypatch.setattr(module, "read_sealed_lifetime", lambda *a, **kw: SimpleNamespace(stop_mono=time.monotonic() + 1))
    elif case == "revoked_window":
        def revoked(*a, **kw):
            raise ContractError(module.Code.DENIED)
        monkeypatch.setattr(module, "read_window", revoked)
    collector = installed_collector(Path(payload["path"]), installation_digest=payload["installation_digest"], approval_digest=payload["approval_digest"])
    if case != "valid":
        with pytest.raises(ContractError):
            collector(binding)
        return
    result = collector(binding)
    assert result.coordination_basis == "approved_window" and result.capacity_one is False
    assert result.window_accepted is True
    assert result.verify(binding, time.monotonic()) > time.monotonic() + 11
    assert len(windows) == 2
    assert module.perform(payload)["aggregate_ready"] is False
    # Exercise the real durable dispatcher with the real collector. Only the
    # outbound data-plane worker is substituted; no sandbox request is sent.
    from agentseek_execution.m3_probe_dispatch import DispatchDirectory
    from agentseek_execution.m3_receipt_probe import ReceiptProbeSource

    sent = []
    observation = {"status": None, "body_size": 0, "body_sha256": None, "complete": False,
                   "reason": "transport_error", "protocol": None}
    monkeypatch.setattr(receipt_probe, "observe_receipted_isolated", lambda *a, **kw: sent.append(kw) or observation)
    directory = DispatchDirectory(root)
    source = ReceiptProbeSource.from_wire(config["receipt_source"])
    def collect():
        return collector(binding)
    assert directory.dispatch_receipted(binding, root / "probe", source=source, collect=collect) == observation
    with pytest.raises(ContractError):
        directory.dispatch_receipted(binding, root / "probe", source=source, collect=collect)
    assert len(sent) == 1
