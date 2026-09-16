import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agentseek_execution import m3_exclusive_window as module
from agentseek_execution.m3_create_worker import CreatePlan
from agentseek_execution.m3_supervisor_snapshot import ClockSample
from agentseek_execution.models import ContractError, canonical


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    root.chmod(0o700)
    path = root / "window"
    boot = "00000000-0000-0000-0000-000000000001"
    writers = ("broker", "sdk", "automation", "cubeops-agenthub")
    plan = CreatePlan(
        "run", "create", "tpl", boot, "a" * 64, "b" * 64, "example.invalid", True, "https://control.example.invalid"
    )
    value = {
        "schema": 1,
        "window_id": "window",
        "run_id": "run",
        "boot_id": boot,
        "candidate_sha256": "a" * 64,
        "creator_id": "m3-worker",
        "inventory_sha256": hashlib.sha256(canonical(sorted(writers)).encode()).hexdigest(),
        "accepted": True,
        "revoked": False,
        "starts_epoch": 990,
        "ends_epoch": 1100,
        "writers": [
            {"id": writer, "responsible": "operator", "confirmed_epoch": 989, "abstain": True} for writer in writers
        ],
    }
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0))
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path, value, plan, writers, ClockSample(boot, 1000, 100, 100)


def read(setup):
    path, _, plan, writers, now = setup
    return module.read_window(
        path,
        pinned_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
        plan=plan,
        creator_id="m3-worker",
        expected_writers=writers,
        now=now,
    )


def test_window_is_coordination_not_platform_exclusion(setup):
    result = read(setup)
    assert result.expires_mono == 200 and not result.technical_exclusion_proven and not result.aggregate_ready


@pytest.mark.parametrize(
    "mode",
    [
        "missing_writer",
        "duplicate_writer",
        "unknown_writer",
        "no_abstain",
        "no_owner",
        "future_confirmation",
        "expired",
        "not_started",
        "revoked",
        "unaccepted",
        "foreign_run",
        "foreign_boot",
        "foreign_candidate",
        "foreign_creator",
        "wrong_inventory",
        "bool_time",
        "extra",
        "permissions",
        "pin",
    ],
)
def test_incomplete_or_changed_confirmation_blocks(setup, mode):
    path, value, plan, writers, now = setup
    pin = hashlib.sha256(path.read_bytes()).hexdigest()
    if mode == "missing_writer":
        value["writers"].pop()
    elif mode == "duplicate_writer":
        value["writers"][-1] = value["writers"][0]
    elif mode == "unknown_writer":
        value["writers"][0]["id"] = "unknown"
    elif mode in {"no_abstain", "no_owner", "future_confirmation"}:
        key, item = {
            "no_abstain": ("abstain", False),
            "no_owner": ("responsible", ""),
            "future_confirmation": ("confirmed_epoch", 1001),
        }[mode]
        value["writers"][0][key] = item
    elif mode.startswith("foreign_"):
        key = {"run": "run_id", "boot": "boot_id", "candidate": "candidate_sha256", "creator": "creator_id"}[mode[8:]]
        value[key] = "other"
    else:
        key, item = {
            "expired": ("ends_epoch", 1010),
            "not_started": ("starts_epoch", 1001),
            "revoked": ("revoked", True),
            "unaccepted": ("accepted", False),
            "wrong_inventory": ("inventory_sha256", "0" * 64),
            "bool_time": ("ends_epoch", True),
            "extra": ("override", True),
            "permissions": ("schema", 1),
            "pin": ("window_id", "changed"),
        }[mode]
        value[key] = item
    path.write_text(json.dumps(value))
    if mode == "permissions":
        path.chmod(0o644)
    if mode != "pin":
        pin = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ContractError):
        module.read_window(
            path, pinned_digest=pin, plan=plan, creator_id="m3-worker", expected_writers=writers, now=now
        )


def test_kernel_boot_must_match(setup):
    path, _, plan, writers, now = setup
    with pytest.raises(ContractError):
        module.read_window(
            path,
            pinned_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
            plan=plan,
            creator_id="m3-worker",
            expected_writers=writers,
            now=replace(now, boot_id="00000000-0000-0000-0000-000000000002"),
        )
