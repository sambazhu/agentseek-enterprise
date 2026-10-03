import hashlib
import json
import ssl
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import httpx
import pytest
from agentseek_execution import m3_create_worker as module
from agentseek_execution.m3_batch_quota import BatchQuota
from agentseek_execution.m3_closeout_evidence import CloseoutEvidence
from agentseek_execution.m3_create_fence import CreateFence
from agentseek_execution.m3_create_receipt import CreateReceiptVault
from agentseek_execution.m3_platform_evidence import PlatformReader
from agentseek_execution.models import ContractError, canonical


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    root.chmod(0o700)
    directory = root / "vault"
    directory.mkdir(mode=0o700)
    vault = CreateReceiptVault(directory, b"k" * 32)
    fence_directory = root / "create-fence"
    fence_directory.mkdir(mode=0o700)
    request = json.dumps({
        "templateID": "tpl",
        "metadata": {"agentseek_run_id": "run", "agentseek_create_token": "create"},
        "network": {"allowPublicTraffic": False},
    }).encode()
    plan = module.CreatePlan(
        "run",
        "create",
        "tpl",
        "boot",
        "c" * 64,
        hashlib.sha256(request).hexdigest(),
        "example.invalid",
        True,
        "https://control.example.invalid",
    )
    approval = {
        "schema": 1,
        "plan": asdict(plan),
        "approved": True,
        "max_creates": 1,
        "w0_accepted": True,
        "expires_epoch": time.time() + 100,
    }
    path = root / "approval"
    raw = json.dumps(approval).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    ca = root / "ca"
    ca.touch()
    tls = ssl.create_default_context()
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 0))
    monkeypatch.setattr(ssl, "create_default_context", lambda **kw: tls)
    state = {
        "calls": [],
        "gates": 0,
        "status": 201,
        "headers": {},
        "response": {
            "sandboxID": "vm",
            "templateID": "tpl",
            "domain": "example.invalid",
            "trafficAccessToken": "synthetic-private-token",
            "envdAccessToken": None,
        },
        "items": [{"sandboxID": "vm"}],
        "detail": {
            "sandboxID": "vm",
            "templateID": "tpl",
            "state": "running",
            "metadata": {"agentseek_run_id": "run", "agentseek_create_token": "create"},
            "startedAt": "2026-09-09T14:00:00.123456789Z",
            "envdAccessToken": None,
        },
    }

    def handler(req):
        state["calls"].append(req)
        if req.method == "GET":
            data = state["items"] if req.url.path == "/sandboxes" else state["detail"]
            return httpx.Response(
                200, stream=httpx.ByteStream(json.dumps(data).encode()), headers={"Content-Type": "application/json"}
            )
        assert list(directory.glob("*.intent")) and state["gates"] == 2
        assert req.content == request
        assert req.headers["X-API-Key"] == "synthetic-api-key"
        if state.get("network_fail"):
            raise httpx.ReadError("synthetic-private-token")
        body = state.get("body", json.dumps(state["response"]).encode())
        return httpx.Response(
            state["status"],
            stream=httpx.ByteStream(body),
            headers={"Content-Type": "application/json", **state["headers"]},
        )

    def transport(**kwargs):
        assert kwargs == {"verify": tls, "retries": 0, "trust_env": False}
        return httpx.MockTransport(handler)

    monkeypatch.setattr(httpx, "HTTPTransport", transport)

    def gate():
        state["gates"] += 1
        if state.get("fail_gate") == state["gates"]:
            raise ValueError("synthetic-private-token")
        return time.monotonic() + 30

    kwargs = {
        "plan": plan,
        "approval_path": path,
        "approval_digest": hashlib.sha256(raw).hexdigest(),
        "installed_candidate_sha256": "c" * 64,
        "current_boot_id": "boot",
        "api_key": "synthetic-api-key",
        "ca_file": ca,
        "vault": vault,
        "fence": CreateFence(fence_directory),
        "collect_admission": gate,
    }
    return SimpleNamespace(
        root=root,
        directory=directory,
        vault=vault,
        request=request,
        plan=plan,
        approval=approval,
        path=path,
        kwargs=kwargs,
        state=state,
    )


def test_one_shot_then_sealed_identity_read_without_detail_tokens(setup):
    s = setup
    worker = module.CreateWorker(**s.kwargs)
    binding = worker.create(s.request)
    assert binding.intent_sha256 != "0" * 64
    assert s.vault.read(binding).traffic_token == s.state["response"]["trafficAccessToken"]
    assert "synthetic" not in repr(binding)
    reader = PlatformReader(
        endpoint=s.plan.endpoint,
        api_key="synthetic-api-key",
        ca_file=s.kwargs["ca_file"],
        domain=s.plan.domain,
        proxy_port=13080,
    )
    result = reader.collect_created(binding, s.vault)
    assert result.sandbox_id == "vm" and result.platform_count == 1 and not result.aggregate_ready
    assert [r.method for r in s.state["calls"]] == ["POST", "GET", "GET"]
    with pytest.raises(ContractError):
        worker.create(s.request)
    assert len(s.state["calls"]) == 3


@pytest.mark.parametrize("failure", [None, "first_gate", "fence", "intent", "second_gate", "network"])
def test_batch_quota_precedes_fence_and_survives_downstream_failure(setup, monkeypatch, failure):
    s = setup
    directory = s.root / "quota"
    directory.mkdir(mode=0o700)
    sequence = (s.plan, replace(s.plan, create_token="next"))
    s.kwargs["batch_quota"] = BatchQuota(directory, sequence)
    slot = directory / "create-slot-0"
    original_claim = s.kwargs["fence"].claim

    def claim(binding):
        assert slot.is_file()
        assert s.state["calls"] == []
        if failure == "fence":
            raise OSError
        original_claim(binding)

    monkeypatch.setattr(s.kwargs["fence"], "claim", claim)
    if failure in {"first_gate", "second_gate"}:
        s.state["fail_gate"] = 1 if failure == "first_gate" else 2
    elif failure == "network":
        s.state["network_fail"] = True
    elif failure == "intent":
        def fail_intent(binding):
            assert slot.is_file()
            raise OSError

        monkeypatch.setattr(s.vault, "reserve_now", fail_intent)
    worker = module.CreateWorker(**s.kwargs)
    if failure is None:
        worker.create(s.request)
    else:
        with pytest.raises(ContractError):
            worker.create(s.request)
    assert slot.exists() is (failure != "first_gate")
    assert len(s.state["calls"]) == int(failure in {None, "network"})
    if failure != "first_gate":
        # Reopening the quota and worker cannot refund or resend the attempt.
        s.kwargs["batch_quota"] = BatchQuota(directory, sequence)
        with pytest.raises(ContractError):
            module.CreateWorker(**s.kwargs).create(s.request)
        assert len(s.state["calls"]) == int(failure in {None, "network"})


@pytest.mark.parametrize("mode", ["success", "closeout", "second_gate", "network", "missing_predecessor"])
def test_successor_worker_composes_quota_handoff_and_one_post(setup, mode):
    s = setup
    a = replace(s.plan, create_token="previous")
    old = a.binding("a" * 64)
    previous = s.vault.reserve_now(old)
    s.vault.seal_response(previous, json.dumps(s.state["response"]).encode())
    s.kwargs["fence"].claim(old)
    directory = s.root / "quota"
    directory.mkdir(mode=0o700)
    quota = BatchQuota(directory, (a, s.plan))
    quota.reserve(a)
    s.kwargs["batch_quota"] = quota
    digest = hashlib.sha256(canonical(asdict(old)).encode()).hexdigest()

    def closeout():
        assert (directory / "create-slot-1").is_file()
        assert not s.state["calls"]
        return CloseoutEvidence("vm", "exact_running_observed" if mode == "closeout" else "known_target_absent",
                                time.monotonic(), 42, 1, digest)

    if mode != "missing_predecessor":
        s.kwargs.update(previous_create=previous, collect_closeout=closeout)
    if mode == "second_gate":
        s.state["fail_gate"] = 2
    elif mode == "network":
        s.state["network_fail"] = True
    if mode == "success":
        result = module.CreateWorker(**s.kwargs).create(s.request)
        assert s.vault.read(result).sandbox_id == "vm"
    else:
        with pytest.raises(ContractError):
            module.CreateWorker(**s.kwargs).create(s.request)
    count = int(mode in {"success", "network"})
    assert len(s.state["calls"]) == count
    with pytest.raises(ContractError):
        module.CreateWorker(**s.kwargs).create(s.request)
    assert len(s.state["calls"]) == count
    assert (directory / "create-slot-1").exists() is (mode != "missing_predecessor")


def test_existing_fence_blocks_new_token_before_receipt_reservation_or_http(setup):
    s = setup
    s.kwargs["fence"].claim(replace(s.plan.binding(s.kwargs["approval_digest"]), create_token="previous-unknown"))
    with pytest.raises(ContractError):
        module.CreateWorker(**s.kwargs).create(s.request)
    assert not s.state["calls"] and not list(s.directory.iterdir())


def test_failure_between_fence_and_receipt_reservation_blocks_all_later_creates(setup, monkeypatch):
    s = setup

    def fail(*args):
        raise OSError

    monkeypatch.setattr(s.vault, "reserve_now", fail)
    with pytest.raises(ContractError):
        module.CreateWorker(**s.kwargs).create(s.request)
    assert (s.root / "create-fence" / "create.pending").is_file()
    assert not s.state["calls"] and not list(s.directory.iterdir())
    with pytest.raises(ContractError):
        s.kwargs["fence"].claim(replace(s.plan.binding(s.kwargs["approval_digest"]), create_token="new"))


def test_approval_revoked_during_second_collector_burns_without_send(setup):
    s = setup
    original = s.kwargs["collect_admission"]

    def gate():
        deadline = original()
        if s.state["gates"] == 2:
            s.path.write_text("{}")
        return deadline

    s.kwargs["collect_admission"] = gate
    with pytest.raises(ContractError):
        module.CreateWorker(**s.kwargs).create(s.request)
    assert not s.state["calls"] and list(s.directory.glob("*.intent"))


@pytest.mark.parametrize(
    "kind",
    ["digest", "expired", "w0", "count", "plan", "mode", "metadata", "request", "candidate", "boot", "nonroot", "gate"],
)
def test_pre_send_rejection_has_no_network_or_reservation(setup, monkeypatch, kind):
    s = setup
    if kind == "digest":
        s.path.write_text("{}")
    elif kind in {"expired", "w0", "count", "plan"}:
        field, value = {
            "expired": ("expires_epoch", 0),
            "w0": ("w0_accepted", False),
            "count": ("max_creates", 2),
            "plan": ("plan", {}),
        }[kind]
        s.approval[field] = value
        raw = json.dumps(s.approval).encode()
        s.path.write_bytes(raw)
        s.kwargs["approval_digest"] = hashlib.sha256(raw).hexdigest()
    elif kind in {"mode", "metadata"}:
        data = json.loads(s.request)
        data["network" if kind == "mode" else "metadata"] = {}
        s.request = json.dumps(data).encode()
        s.kwargs["plan"] = replace(s.plan, request_sha256=hashlib.sha256(s.request).hexdigest())
    elif kind == "request":
        s.request = b"{}"
    elif kind == "candidate":
        s.kwargs["installed_candidate_sha256"] = "d" * 64
    elif kind == "boot":
        s.kwargs["current_boot_id"] = "other"
    elif kind == "nonroot":
        monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 123))
    else:
        s.state["fail_gate"] = 1
    with pytest.raises(ContractError):
        module.CreateWorker(**s.kwargs).create(s.request)
    assert not s.state["calls"] and not list(s.directory.iterdir())


@pytest.mark.parametrize(
    "kind",
    ["gate", "network", "redirect", "status", "encoding", "type", "oversize", "duplicate_json", "bad_response", "seal"],
)
def test_uncertain_results_burn_slot_and_never_retry(setup, monkeypatch, kind):
    s = setup
    if kind == "gate":
        s.state["fail_gate"] = 2
    elif kind == "network":
        s.state["network_fail"] = True
    elif kind in {"redirect", "status"}:
        s.state["status"] = 302 if kind == "redirect" else 503
        s.state["headers"]["Location"] = "https://untrusted.invalid"
    elif kind in {"encoding", "type"}:
        s.state["headers"]["Content-Encoding" if kind == "encoding" else "Content-Type"] = "unsupported"
    elif kind == "oversize":
        s.state["body"] = b"x" * 65537
    elif kind == "duplicate_json":
        s.state["body"] = b'{"sandboxID":"vm","sandboxID":"other"}'
    elif kind == "bad_response":
        s.state["response"]["templateID"] = "foreign"
    else:

        def fail(*args):
            raise OSError("synthetic-private-token")

        monkeypatch.setattr(s.vault, "seal_response", fail)
    worker = module.CreateWorker(**s.kwargs)
    with pytest.raises(ContractError) as error:
        worker.create(s.request)
    assert "synthetic" not in str(error.value)
    assert list(s.directory.glob("*.intent")) and not list(s.directory.glob("*.receipt"))
    count = len(s.state["calls"])
    with pytest.raises(ContractError):
        worker.create(s.request)
    assert len(s.state["calls"]) == count


@pytest.mark.parametrize("kind", ["id", "template", "marker", "state", "domain", "started", "count"])
def test_sealed_identity_reader_rejects_mismatched_platform(setup, kind):
    s = setup
    binding = module.CreateWorker(**s.kwargs).create(s.request)
    if kind == "count":
        s.state["items"] *= 2
    else:
        field, value = {
            "id": ("sandboxID", "other"),
            "template": ("templateID", "other"),
            "marker": ("metadata", {}),
            "state": ("state", "paused"),
            "domain": ("domain", "foreign.invalid"),
            "started": ("startedAt", None),
        }[kind]
        s.state["detail"][field] = value
    reader = PlatformReader(
        endpoint=s.plan.endpoint,
        api_key="synthetic-api-key",
        ca_file=s.kwargs["ca_file"],
        domain=s.plan.domain,
        proxy_port=13080,
    )
    with pytest.raises(ContractError):
        reader.collect_created(binding, s.vault)
