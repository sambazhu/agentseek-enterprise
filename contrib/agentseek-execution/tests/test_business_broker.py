import base64
from dataclasses import asdict, replace
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from agentseek_execution.business_broker import BusinessBroker, BusinessPermit, load_approved_permits
from agentseek_execution.business_execution import BusinessRequest
from agentseek_execution.business_http import validated_result, decode_wire
from agentseek_execution.csv_business import BusinessStore
from agentseek_execution.business_workspace import CsvWorkspace
from test_csv_business import SyntheticSandboxProvider


@pytest.fixture
def broker_case(tmp_path):
    tmp_path.chmod(0o700)
    store = BusinessStore(tmp_path.resolve())
    request = BusinessRequest("remote-request", "owner", "file", "sum")
    data, token = b"group,amount\nA,1\nA,2\nB,4\n", "synthetic-broker-token-" + "x" * 32
    permit = BusinessPermit(request, hashlib.sha256(data).hexdigest(), hashlib.sha256(token.encode()).hexdigest(), time.time()+120)
    provider = SyntheticSandboxProvider(store, request.owner_id)
    (tmp_path / "workspace").mkdir(mode=0o700)
    workspace = CsvWorkspace((tmp_path / "workspace").resolve())
    original_destroy = provider.destroy
    def destroy(attempt):
        outcome = store.snapshot(request)
        assert workspace.reference(request, attempt, outcome.artifact_ref)["filename"] == "summary.csv"
        return original_destroy(attempt)
    provider.destroy = destroy
    broker = BusinessBroker(permits=[permit], store=store, workspace=workspace, provider_for=lambda p: provider)
    payload = dict(operation="execute", request=asdict(request), input=base64.b64encode(data).decode())
    return SimpleNamespace(store=store, request=request, data=data, token=token, permit=permit,
                           provider=provider, broker=broker, payload=payload, path=tmp_path)


def test_whole_business_request_and_replay_are_idempotent(broker_case):
    s = broker_case
    first = s.broker.dispatch("Bearer " + s.token, s.payload)
    outcome, data = validated_result(s.request, first)
    assert outcome.state == "succeeded" and outcome.cleanup_confirmed
    assert data == b"group,total\nA,3\nB,4\n"
    assert s.broker.dispatch("Bearer " + s.token, s.payload) == first
    assert s.broker.dispatch("Bearer " + s.token, dict(s.payload, operation="result", input="")) == first
    assert s.provider.events == ["create", "execute", "destroy"]


@pytest.mark.parametrize("change", ["credential", "owner", "instruction", "input", "path", "operation"])
def test_client_cannot_expand_permit(broker_case, change):
    s = broker_case
    payload, token = dict(s.payload, request=dict(s.payload["request"])), s.token
    if change == "credential": token = "wrong" * 10
    elif change == "owner": payload["request"]["owner_id"] = "other"
    elif change == "instruction": payload["request"]["instruction"] = "shell"
    elif change == "input": payload["input"] = base64.b64encode(b"different").decode()
    elif change == "path": payload["host_path"] = "/tmp/input"
    else: payload["operation"] = "create"
    with pytest.raises(ValueError):
        s.broker.dispatch("Bearer " + token, payload)
    assert s.provider.events == []


def test_expired_permit_cannot_start_but_can_read(broker_case):
    s = broker_case
    s.broker.clock = lambda: s.permit.expires_epoch + 1
    with pytest.raises(ValueError): s.broker.dispatch("Bearer " + s.token, s.payload)
    assert s.broker.dispatch("Bearer " + s.token, dict(s.payload, operation="result", input="")) == {"state": "not_found"}
    assert not s.provider.events


def test_reservation_is_not_permission_to_restart(broker_case):
    s = broker_case
    s.store.reserve(s.request)
    outcome, data = validated_result(s.request, s.broker.dispatch("Bearer " + s.token, s.payload))
    assert outcome.state == "reconciling" and data is None
    assert not s.provider.events


@pytest.mark.parametrize("field,value", [("owner_id", "other"), ("sha256", "0"*64), ("data", "eA==")])
def test_remote_result_scope_and_bytes_checked(broker_case, field, value):
    s = broker_case
    result = s.broker.dispatch("Bearer " + s.token, s.payload)
    with pytest.raises(ValueError): validated_result(s.request, dict(result, **{field: value}))


def test_catalog_requires_pinned_explicit_approval(broker_case):
    s = broker_case
    path = s.path / "permits.json"
    value = dict(schema=1, approved=True, permits=[asdict(s.permit)])
    path.write_text(json.dumps(value)); path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert load_approved_permits(path.resolve(), digest) == (s.permit,)
    value["approved"] = False
    path.write_text(json.dumps(value))
    with pytest.raises(Exception): load_approved_permits(path.resolve(), digest)
    with pytest.raises(ValueError): load_approved_permits(path.resolve(), hashlib.sha256(path.read_bytes()).hexdigest())


def test_duplicate_json_keys_rejected():
    with pytest.raises(ValueError): decode_wire(b'{"operation":"result","operation":"execute"}')
