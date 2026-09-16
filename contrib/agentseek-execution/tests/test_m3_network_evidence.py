import hashlib
import json
from dataclasses import replace

import pytest
from agentseek_execution import m3_network_evidence as module
from agentseek_execution.m3_create_receipt import CreateBinding, CreateReceiptVault
from agentseek_execution.models import ContractError


def fixture(tmp_path, request, restricted=True):
    root = tmp_path.resolve() / "private"
    root.mkdir(mode=0o700)
    path = root / "request"
    path.write_text(json.dumps(request))
    path.chmod(0o600)
    vault = CreateReceiptVault(root, b"k" * 32)
    create = vault.reserve_now(CreateBinding(
        "run", "create", "tpl", "boot", "a" * 64, "b" * 64,
        hashlib.sha256(path.read_bytes()).hexdigest(), "0" * 64, "example.invalid", restricted,
    ))
    vault.seal_response(create, json.dumps({
        "sandboxID": "vm", "templateID": "tpl", "domain": "example.invalid",
        "trafficAccessToken": "synthetic-token", "envdAccessToken": None,
    }).encode())
    return path, vault, create


def request(public=False):
    return {"templateID": "tpl", "metadata": {"agentseek_run_id": "run", "agentseek_create_token": "create"},
            "network": {"allowPublicTraffic": public}}


@pytest.mark.parametrize("restricted", [True, False])
def test_exact_network_intent_does_not_claim_enforcement(tmp_path, restricted):
    path, vault, create = fixture(tmp_path, request(not restricted), restricted)
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    result = module.read_network_intent(path, create=create, vault=vault)
    assert result.sandbox_id == "vm" and result.restricted is restricted
    assert result.enforcement_verified is False and result.aggregate_ready is False
    assert "synthetic-token" not in repr(result)
    assert before == {p.name: p.read_bytes() for p in path.parent.iterdir()}


@pytest.mark.parametrize("value", [None, 0, 1, "false", True])
def test_non_boolean_or_opposite_mode_rejected_even_with_matching_digest(tmp_path, value):
    path, vault, create = fixture(tmp_path, request(value))
    with pytest.raises(ContractError):
        module.read_network_intent(path, create=create, vault=vault)


@pytest.mark.parametrize("change", ["digest", "metadata", "template", "missing", "egress_only"])
def test_altered_or_unbound_network_request_rejected(tmp_path, change):
    value = request()
    if change == "metadata":
        value["metadata"]["agentseek_run_id"] = "other"
    elif change == "template":
        value["templateID"] = "other"
    elif change == "missing":
        value.pop("network")
    elif change == "egress_only":
        value["network"] = {"allowInternetAccess": False}
    path, vault, create = fixture(tmp_path, value)
    if change == "digest":
        path.write_text(json.dumps(request(True)))
    with pytest.raises(ContractError):
        module.read_network_intent(path, create=create, vault=vault)


def test_unknown_create_cannot_supply_network_evidence(tmp_path):
    path, vault, create = fixture(tmp_path, request())
    unknown = vault.reserve_now(replace(create, create_token="unknown", intent_sha256="0" * 64))
    with pytest.raises(ContractError):
        module.read_network_intent(path, create=unknown, vault=vault)
