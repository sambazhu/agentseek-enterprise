import hashlib
import json
from dataclasses import asdict, replace

import pytest

from agentseek_execution import m3_receipt_probe as module
from agentseek_execution.m3_create_fence import CreateFence
from agentseek_execution.m3_create_receipt import CreateReceiptVault
from agentseek_execution.m3_create_worker import CreatePlan
from agentseek_execution.models import ContractError, canonical
from test_m3_receipt_probe import setup, rewrite


def write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def cross(setup):
    s = setup
    vault = CreateReceiptVault(s.directory, s.source.vault_key_file.read_bytes())
    previous = vault.reserve_now(replace(s.source.create, create_token="previous", intent_sha256="0" * 64))
    vault.seal_response(previous, json.dumps(dict(s.response, sandboxID="old-vm",
                                                 trafficAccessToken="synthetic-donor-secret")).encode())
    def plan(binding):
        fields = asdict(binding)
        for key in ("intent_sha256", "approval_sha256"):
            fields.pop(key)
        return CreatePlan(**fields, endpoint="https://example.invalid")
    fence = s.root / "fence"
    fence.mkdir(mode=0o700)
    CreateFence(fence).claim(replace(s.source.create, intent_sha256="0" * 64))
    record = CreateFence._record(replace(previous, intent_sha256="0" * 64))
    archive = fence / ("create.closed-" + hashlib.sha256(record).hexdigest())
    archive.write_bytes(record)
    archive.chmod(0o600)
    pre = s.root / "pre"
    pre_sha = write(pre, {"plan": asdict(plan(s.source.create))})
    installed = dict(schema=3, previous_create=asdict(previous),
                     vault_directory=str(s.directory), receipt_key_file=str(s.source.vault_key_file),
                     precreate_file=str(pre), precreate_sha256=pre_sha,
                     approval_sha256=s.source.create.approval_sha256,
                     batch_sequence=[asdict(plan(previous)), asdict(plan(s.source.create))],
                     fence_directory=str(fence))
    successor = s.root / "successor"
    digest = write(successor, installed)
    donor = dict(create=asdict(previous), successor_file=str(successor), successor_sha256=digest)
    s.source = replace(s.source, donor=donor)
    s.config.update(schema=4, state="cross_guest", donor_ref=hashlib.sha256(canonical(donor).encode()).hexdigest())
    rewrite(s)
    s.archive, s.successor, s.installed, s.previous = archive, successor, installed, previous
    return s


def test_x1_target_and_donor_are_distinct_and_secrets_never_serialized(cross):
    s = cross
    request = module.prepare_receipted(s.path, binding=s.binding, source=s.source)
    assert request.headers["Host"] == "49983-vm.example.invalid"
    assert request.headers["e2b-traffic-access-token"] == "synthetic-donor-secret"
    assert "cube-traffic-access-token" not in request.headers
    assert "X-Access-Token" not in request.headers
    assert "synthetic-donor-secret" not in repr(request)
    assert "synthetic-donor-secret" not in canonical(s.source.wire())
    assert module.ReceiptProbeSource.from_wire(s.source.wire()) == s.source


@pytest.mark.parametrize("mode", ["archive", "current_fence", "pin", "foreign_run", "same_create",
                                      "same_target", "same_token", "receipt", "endpoint", "alias",
                                      "donor_ref", "old_schema", "sequence", "approval"])
def test_x1_invalid_provenance_is_rejected(cross, monkeypatch, mode):
    s = cross
    if mode == "archive":
        s.archive.unlink()
    elif mode == "current_fence":
        (s.archive.parent / "create.pending").write_bytes(b"invalid")
    elif mode == "pin":
        s.successor.write_bytes(b"{}")
    elif mode in {"foreign_run", "same_create"}:
        donor = dict(s.source.donor)
        donor["create"] = dict(donor["create"])
        donor["create"]["run_id" if mode == "foreign_run" else "create_token"] = "foreign" if mode == "foreign_run" else s.source.create.create_token
        s.source = replace(s.source, donor=donor)
        s.config["donor_ref"] = hashlib.sha256(canonical(donor).encode()).hexdigest()
        rewrite(s)
    elif mode in {"same_target", "same_token"}:
        original = CreateReceiptVault.read
        def changed(vault, binding):
            result = original(vault, binding)
            if binding == s.previous:
                return replace(result, **({"sandbox_id": "vm"} if mode == "same_target"
                                           else {"traffic_token": s.response["trafficAccessToken"]}))
            return result
        monkeypatch.setattr(CreateReceiptVault, "read", changed)
    elif mode == "receipt":
        for path in s.directory.glob("*.receipt"):
            path.write_bytes(b"bad")
    elif mode in {"sequence", "approval"}:
        if mode == "sequence":
            s.installed["batch_sequence"].reverse()
        else:
            s.installed["approval_sha256"] = "f" * 64
        donor = dict(s.source.donor, successor_sha256=write(s.successor, s.installed))
        s.source = replace(s.source, donor=donor)
        s.config["donor_ref"] = hashlib.sha256(canonical(donor).encode()).hexdigest()
        rewrite(s)
    else:
        key, value = {"endpoint": ("endpoint", "E2"), "alias": ("traffic_alias", "cube"),
                      "donor_ref": ("donor_ref", "0" * 64), "old_schema": ("schema", 3)}[mode]
        s.config[key] = value
        rewrite(s)
    with pytest.raises(ContractError):
        module.prepare_receipted(s.path, binding=s.binding, source=s.source)


def test_standalone_receipt_worker_cannot_send_x1(cross, monkeypatch):
    s = cross
    monkeypatch.setattr(module, "run_worker", lambda *a, **k: pytest.fail("must not send"))
    with pytest.raises(ContractError):
        module.observe_receipted_isolated(s.path, binding=s.binding, source=s.source, verified_deadline=1e12)
