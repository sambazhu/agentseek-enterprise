import hashlib
import json
import os
import ssl
from dataclasses import asdict, replace

import httpx
import pytest
from agentseek_execution import m3_platform_evidence as module
from agentseek_execution.m3_probe_dispatch import Binding
from agentseek_execution.models import ContractError


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "private"
    root.mkdir(mode=0o700)
    config = root / "config"
    value = {
        "schema": 1,
        "endpoint": "E1",
        "host": "49983-vm.example.invalid",
        "proxy_port": 13080,
        "credentials": {"api_key": "synthetic-key", "traffic": "traffic", "envd": "envd"},
        "layer": "envd",
        "state": "missing",
        "replacement": None,
    }
    config.write_text(json.dumps(value))
    config.chmod(0o600)
    binding = Binding(
        "run",
        "create",
        "vm",
        "tpl",
        "a" * 64,
        "case",
        hashlib.sha256(config.read_bytes()).hexdigest(),
        "approval",
        "b" * 64,
        "boot",
    )
    approval = root / "approval"
    approval.write_text(json.dumps({"schema": 1, "binding": asdict(binding), "approved": True, "expires_epoch": 1120}))
    approval.chmod(0o600)
    digest = hashlib.sha256(approval.read_bytes()).hexdigest()
    ca = root / "ca"
    ca.write_text("synthetic")
    monkeypatch.setattr(module.ssl, "create_default_context", lambda **kw: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    reader = module.PlatformReader(
        endpoint="https://control.example.invalid",
        api_key="synthetic-key",
        ca_file=ca,
        domain="example.invalid",
        proxy_port=13080,
    )
    info = {
        "sandboxID": "vm",
        "templateID": "tpl",
        "state": "running",
        "startedAt": 1000,
        "domain": "example.invalid",
        "metadata": {"agentseek_run_id": "run", "agentseek_create_token": "create"},
        "trafficAccessToken": "traffic",
        "envdAccessToken": "envd",
    }
    return config, binding, approval, digest, reader, info


def test_pinned_approval_actual_private_file(fixture):
    _, binding, path, digest, _, _ = fixture
    snapshot = module.read_approval(path, pinned_digest=digest, binding=binding, now_epoch=1000, owner_uid=os.getuid())
    assert snapshot.binding == binding and snapshot.expires_epoch == 1120


@pytest.mark.parametrize("mode", ["changed", "foreign", "expired", "public", "revoked", "duplicate"])
def test_approval_rejects(fixture, mode):
    _, binding, path, digest, _, _ = fixture
    now = 1000
    if mode == "changed":
        path.write_text(path.read_text() + " ")
    elif mode == "foreign":
        binding = replace(binding, case_id="other")
    elif mode == "expired":
        now = 1110
    elif mode == "public":
        path.chmod(0o644)
    else:
        if mode == "revoked":
            value = json.loads(path.read_text())
            value["approved"] = False
            path.write_text(json.dumps(value))
        else:
            path.write_text('{"schema":1,"schema":1}')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ContractError):
        module.read_approval(path, pinned_digest=digest, binding=binding, now_epoch=now, owner_uid=os.getuid())


class Stream(httpx.SyncByteStream):
    def __init__(self, raw):
        self.raw = raw

    def __iter__(self):
        yield self.raw


def client_mock(monkeypatch, info, *, items=None, status=200, raw=None):
    calls = []
    real_client = httpx.Client

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["X-API-Key"] == "synthetic-key"
        value = ([{"sandboxID": "vm"}] if items is None else items) if request.url.path == "/sandboxes" else info
        return httpx.Response(
            status,
            headers={"Content-Type": "application/json"},
            stream=Stream(json.dumps(value).encode() if raw is None else raw),
        )

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False and kwargs["timeout"] == 2
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "Client", client)
    return calls


def test_actual_get_paths_and_binding(fixture, monkeypatch):
    config, binding, _, _, reader, info = fixture
    calls = client_mock(monkeypatch, info)
    snapshot = reader.collect(binding, config)
    assert snapshot.aggregate_ready is False and snapshot.sandbox_id == "vm"
    assert [r.url.path for r in calls] == ["/sandboxes", "/sandboxes/vm"]
    assert "traffic" not in repr(snapshot) and "envd" not in repr(snapshot)


def test_v2_request_does_not_bypass_receipt_provenance_gate(fixture, monkeypatch):
    config, binding, _, _, reader, info = fixture
    value = json.loads(config.read_text())
    value.update(schema=2, traffic_alias="cube", layer="traffic", state="correct")
    value["credentials"]["envd"] = None
    config.write_text(json.dumps(value))
    binding = replace(binding, config_sha256=hashlib.sha256(config.read_bytes()).hexdigest())
    info.pop("trafficAccessToken")
    info["envdAccessToken"] = None
    calls = client_mock(monkeypatch, info)
    with pytest.raises(ContractError):
        reader.collect(binding, config)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("sandboxID", "other"),
        ("templateID", "other"),
        ("state", "paused"),
        ("domain", "other.invalid"),
        ("metadata", {}),
        ("envdAccessToken", None),
        ("trafficAccessToken", "foreign"),
    ],
)
def test_wrong_or_missing_live_fields_block(fixture, monkeypatch, field, value):
    config, binding, _, _, reader, info = fixture
    info[field] = value
    calls = client_mock(monkeypatch, info)
    with pytest.raises(ContractError):
        reader.collect(binding, config)
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["redirect", "empty", "multiple", "oversize", "duplicate"])
def test_bad_control_response_not_retried(fixture, monkeypatch, mode):
    config, binding, _, _, reader, info = fixture
    kwargs = {
        "redirect": {"status": 302},
        "empty": {"items": []},
        "multiple": {"items": [{}, {}]},
        "oversize": {"raw": b"x" * 65537},
        "duplicate": {"raw": b'{"x":1,"x":2}'},
    }[mode]
    calls = client_mock(monkeypatch, info, **kwargs)
    with pytest.raises(ContractError):
        reader.collect(binding, config)
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [("host", "49983-other.example.invalid"), ("proxy_port", 13081)])
def test_request_route_must_match_receipt(fixture, monkeypatch, field, value):
    config, binding, _, _, reader, info = fixture
    data = json.loads(config.read_text())
    data[field] = value
    config.write_text(json.dumps(data))
    binding = replace(binding, config_sha256=hashlib.sha256(config.read_bytes()).hexdigest())
    client_mock(monkeypatch, info)
    with pytest.raises(ContractError):
        reader.collect(binding, config)
