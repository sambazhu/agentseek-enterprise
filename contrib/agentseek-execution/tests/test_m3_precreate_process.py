import hashlib
import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from agentseek_execution import m3_create_worker as creating
from agentseek_execution import m3_evidence_process as existing
from agentseek_execution import m3_precreate_process as module
from agentseek_execution.models import ContractError
from test_m3_evidence_process import Stream
from test_m3_evidence_process import fixture as installed_fixture  # noqa: F401 -- shared private-file fixture
from test_m3_template_evidence import template as template_fixture  # noqa: F401 -- synthetic projection fixture


def test_confirmed_supervision_contract_does_not_grant_admission():
    for checks, missing in module.RESULTS.values():
        assert "local_supervisor_identity" in checks
        assert "independent_supervisor" not in missing
        assert {"create_watchdog", "unknown_outcome_reconciliation", "unified_create_admission"} <= set(missing)


@pytest.fixture
def setup(request, monkeypatch):
    root, payload, calls, hooks = request.getfixturevalue("installed_fixture")
    old = json.loads((root / "installation").read_bytes())
    plan = creating.CreatePlan(
        "run",
        "create",
        "tpl",
        old["binding"]["boot_id"],
        "b" * 64,
        "c" * 64,
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
        "expires_epoch": time.time() + 120,
    }
    (root / "approval").write_text(json.dumps(approval))
    payload["approval_digest"] = hashlib.sha256((root / "approval").read_bytes()).hexdigest()
    api = root / "api-key"
    api.write_text("synthetic-api-key")
    api.chmod(0o600)
    config = {
        "schema": 1,
        "plan": asdict(plan),
        "candidate_sha256": "b" * 64,
        "approval_file": str(root / "approval"),
        "supervisor_directory": str(root),
        "supervisor_identity": old["supervisor_identity"],
        "api_key_file": str(api),
        "ca_file": str(root / "ca"),
    }
    (root / "installation").write_text(json.dumps(config))
    payload["installation_digest"] = hashlib.sha256((root / "installation").read_bytes()).hexdigest()
    manifest = json.loads((root / "manifest.json").read_bytes())
    manifest["sandboxes"] = {}
    (root / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0))
    monkeypatch.setattr(creating, "os", SimpleNamespace(getuid=lambda: 0))
    monkeypatch.setattr(module, "SupervisorReader", existing.SupervisorReader)
    monkeypatch.setattr(module, "verify_identity", existing.verify_identity)
    state = {"list": []}
    original = Stream.__iter__

    def iterate(stream):
        stream.value = state["list"]
        return original(stream)

    monkeypatch.setattr(Stream, "__iter__", iterate)
    return root, payload, calls, hooks, state


def test_empty_precreate_collection_is_read_only_not_authority(setup):
    root, payload, calls, _, _ = setup
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
    result = module.perform(payload)
    assert result["aggregate_ready"] is False and result["checks"] == module.CHECKS
    assert result["missing"] == module.MISSING
    assert "synthetic" not in json.dumps(result) and "deadline" not in result
    assert [(r.method, r.url.path) for r in calls] == [("GET", "/sandboxes")]
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}


def test_internal_evidence_keeps_pins_and_short_freshness_without_grant(setup):
    root, payload, calls, _, _ = setup
    plan = creating.CreatePlan(**json.loads((root / "installation").read_bytes())["plan"])
    evidence = module.collect_evidence(payload, expected_plan=plan)
    assert evidence.plan == plan
    assert evidence.installation_digest == payload["installation_digest"]
    assert evidence.approval_digest == payload["approval_digest"]
    assert 0 < evidence.valid_until_mono - evidence.observed_mono <= 2
    assert "create_token" not in repr(evidence)
    report = evidence.report()
    assert not report["aggregate_ready"]
    assert "plan" not in report and "valid_until_mono" not in report
    report["checks"].clear()
    assert evidence.report()["checks"] == module.CHECKS and module.CHECKS
    assert [r.method for r in calls] == ["GET"]


def test_internal_expected_plan_mismatch_blocks_before_network(setup):
    root, payload, calls, _, _ = setup
    plan = creating.CreatePlan(**json.loads((root / "installation").read_bytes())["plan"])
    with pytest.raises(ContractError):
        module.collect_evidence(payload, expected_plan=replace(plan, run_id="different"))
    assert not calls


@pytest.mark.parametrize("deadlines", [(0, 100), (100, 0)])
def test_internal_evidence_retains_both_approval_bounds(setup, monkeypatch, deadlines):
    _, payload, _, _, _ = setup
    origin = time.monotonic()
    values = iter(origin + offset for offset in deadlines)
    monkeypatch.setattr(module, "read_create_approval", lambda *args, **kwargs: next(values))
    with pytest.raises(ContractError):
        module.collect_evidence(payload)


@pytest.mark.parametrize("mode", ["alarm", "nonempty_manifest", "approval", "installation", "identity"])
def test_preconditions_block_before_get(setup, monkeypatch, mode):
    root, payload, calls, _, _ = setup
    if mode == "alarm":
        (root / "alarm").touch()
    elif mode == "nonempty_manifest":
        path = root / "manifest.json"
        data = json.loads(path.read_bytes())
        data["sandboxes"] = {"old": {"registered_at": time.time() - 5}}
        path.write_text(json.dumps(data))
    elif mode == "identity":

        def fail(*args):
            raise ValueError

        monkeypatch.setattr(module, "verify_identity", fail)
    else:
        (root / mode).write_text("{}")
    with pytest.raises((ContractError, ValueError)):
        module.perform(payload)
    assert not calls


@pytest.mark.parametrize("value", [[{"sandboxID": "other"}], {}, None, False])
def test_platform_nonempty_or_wrong_shape_blocks(setup, value):
    _, payload, calls, _, state = setup
    state["list"] = value
    with pytest.raises(ContractError):
        module.perform(payload)
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["alarm", "approval", "installation", "api-key", "manifest"])
def test_changes_during_get_block_final_report(setup, mode):
    root, payload, calls, hooks, _ = setup

    def change(request):
        if mode == "alarm":
            (root / "alarm").touch()
        elif mode == "manifest":
            path = root / "manifest.json"
            data = json.loads(path.read_bytes())
            data["sandboxes"] = {"new": {"registered_at": time.time() - 5}}
            path.write_text(json.dumps(data))
        else:
            (root / mode).write_text("changed")

    hooks.append(change)
    with pytest.raises((ContractError, ValueError)):
        module.perform(payload)
    assert len(calls) == 1


def test_launcher_is_bounded_and_never_passes_secret_values(setup, monkeypatch):
    root, payload, _, _, _ = setup
    result = {
        "schema": 1,
        "aggregate_ready": False,
        "plan_digest": "a" * 64,
        "checks": module.CHECKS,
        "missing": module.MISSING,
    }

    def run(command, raw, *, budget, environment):
        assert command[-1] == "agentseek_execution.m3_precreate_process" and "-I" in command
        assert budget == 10 and environment == {} and b"synthetic" not in raw
        assert json.loads(raw) == payload
        return json.dumps(result).encode()

    monkeypatch.setattr(module, "run_worker", run)
    assert (
        module.collect_isolated(
            root / "installation",
            installation_digest=payload["installation_digest"],
            approval_digest=payload["approval_digest"],
        )
        == result
    )
    result["aggregate_ready"] = True
    with pytest.raises(ContractError):
        module.collect_isolated(
            root / "installation",
            installation_digest=payload["installation_digest"],
            approval_digest=payload["approval_digest"],
        )


@pytest.mark.parametrize("mode", ["success", "wrong_artifact", "duplicate_container"])
def test_template_precreate_version_two(setup, request, mode):
    root, payload, calls, hooks, state = setup
    template, pins = request.getfixturevalue("template_fixture")
    config = json.loads((root / "installation").read_bytes())
    config.update(schema=2, template_pins=asdict(pins))
    (root / "installation").write_text(json.dumps(config))
    payload["installation_digest"] = hashlib.sha256((root / "installation").read_bytes()).hexdigest()
    if mode == "wrong_artifact":
        template["replicas"][0]["artifact_id"] = "other"
    elif mode == "duplicate_container":
        template["createRequest"]["containers"] *= 2

    def choose(req):
        if req.url.path.startswith("/templates/"):
            assert req.url.path == "/templates/tpl" and req.url.query == b"limit=1"
            state["list"] = template
        else:
            state["list"] = []

    hooks.append(choose)
    if mode == "success":
        result = module.perform(payload)
        assert result["schema"] == 2 and not result["aggregate_ready"]
        assert result["checks"] == module.TEMPLATE_CHECKS and result["missing"] == module.TEMPLATE_MISSING
        assert "synthetic" not in json.dumps(result)
        assert [req.url.path for req in calls] == ["/templates/tpl", "/sandboxes"]
    else:
        with pytest.raises(ContractError):
            module.perform(payload)
        assert len(calls) == 1


@pytest.mark.parametrize("revoke", [False, True])
def test_window_confirmations_rechecked_after_network(setup, request, monkeypatch, revoke):
    from agentseek_execution import m3_exclusive_window as window_module
    from agentseek_execution.models import canonical

    root, payload, calls, hooks, state = setup
    template, pins = request.getfixturevalue("template_fixture")
    config = json.loads((root / "installation").read_bytes())
    writers = ["broker", "sdk", "automation", "cubeops-agenthub"]
    now = time.time()
    record = {
        "schema": 1,
        "window_id": "window",
        "run_id": "run",
        "boot_id": config["plan"]["boot_id"],
        "candidate_sha256": config["candidate_sha256"],
        "creator_id": "m3-worker",
        "inventory_sha256": hashlib.sha256(canonical(sorted(writers)).encode()).hexdigest(),
        "accepted": True,
        "revoked": False,
        "starts_epoch": now - 2,
        "ends_epoch": now + 120,
        "writers": [
            {"id": item, "responsible": "operator", "confirmed_epoch": now - 3, "abstain": True} for item in writers
        ],
    }
    path = root / "window"
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    config.update(
        schema=3,
        template_pins=asdict(pins),
        exclusive_window={
            "path": str(path),
            "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            "creator_id": "m3-worker",
            "writers": writers,
        },
    )
    (root / "installation").write_text(json.dumps(config))
    payload["installation_digest"] = hashlib.sha256((root / "installation").read_bytes()).hexdigest()
    monkeypatch.setattr(window_module, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0))

    def choose(req):
        state["list"] = template if req.url.path.startswith("/templates/") else []
        if revoke and req.url.path == "/sandboxes":
            path.write_text("{}")

    hooks.append(choose)
    if revoke:
        with pytest.raises(ContractError):
            module.perform(payload)
    else:
        result = module.perform(payload)
        assert result["schema"] == 3 and result["checks"] == module.WINDOW_CHECKS
        assert result["missing"] == module.WINDOW_MISSING and not result["aggregate_ready"]
    assert len(calls) == 2
