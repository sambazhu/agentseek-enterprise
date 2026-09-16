import hashlib
import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from agentseek_execution import m3_receipt_probe as module
from agentseek_execution.m3_auth_probe import TRAFFIC_ALIASES, Observation
from agentseek_execution.m3_create_receipt import CreateBinding, CreateReceiptVault
from agentseek_execution.m3_probe_dispatch import Binding, DispatchDirectory, Evidence
from agentseek_execution.m3_probe_process import prepare
from agentseek_execution.models import Code, ContractError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    root.chmod(0o700)
    directory = root / "vault"
    directory.mkdir(mode=0o700)
    dispatch = root / "dispatch"
    dispatch.mkdir(mode=0o700)
    key_path = root / "key"
    key_path.write_bytes(b"k" * 32)
    key_path.chmod(0o600)
    api_path = root / "api-key"
    api_path.write_bytes(b"synthetic-api-key")
    api_path.chmod(0o600)
    vault = CreateReceiptVault(directory, b"k" * 32)
    create = CreateBinding(
        "run", "create", "tpl", "boot", "a" * 64, "b" * 64, "c" * 64, "0" * 64, "example.invalid", True
    )
    create = vault.reserve_now(create)
    response = {
        "sandboxID": "vm",
        "templateID": "tpl",
        "domain": "example.invalid",
        "trafficAccessToken": "synthetic-traffic-secret",
        "envdAccessToken": None,
    }
    vault.seal_response(create, json.dumps(response).encode())
    source = module.ReceiptProbeSource(create, directory, key_path, api_path, "example.invalid", 13080)
    config = {
        "schema": 3,
        "receipt_ref": source.reference(),
        "endpoint": "E1",
        "layer": "traffic",
        "state": "correct",
        "traffic_alias": "e2b",
    }
    path = root / "config"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    binding = Binding(
        "run",
        "create",
        "vm",
        "tpl",
        "d" * 64,
        "case",
        hashlib.sha256(path.read_bytes()).hexdigest(),
        "case-approval",
        "b" * 64,
        "boot",
    )
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 0))
    return SimpleNamespace(
        root=root,
        directory=directory,
        dispatch=dispatch,
        source=source,
        path=path,
        config=config,
        binding=binding,
        response=response,
    )


def rewrite(s):
    s.path.write_text(json.dumps(s.config))
    s.binding = replace(s.binding, config_sha256=hashlib.sha256(s.path.read_bytes()).hexdigest())


@pytest.mark.parametrize("endpoint", ["E1", "E2", "E3"])
@pytest.mark.parametrize("alias", ["e2b", "cube"])
@pytest.mark.parametrize("state", ["correct", "wrong", "missing"])
def test_receipt_only_matrix(setup, endpoint, alias, state):
    s = setup
    s.config.update(endpoint=endpoint, traffic_alias=alias, state=state)
    rewrite(s)
    result = module.prepare_receipted(s.path, binding=s.binding, source=s.source)
    assert result.headers["Host"] == "49983-vm.example.invalid"
    assert result.headers["X-API-Key"] == s.source.api_key_file.read_text()
    assert "X-Access-Token" not in result.headers
    header = TRAFFIC_ALIASES[alias]
    other = TRAFFIC_ALIASES["cube" if alias == "e2b" else "e2b"]
    assert other not in result.headers
    if state == "missing":
        assert header not in result.headers
    elif state == "correct":
        assert result.headers[header] == s.response["trafficAccessToken"]
    else:
        again = module.prepare_receipted(s.path, binding=s.binding, source=s.source)
        assert result.headers[header] != s.response["trafficAccessToken"]
        assert result.headers[header] != again.headers[header]
    assert "synthetic" not in repr(result)
    assert "synthetic" not in s.path.read_text()


@pytest.mark.parametrize(
    "key,value",
    [
        ("credentials", {}),
        ("host", "other.invalid"),
        ("replacement", "supplied-secret"),
        ("source", {}),
        ("api_key", "supplied-secret"),
        ("proxy_port", 1234),
        ("state", "cross_guest"),
        ("layer", "envd"),
        ("receipt_ref", "0" * 64),
        ("schema", 2),
        ("traffic_alias", "both"),
        ("endpoint", "E4"),
    ],
)
def test_untrusted_config_overrides_rejected(setup, key, value):
    s = setup
    s.config[key] = value
    rewrite(s)
    with pytest.raises(ContractError):
        module.prepare_receipted(s.path, binding=s.binding, source=s.source)


@pytest.mark.parametrize("key", ["run_id", "create_token", "template_id", "sandbox_id", "boot_id", "candidate_sha256"])
def test_case_must_match_trusted_create_identity(setup, key):
    s = setup
    value = "f" * 64 if key.endswith("sha256") else "foreign"
    with pytest.raises(ContractError):
        module.prepare_receipted(s.path, binding=replace(s.binding, **{key: value}), source=s.source)


@pytest.mark.parametrize(
    "mode",
    ["wrong_key", "missing", "corrupt", "key_permissions", "api_permissions", "domain", "nonroot", "config_changed"],
)
def test_untrusted_storage_and_installation_fail_closed(setup, monkeypatch, mode):
    s = setup
    if mode == "wrong_key":
        s.source.vault_key_file.write_bytes(b"x" * 32)
    elif mode == "missing":
        next(s.directory.glob("*.receipt")).unlink()
    elif mode == "corrupt":
        next(s.directory.glob("*.receipt")).write_bytes(b"corrupt")
    elif mode in {"key_permissions", "api_permissions"}:
        (s.source.vault_key_file if mode == "key_permissions" else s.source.api_key_file).chmod(0o644)
    elif mode == "domain":
        s.source = replace(s.source, domain="foreign.invalid")
    elif mode == "nonroot":
        monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 10001))
    else:
        s.path.write_text("{}")
    with pytest.raises(ContractError) as error:
        module.prepare_receipted(s.path, binding=s.binding, source=s.source)
    assert "synthetic" not in str(error.value)


def test_legacy_prepare_cannot_interpret_receipt_schema(setup):
    with pytest.raises(ContractError):
        prepare(setup.path)


@pytest.mark.parametrize("state", ["correct", "wrong", "missing"])
def test_public_receipt_without_token_is_not_a_reference(setup, state):
    s = setup
    vault = CreateReceiptVault(s.directory, s.source.vault_key_file.read_bytes())
    create = vault.reserve_now(replace(s.source.create, create_token="public-create", restricted=False))
    response = dict(s.response)
    response.pop("trafficAccessToken")
    vault.seal_response(create, json.dumps(response).encode())
    s.source = replace(s.source, create=create)
    s.binding = replace(s.binding, create_token=create.create_token)
    s.config.update(receipt_ref=s.source.reference(), state=state)
    rewrite(s)
    with pytest.raises(ContractError):
        module.prepare_receipted(s.path, binding=s.binding, source=s.source)


def test_random_collision_never_sends_correct_token_as_wrong(setup, monkeypatch):
    s = setup
    s.config["state"] = "wrong"
    rewrite(s)
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _: s.response["trafficAccessToken"])
    with pytest.raises(ContractError):
        module.prepare_receipted(s.path, binding=s.binding, source=s.source)


def observation():
    return Observation(None, 0, None, False, "transport_error", None)


def test_child_reloads_receipt_and_parent_passes_only_references(setup, monkeypatch):
    s = setup
    sent = []
    monkeypatch.setattr(module, "observe", lambda req, **kw: sent.append(req) or observation())

    def run(command, raw, *, budget, environment):
        assert command[-1] == "agentseek_execution.m3_receipt_probe"
        assert "-I" in command and budget == 10 and environment == {}
        assert b"synthetic" not in raw and b"kkkkkkkk" not in raw
        value = json.loads(raw)
        return json.dumps(module.perform(value)).encode()

    monkeypatch.setattr(module, "run_worker", run)
    result = module.observe_receipted_isolated(
        s.path, binding=s.binding, source=s.source, verified_deadline=time.monotonic() + 30
    )
    assert result == asdict(observation()) and len(sent) == 1


@pytest.mark.parametrize("mode", ["config", "receipt", "deadline"])
def test_child_rechecks_after_parent_preparation(setup, monkeypatch, mode):
    s = setup
    module.prepare_receipted(s.path, binding=s.binding, source=s.source)
    sent = []
    monkeypatch.setattr(module, "observe", lambda req, **kw: sent.append(req) or observation())
    value = {
        "schema": 1,
        "path": str(s.path),
        "binding": asdict(s.binding),
        "source": s.source.wire(),
        "deadline": time.monotonic() + 30,
    }
    if mode == "config":
        s.path.write_text("{}")
    elif mode == "receipt":
        next(s.directory.glob("*.receipt")).unlink()
    else:
        value["deadline"] = time.monotonic()
    with pytest.raises(ContractError):
        module.perform(value)
    assert not sent


def test_child_disk_work_cannot_consume_send_budget(setup, monkeypatch):
    s = setup
    original = module.prepare_receipted
    clock = [time.monotonic()]
    sent = []
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def slow(*args, **kwargs):
        request = original(*args, **kwargs)
        clock[0] += 25
        return request

    monkeypatch.setattr(module, "prepare_receipted", slow)
    monkeypatch.setattr(module, "observe", lambda *args, **kwargs: sent.append(1) or observation())
    with pytest.raises(ContractError):
        module.perform({
            "schema": 1,
            "path": str(s.path),
            "binding": asdict(s.binding),
            "source": s.source.wire(),
            "deadline": clock[0] + 30,
        })
    assert not sent


@pytest.mark.parametrize("mode", ["success", "revoked", "child_failed"])
def test_dispatch_retains_same_case_at_most_once(setup, monkeypatch, mode):
    s = setup
    gates = []
    sends = []

    def collect():
        gates.append(1)
        now = time.monotonic()
        approved = not (mode == "revoked" and len(gates) == 2)
        return Evidence(s.binding, s.binding, now, now, now + 30, now + 30, "run", "boot", True, approved, True, 1)

    def send(*args, **kwargs):
        assert list(s.dispatch.glob("*.intent"))
        sends.append(1)
        if mode == "child_failed":
            raise ContractError(Code.UNKNOWN)
        return asdict(observation())

    monkeypatch.setattr(module, "observe_receipted_isolated", send)
    dispatcher = DispatchDirectory(s.dispatch)
    if mode == "success":
        assert dispatcher.dispatch_receipted(s.binding, s.path, source=s.source, collect=collect) == asdict(
            observation()
        )
        assert dispatcher.inspect(s.binding)["state"] == "observed"
    else:
        with pytest.raises(ContractError):
            dispatcher.dispatch_receipted(s.binding, s.path, source=s.source, collect=collect)
        assert dispatcher.inspect(s.binding)["state"] == "unknown"
    count = len(sends)
    with pytest.raises(ContractError):
        dispatcher.dispatch_receipted(s.binding, s.path, source=s.source, collect=collect)
    assert len(sends) == count
