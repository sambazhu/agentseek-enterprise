"""Offline transport/approval contracts; no real Cube or credentials."""

import base64
from dataclasses import asdict, replace
import hashlib
import json
import struct
import time
from types import SimpleNamespace

import pytest

from agentseek_execution import business_cube_session as module
from agentseek_execution.business_execution import BusinessRequest
from agentseek_execution.csv_business import csv_command
from agentseek_execution.models import ContractError
from test_m3_receipt_probe import setup
from test_m3_supervisor_identity import fixture as identity_fixture


def frame(value, flag=0):
    raw = json.dumps(value).encode()
    return bytes([flag]) + struct.pack(">I", len(raw)) + raw


def stream(stdout=b"ok", code=0):
    return (frame({"event": {"start": {"pid": 1}}})
            + frame({"event": {"data": {"stdout": base64.b64encode(stdout).decode()}}})
            + frame({"event": {"end": {"exitCode": code}}}) + frame({}, 2))


def test_complete_command_stream():
    assert module.command_stdout(stream()) == "ok"


@pytest.mark.parametrize("body", [b"", b"\x00", stream()[:-5], stream(code=1),
    stream() + frame({}), frame({"event": {"end": {}}}) + frame({}, 2),
    frame({"event": {"end": {"exitCode": 0}}}) + frame({"error": {"code": "unknown"}}, 2),
    b"x" * (module.LIMIT + 1)])
def test_uncertain_stream_never_returns_success(body):
    with pytest.raises((ContractError, ValueError)):
        module.command_stdout(body)


@pytest.fixture
def plan(setup):
    data = b"group,amount\nA,1\n"
    request = BusinessRequest("request", "owner", "input", "sum")
    value = dict(schema=1, csv_approved=True, termination_approved=True, expires_epoch=time.time()+120,
        expected_create=asdict(replace(setup.source.create, intent_sha256="0"*64)),
        input_sha256=hashlib.sha256(data).hexdigest(), instruction_sha256=hashlib.sha256(b"sum").hexdigest(),
        vault_directory=str(setup.source.vault_directory), vault_key_file=str(setup.source.vault_key_file),
        control={}, supervisor_directory=str(setup.root / "supervisor"), supervisor_identity={})
    path = setup.root / "business.json"
    def write():
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = write()
    return SimpleNamespace(value=value, path=path, digest=digest, write=write, data=data,
                           request=request, binding=asdict(setup.source.create))


def test_request_and_created_binding_are_exact(plan):
    factory = module.ApprovedCsvSessionFactory(plan.path, plan.digest)
    assert factory.validate_request(plan.request, plan.data) == plan.value["expected_create"]
    assert factory(plan.binding).binding == plan.binding
    with pytest.raises(ContractError):
        factory.validate_request(replace(plan.request, instruction="other"), plan.data)
    with pytest.raises(ContractError):
        factory.validate_request(plan.request, plan.data + b"B,2\n")
    with pytest.raises(ContractError):
        factory(dict(plan.binding, run_id="other"))


@pytest.mark.parametrize("field,value", [("csv_approved", False), ("termination_approved", False),
    ("expires_epoch", 0), ("expires_epoch", float("inf")), ("schema", True)])
def test_unapproved_plan_rejected(plan, field, value):
    plan.value[field] = value
    with pytest.raises((ContractError, ValueError)):
        module.read_plan(plan.path, plan.write())


def test_worker_argv_never_contains_csv_or_credentials(monkeypatch, plan):
    stdout = json.dumps({"csv": base64.b64encode(b"group,total\nA,1\n").decode(), "groups": 1})
    calls = []
    def worker(argv, stdin, **kwargs):
        calls.append((argv, json.loads(stdin), kwargs))
        return json.dumps({"stdout": stdout}).encode()
    monkeypatch.setattr(module, "run_worker", worker)
    session = module.ApprovedCsvSession(plan.path, plan.digest, plan.binding)
    assert session.run(csv_command(plan.data), timeout=15) == stdout
    argv, payload, options = calls[0]
    assert argv[1:] == ["-I", "-m", "agentseek_execution.business_cube_session"]
    assert payload["operation"] == "run" and options == {"budget": 25, "environment": {}}
    with pytest.raises(ContractError):
        session.run("sh -c whoami", timeout=15)
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["run", "terminate"])
def test_worker_fixed_protocol_and_exact_target(monkeypatch, setup, plan, operation, identity_fixture):
    import httpx
    plan.value["control"] = dict(endpoint="https://example.invalid", api_key="synthetic",
        ca_file=str(setup.root / "ca"), domain="example.invalid", proxy_port=13080)
    pins, supervisor, _, _, _ = identity_fixture
    plan.value["supervisor_identity"] = asdict(pins)
    plan.digest = plan.write()
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0))
    vault = module.CreateReceiptVault(setup.source.vault_directory,
                                     module.config_bytes(setup.source.vault_key_file))
    sent = vault.read_intent(module.CreateBinding(**plan.binding))["sent_mono"]
    monkeypatch.setattr(module, "system_clock", lambda: SimpleNamespace(boot_id="boot", monotonic=sent))
    monkeypatch.setattr(module, "PlatformReader", lambda **kwargs: SimpleNamespace(_tls=True,
        collect_created=lambda *a: SimpleNamespace(started_epoch=time.time())))
    real_verify = module.verify_identity
    identity_calls = []
    def verify(snapshot, pins):
        assert snapshot is supervisor
        identity_calls.append(pins)
        return real_verify(snapshot, pins)
    monkeypatch.setattr(module, "verify_identity", verify)
    monkeypatch.setattr(module, "SupervisorReader", lambda path: SimpleNamespace(read=lambda **kwargs: supervisor))
    output = json.dumps({"csv": base64.b64encode(b"group,total\nA,1\n").decode(), "groups": 1})
    calls = []
    class Response:
        status_code = 200
        headers = {"Content-Type": "application/connect+json"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_raw(self, **kwargs): yield stream(output.encode())
    class Client(Response):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        def stream(self, method, url, **kwargs):
            assert method == "POST" and url == "http://127.0.0.1:13080/process.Process/Start"
            assert kwargs["headers"]["Host"] == "49983-vm.example.invalid"
            request = json.loads(kwargs["content"][5:])
            assert request["process"]["cmd"] == "python3"
            assert request["process"]["args"][2] == module.CSV_PROGRAM
            calls.append("run")
            return Response()
        def delete(self, url, **kwargs):
            assert url == "https://example.invalid/sandboxes/vm"
            calls.append("terminate")
            return Response()
    monkeypatch.setattr(httpx, "Client", Client)
    result = module.perform(dict(path=str(plan.path), sha256=plan.digest, binding=plan.binding,
        operation=operation, data=base64.b64encode(plan.data).decode() if operation == "run" else ""))
    assert calls == [operation]
    assert len(identity_calls) == (1 if operation == "run" else 0)
    assert result == ({"stdout": output} if operation == "run" else {"delete_accepted": True})
