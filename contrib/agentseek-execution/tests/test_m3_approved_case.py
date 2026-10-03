import hashlib
import json
import os
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from agentseek_execution import m3_approved_case as module
from agentseek_execution import m3_create_process as creating
from agentseek_execution import m3_receipt_probe as probing
from agentseek_execution.m3_auth_probe import TRAFFIC_ALIASES
from agentseek_execution.m3_create_receipt import CreateBinding, CreateReceiptVault
from agentseek_execution.m3_probe_dispatch import Binding
from agentseek_execution.m3_supervisor_snapshot import ClockSample
from agentseek_execution.m3_template_evidence import TemplatePins
from agentseek_execution.models import ContractError
from test_m3_create_process import (  # noqa: F401 -- transitive fixtures
    admission_fixture,
    installed_fixture,
    precreate_fixture,
    template_fixture,
)
from test_m3_create_process import setup as create_process_fixture  # noqa: F401 -- synthetic create installation
from test_m3_receipt_probe import setup as receipt_fixture  # noqa: F401 -- private encrypted receipt fixture


@pytest.fixture
def setup(request, monkeypatch):
    s = request.getfixturevalue("receipt_fixture")
    boot = "00000000-0000-0000-0000-000000000001"
    # Fixture's first create has a deliberately synthetic boot. Use a separate
    # canonical-clock create, retaining the original fixture's immutable files.
    create = replace(s.source.create, create_token="second", boot_id=boot, intent_sha256="0" * 64)
    vault = CreateReceiptVault(s.directory, b"k" * 32)
    create = vault.reserve_now(create)
    vault.seal_response(create, json.dumps(s.response).encode())
    s.source = replace(s.source, create=create)
    s.config["receipt_ref"] = s.source.reference()
    s.path.write_text(json.dumps(s.config))
    s.binding = replace(
        s.binding, create_token="second", boot_id=boot, config_sha256=hashlib.sha256(s.path.read_bytes()).hexdigest()
    )
    s.approval = s.root / "case-approval"
    s.pins = TemplatePins("tpl", "192.0.2.1", "artifact", s.binding.artifact_sha256)
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0))
    monkeypatch.setattr(
        module, "system_clock", lambda: ClockSample(boot, time.time(), time.monotonic(), time.monotonic())
    )
    original = module.read_approval
    monkeypatch.setattr(
        module, "read_approval", lambda *args, **kwargs: original(*args, **kwargs, owner_uid=os.getuid())
    )
    approve(s)
    return s


def approve(s, **changes):
    data = {"schema": 1, "binding": asdict(s.binding), "approved": True, "expires_epoch": time.time() + 60}
    data.update(changes)
    s.approval.write_text(json.dumps(data))
    s.approval.chmod(0o600)
    s.digest = hashlib.sha256(s.approval.read_bytes()).hexdigest()


def load(s):
    return module.load_approved_case(
        s.path,
        s.approval,
        approval_digest=s.digest,
        approval_ref=s.binding.approval_ref,
        case_id=s.binding.case_id,
        source=s.source,
        template_pins=s.pins,
    )


@pytest.mark.parametrize("alias", ["e2b", "cube"])
@pytest.mark.parametrize("state", ["correct", "wrong", "missing"])
def test_exact_existing_approval_binds_sealed_id_and_selectors_readonly(setup, alias, state):
    s = setup
    s.config.update(traffic_alias=alias, state=state)
    s.path.write_text(json.dumps(s.config))
    s.binding = replace(s.binding, config_sha256=hashlib.sha256(s.path.read_bytes()).hexdigest())
    approve(s)
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in s.root.rglob("*") if p.is_file()}
    result = load(s)
    assert result.binding == s.binding and not result.aggregate_ready
    assert 11 <= result.approval_deadline_mono - result.observed_mono <= 60
    assert "synthetic" not in repr(result)
    header = TRAFFIC_ALIASES[alias]
    if state == "correct":
        assert result.request.headers[header] == s.response["trafficAccessToken"]
    elif state == "wrong":
        assert result.request.headers[header] != s.response["trafficAccessToken"]
    else:
        assert header not in result.request.headers
    assert before == {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in s.root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("field", ["sandbox_id", "case_id", "artifact_sha256", "config_sha256", "approval_ref"])
def test_approval_binding_cannot_override_derived_receipt_case(setup, field):
    s = setup
    binding = asdict(s.binding)
    binding[field] = "f" * 64 if field.endswith("sha256") else "other"
    approve(s, binding=binding)
    with pytest.raises(ContractError):
        load(s)


@pytest.mark.parametrize("mode", ["missing", "changed", "revoked", "expired", "create_approval"])
def test_create_permission_or_invalid_case_permission_is_not_probe_permission(setup, mode):
    s = setup
    if mode == "missing":
        s.approval.unlink()
    elif mode == "changed":
        s.approval.write_text("{}")
    elif mode == "revoked":
        approve(s, approved=False)
    elif mode == "expired":
        approve(s, expires_epoch=time.time() + 10)
    else:
        approve(s, plan={}, max_creates=1, w0_accepted=True)
    with pytest.raises(ContractError):
        load(s)


def test_unknown_without_sealed_receipt_cannot_materialize_case(setup):
    s = setup
    vault = CreateReceiptVault(s.directory, b"k" * 32)
    create = vault.reserve_now(replace(s.source.create, create_token="unknown", intent_sha256="0" * 64))
    s.source = replace(s.source, create=create)
    with pytest.raises(ContractError):
        load(s)


def test_approval_revoked_during_request_preparation_blocks(setup, monkeypatch):
    s = setup
    original = module.prepare_receipted

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        s.approval.write_text("{}")
        return result

    monkeypatch.setattr(module, "prepare_receipted", prepare)
    with pytest.raises(ContractError):
        load(s)


def test_created_receipt_to_independent_approved_case_without_probe_send(request, monkeypatch):
    s = request.getfixturevalue("create_process_fixture")
    create = CreateBinding(**creating.perform(s.launch)["binding"])
    source = probing.ReceiptProbeSource(
        create, s.root / "vault", s.root / "receipt-key", s.root / "api-key", create.domain, 13080
    )
    config = {
        "schema": 3,
        "receipt_ref": source.reference(),
        "endpoint": "E2",
        "layer": "traffic",
        "state": "missing",
        "traffic_alias": "cube",
    }
    path = s.root / "case-config"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    pins = TemplatePins(**s.config["template_pins"])
    binding = Binding(
        create.run_id,
        create.create_token,
        "vm",
        create.template_id,
        pins.artifact_sha256,
        "case-e2-missing",
        hashlib.sha256(path.read_bytes()).hexdigest(),
        "case-approval",
        create.candidate_sha256,
        create.boot_id,
    )
    approval = s.root / "row-approval"
    approval.write_text(
        json.dumps({"schema": 1, "binding": asdict(binding), "approved": True, "expires_epoch": time.time() + 60})
    )
    approval.chmod(0o600)
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0))
    monkeypatch.setattr(probing, "os", SimpleNamespace(getuid=lambda: 0))
    monkeypatch.setattr(
        module, "system_clock", lambda: ClockSample(create.boot_id, time.time(), time.monotonic(), time.monotonic())
    )
    original = module.read_approval
    monkeypatch.setattr(
        module, "read_approval", lambda *args, **kwargs: original(*args, **kwargs, owner_uid=os.getuid())
    )
    result = module.load_approved_case(
        path,
        approval,
        approval_digest=hashlib.sha256(approval.read_bytes()).hexdigest(),
        approval_ref=binding.approval_ref,
        case_id=binding.case_id,
        source=source,
        template_pins=pins,
    )
    assert result.binding == binding and result.request.method == "GET" and not result.aggregate_ready
    assert not any(name in result.request.headers for name in TRAFFIC_ALIASES.values())
    assert [r.method for r in s.network] == ["GET"] * 4 + ["POST"]  # only the simulated creation
