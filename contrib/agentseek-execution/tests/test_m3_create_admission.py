import hashlib
import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import httpx
import pytest
from agentseek_execution import m3_create_admission as module
from agentseek_execution import m3_create_watchdog as watchdog
from agentseek_execution import m3_exclusive_window as window_module
from agentseek_execution.m3_create_fence import CreateFence
from agentseek_execution.m3_create_receipt import CreateReceiptVault
from agentseek_execution.m3_create_worker import CreatePlan, CreateWorker
from agentseek_execution.models import ContractError, canonical
from test_m3_precreate_process import installed_fixture, template_fixture  # noqa: F401 -- transitive pytest fixtures
from test_m3_precreate_process import setup as precreate_fixture  # noqa: F401 -- private files and synthetic HTTP


@pytest.fixture
def setup(request, monkeypatch):
    root, payload, calls, hooks, state = request.getfixturevalue("precreate_fixture")
    template, pins = request.getfixturevalue("template_fixture")
    config = json.loads((root / "installation").read_bytes())
    body = canonical({
        "templateID": "tpl",
        "metadata": {"agentseek_run_id": "run", "agentseek_create_token": "create"},
        "network": {"allowPublicTraffic": False},
    }).encode()
    config["plan"]["request_sha256"] = hashlib.sha256(body).hexdigest()
    approval = json.loads((root / "approval").read_bytes())
    approval["plan"] = config["plan"]
    (root / "approval").write_text(json.dumps(approval))
    payload["approval_digest"] = hashlib.sha256((root / "approval").read_bytes()).hexdigest()
    now = time.time()
    record = {
        "schema": 1,
        "window_id": "window",
        "run_id": "run",
        "boot_id": config["plan"]["boot_id"],
        "candidate_sha256": config["candidate_sha256"],
        "creator_id": "m3-worker",
        "inventory_sha256": hashlib.sha256(canonical(["other-writer"]).encode()).hexdigest(),
        "accepted": True,
        "revoked": False,
        "starts_epoch": now - 2,
        "ends_epoch": now + 60,
        "writers": [{"id": "other-writer", "responsible": "operator", "confirmed_epoch": now - 3, "abstain": True}],
    }
    window = root / "window"
    window.write_text(json.dumps(record))
    window.chmod(0o600)
    config.update(
        schema=3,
        template_pins=asdict(pins),
        exclusive_window={
            "path": str(window),
            "digest": hashlib.sha256(window.read_bytes()).hexdigest(),
            "creator_id": "m3-worker",
            "writers": ["other-writer"],
        },
    )
    (root / "installation").write_text(json.dumps(config))
    payload["installation_digest"] = hashlib.sha256((root / "installation").read_bytes()).hexdigest()
    monkeypatch.setattr(window_module, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0))

    def choose(req):
        state["list"] = template if req.url.path.startswith("/templates/") else []

    hooks.append(choose)
    plan = CreatePlan(**config["plan"])
    admission = module.LiveCreateAdmission(
        root / "installation",
        plan=plan,
        installation_digest=payload["installation_digest"],
        approval_digest=payload["approval_digest"],
    )
    return SimpleNamespace(
        root=root,
        payload=payload,
        calls=calls,
        admission=admission,
        config=config,
        plan=plan,
        template=template,
        body=body,
    )


def test_live_collection_repeated_and_w0_shortens_approval(setup):
    s = setup
    start = time.monotonic()
    first = s.admission()
    second = s.admission()
    assert start + 11 < second <= first + 0.5 < start + 61
    assert [r.url.path for r in s.calls] == ["/templates/tpl", "/sandboxes"] * 2
    (s.root / "window").write_text("{}")
    with pytest.raises(ContractError):
        s.admission()
    assert len(s.calls) == 4


@pytest.mark.parametrize("version", [1, 2])
def test_incomplete_live_schema_cannot_grant(setup, version):
    s = setup
    s.config["schema"] = version
    s.config.pop("exclusive_window")
    if version == 1:
        s.config.pop("template_pins")
    path = s.root / "installation"
    path.write_text(json.dumps(s.config))
    admission = module.LiveCreateAdmission(
        path,
        plan=s.plan,
        approval_digest=s.payload["approval_digest"],
        installation_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    with pytest.raises(ContractError):
        admission()


@pytest.mark.parametrize("mode", ["stale", "short_approval", "wrong_plan", "wrong_installation", "wrong_approval"])
def test_returned_internal_evidence_checked_without_extending_it(setup, monkeypatch, mode):
    s = setup
    evidence = module.collect_evidence(s.payload, expected_plan=s.plan)
    changes = {
        "stale": {"valid_until_mono": time.monotonic() - 1},
        "short_approval": {"approval_deadline_mono": time.monotonic() + 10},
        "wrong_plan": {"plan": replace(s.plan, run_id="other")},
        "wrong_installation": {"installation_digest": "0" * 64},
        "wrong_approval": {"approval_digest": "0" * 64},
    }
    monkeypatch.setattr(module, "collect_evidence", lambda *args, **kwargs: replace(evidence, **changes[mode]))
    with pytest.raises(ContractError):
        s.admission()


@pytest.mark.parametrize("revoke", [False, True])
@pytest.mark.parametrize("isolated", [False, True])
def test_actual_worker_uses_live_collector_before_and_after_intent(setup, monkeypatch, revoke, isolated):
    s = setup
    calls = []
    for name in ("vault", "fence"):
        (s.root / name).mkdir(mode=0o700)
    vault = CreateReceiptVault(s.root / "vault", b"k" * 32)

    def handle(req):
        calls.append(req)
        if req.method == "GET":
            value = s.template if req.url.path.startswith("/templates/") else []
            if revoke and len(calls) == 4:
                (s.root / "window").write_text("{}")
            status = 200
        else:
            assert req.method == "POST" and req.content == s.body and len(calls) == 5
            assert (s.root / "fence" / "create.pending").is_file()
            assert len(list((s.root / "vault").glob("*.intent"))) == 1
            value = {
                "sandboxID": "vm",
                "templateID": "tpl",
                "domain": "example.invalid",
                "trafficAccessToken": "synthetic-secret",
                "envdAccessToken": None,
            }
            status = 201
        return httpx.Response(
            status, headers={"Content-Type": "application/json"}, stream=httpx.ByteStream(json.dumps(value).encode())
        )

    monkeypatch.setattr(httpx, "Client", lambda **kw: httpx._client.Client(transport=httpx.MockTransport(handle)))
    worker = CreateWorker(
        plan=s.plan,
        approval_path=s.root / "approval",
        approval_digest=s.payload["approval_digest"],
        installed_candidate_sha256=s.plan.candidate_sha256,
        current_boot_id=s.plan.boot_id,
        api_key="synthetic-api-key",
        ca_file=s.root / "ca",
        vault=vault,
        fence=CreateFence(s.root / "fence"),
        collect_admission=s.admission,
    )
    if isolated:
        monkeypatch.setattr(watchdog, "sys", SimpleNamespace(platform="linux"))
        monkeypatch.setattr(watchdog.threading, "active_count", lambda: 1)

    def create():
        if isolated:
            return watchdog.create_isolated(worker, s.body, deadline=time.monotonic() + 30)
        return worker.create(s.body)

    if revoke:
        with pytest.raises(ContractError):
            create()
        assert (s.root / "window").read_text() == "{}"
    else:
        binding = create()
        assert vault.read(binding).sandbox_id == "vm"
    if not isolated:
        assert [r.method for r in calls] == ["GET"] * 4 + {True: [], False: ["POST"]}[revoke]
    assert (s.root / "fence" / "create.pending").is_file()
