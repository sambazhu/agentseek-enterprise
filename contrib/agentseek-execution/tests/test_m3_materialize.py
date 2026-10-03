import hashlib
import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from agentseek_execution import m3_materialize as module
from agentseek_execution.m3_platform_evidence import read_approval
from agentseek_execution.m3_probe_dispatch import Binding
from agentseek_execution.m3_receipt_probe import ReceiptProbeSource, prepare_receipted
from agentseek_execution.models import ContractError
from test_m3_receipt_probe import setup
from test_m3_cross_guest import cross


@pytest.fixture
def permit(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(module, "os", SimpleNamespace(**{k: getattr(module.os, k) for k in dir(module.os)
                                                        if not k.startswith("__")}))
    monkeypatch.setattr(module.os, "getuid", lambda: 0)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module, "system_clock", lambda: SimpleNamespace(boot_id="boot"))
    def write(path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    output = s.root / "output"
    output.mkdir(mode=0o700)
    saved = s.root / "created"
    write(saved, dict(schema=1, config_sha256="a" * 64,
                      result=dict(schema=1, binding=asdict(s.source.create), registered_and_observed=True)))
    expected = replace(s.source.create, intent_sha256="0" * 64)
    plan = asdict(expected)
    for name in ("intent_sha256", "approval_sha256"):
        plan.pop(name)
    plan["endpoint"] = "https://example.invalid"
    source = s.source.wire()
    source.pop("create")
    value = dict(schema=1, approved=True, materialize_exact_rows=True, expires_epoch=time.time() + 120,
                 slot="A", reserve_seconds=5, expected_create=asdict(expected), create_result_file=str(saved),
                 receipt_source=source, output_directory=str(output), dispatch_directory=str(s.dispatch),
                 case_ids=[f"A-{i}" for i in range(5)], approval_refs=[f"approval-{i}" for i in range(5)],
                 installation=dict(supervisor_directory=str(s.root), supervisor_identity={},
                     control=dict(endpoint=plan["endpoint"], domain=s.source.domain, proxy_port=13080,
                                  api_key=s.source.api_key_file.read_text(), ca_file=str(s.root / "ca")),
                     plan=plan, template_pins=dict(template_id="tpl", artifact_id="artifact",
                                                  node_ip="192.0.2.1", artifact_sha256="d" * 64),
                     exclusive_window={}, create_request_file=str(s.root / "request")))
    path = s.root / "permit"
    digest = write(path, value)
    return SimpleNamespace(path=path, digest=digest, value=value, output=output, write=write, saved=saved)


def test_exact_approvals_and_probes_accepted_by_existing_readers(permit):
    s = permit
    manifest = module.materialize(s.path, s.digest)
    assert manifest["rows"] == 5 and manifest["next_create_authorized"] is False
    slot = json.loads((s.output / "slot.json").read_bytes())
    for row in slot["rows"]:
        from pathlib import Path
        installed = json.loads(Path(row["installation"]).read_bytes())
        binding = Binding(**installed["binding"])
        snapshot = read_approval(Path(installed["approval_file"]), pinned_digest=row["approval_sha256"],
                                 binding=binding, now_epoch=time.time(), owner_uid=__import__("os").getuid())
        assert snapshot.binding == binding
        request = prepare_receipted(Path(installed["probe_config"]), binding=binding,
                                     source=ReceiptProbeSource.from_wire(installed["receipt_source"]))
        assert request.headers["Host"] == "49983-vm.example.invalid"
    assert "synthetic-api-key" not in json.dumps(manifest)
    with pytest.raises(ContractError):
        module.materialize(s.path, s.digest)


@pytest.mark.parametrize("mode", ["not_approved", "no_delegation", "expired", "foreign_create", "unknown_field",
                                   "duplicate_cases", "bad_pin", "different_boot", "receipt_missing"])
def test_invalid_preapproval_or_receipt_never_writes_rows(permit, monkeypatch, mode):
    s = permit
    if mode == "not_approved":
        s.value["approved"] = False
    elif mode == "no_delegation":
        s.value["materialize_exact_rows"] = False
    elif mode == "expired":
        s.value["expires_epoch"] = time.time() - 1
    elif mode == "foreign_create":
        s.value["expected_create"]["create_token"] = "foreign"
    elif mode == "unknown_field":
        s.value["replacement_token"] = "not-permitted"
    elif mode == "duplicate_cases":
        s.value["case_ids"][1] = s.value["case_ids"][0]
    elif mode == "different_boot":
        monkeypatch.setattr(module, "system_clock", lambda: SimpleNamespace(boot_id="other"))
    elif mode == "receipt_missing":
        from pathlib import Path
        for path in Path(s.value["receipt_source"]["vault_directory"]).glob("*.receipt"):
            path.unlink()
    digest = s.write(s.path, s.value)
    if mode == "bad_pin":
        digest = "f" * 64
    with pytest.raises((ContractError, FileNotFoundError)):
        module.materialize(s.path, digest)
    assert not list(s.output.iterdir())


def test_b_materialization_uses_real_donor_and_existing_probe_reader(permit, cross):
    from pathlib import Path
    s = permit
    previous = s.path.parent / "a-slot-result"
    previous_digest = s.write(previous, dict(slot="A", rows_observed=5, next_create_authorized=False,
                                           create=cross.source.donor["create"]))
    s.value.update(slot="B", donor=cross.source.donor,
                   previous_slot=dict(path=str(previous), sha256=previous_digest),
                   case_ids=[f"B-{i}" for i in range(6)], approval_refs=[f"B-approval-{i}" for i in range(6)])
    digest = s.write(s.path, s.value)
    manifest = module.materialize(s.path, digest)
    slot = json.loads(Path(manifest["slot_file"]).read_bytes())
    assert len(slot["rows"]) == 6
    installed = json.loads(Path(slot["rows"][-1]["installation"]).read_bytes())
    request = prepare_receipted(Path(installed["probe_config"]), binding=Binding(**installed["binding"]),
                                source=ReceiptProbeSource.from_wire(installed["receipt_source"]))
    assert request.headers["Host"] == "49983-vm.example.invalid"
    assert request.headers["e2b-traffic-access-token"] == "synthetic-donor-secret"


def test_partial_output_never_publishes_slot_and_cannot_resume(permit, monkeypatch):
    s = permit
    original = module._save
    def fail(fd, name, value):
        if name == "installation-1.json":
            raise OSError("synthetic write failure")
        original(fd, name, value)
    monkeypatch.setattr(module, "_save", fail)
    with pytest.raises(OSError):
        module.materialize(s.path, s.digest)
    assert (s.output / "materialize-intent.json").exists()
    assert not (s.output / "slot.json").exists()
    assert not (s.output / "materialize-result.json").exists()
    monkeypatch.setattr(module, "_save", original)
    with pytest.raises(ContractError):
        module.materialize(s.path, s.digest)


@pytest.mark.parametrize("mode", ["expired_during_write", "preapproval_changed", "receipt_output_changed"])
def test_final_recheck_withholds_slot_on_mid_generation_change(permit, monkeypatch, mode):
    s = permit
    original = module._save
    def save(fd, name, value):
        original(fd, name, value)
        if name == "installation-4.json":
            if mode == "expired_during_write":
                monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: s.value["expires_epoch"] + 1))
            elif mode == "preapproval_changed":
                s.write(s.path, dict(s.value, approved=False))
            else:
                s.write(s.saved, {})
    monkeypatch.setattr(module, "_save", save)
    with pytest.raises(ContractError):
        module.materialize(s.path, s.digest)
    assert not (s.output / "slot.json").exists()
    assert not (s.output / "materialize-result.json").exists()
