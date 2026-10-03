import hashlib
import json
from dataclasses import replace

import pytest
from agentseek_execution import m3_lifetime_evidence as module
from agentseek_execution.m3_probe_dispatch import Binding
from agentseek_execution.m3_supervisor_snapshot import ClockSample
from agentseek_execution.models import ContractError


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path.resolve() / "private"
    root.mkdir(mode=0o700)
    path = root / "intent"
    path.write_text(
        json.dumps({
            "schema": 1,
            "run_id": "run",
            "create_token": "create",
            "boot_id": "boot",
            "sent_epoch": 990,
            "sent_mono": 90,
        })
    )
    path.chmod(0o600)
    binding = Binding("run", "create", "vm", "tpl", "a" * 64, "case", "b" * 64, "approval", "c" * 64, "boot")
    return path, {
        "pinned_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        "binding": binding,
        "now": ClockSample("boot", 1000, 100, 100),
        "started_epoch": 992,
        "end_epoch": None,
    }


def test_intent_earlier_than_admission_controls(fixture):
    path, kwargs = fixture
    result = module.read_lifetime(path, **kwargs)
    assert result.stop_mono == 210 and result.remaining_seconds == 110
    assert result.platform_hard_termination_proven is False


def test_end_at_can_shorten_never_extend(fixture):
    path, kwargs = fixture
    assert module.read_lifetime(path, **{**kwargs, "end_epoch": 1040}).stop_mono == 140
    assert module.read_lifetime(path, **{**kwargs, "end_epoch": 9000}).stop_mono == 210
    assert module.read_lifetime(path, **{**kwargs, "started_epoch": 980}).stop_mono == 200


@pytest.mark.parametrize(
    "change",
    [
        {"started_epoch": 1001},
        {"started_epoch": True},
        {"end_epoch": 980},
        {"end_epoch": 1005},
        {"end_epoch": float("inf")},
        {"now": ClockSample("other", 1000, 100, 100)},
        {"now": ClockSample("boot", 1002, 100, 100)},
        {"now": ClockSample("boot", 1105, 205, 205)},
    ],
)
def test_invalid_or_exhausted_budget_blocks(fixture, change):
    path, kwargs = fixture
    with pytest.raises(ContractError):
        module.read_lifetime(path, **{**kwargs, **change})


def test_wrong_digest_or_binding(fixture):
    path, kwargs = fixture
    for changed in ({"pinned_digest": "d" * 64}, {"binding": replace(kwargs["binding"], create_token="other")}):
        with pytest.raises(ContractError):
            module.read_lifetime(path, **{**kwargs, **changed})


@pytest.mark.parametrize("value", [True, None, "2026-09-09T01:00:00", "nonsense", float("nan"), -1])
def test_bad_platform_timestamps(value):
    with pytest.raises(ContractError):
        module.platform_epoch(value)


def test_nanosecond_timestamp_and_timezone():
    assert module.platform_epoch("2026-09-09T01:00:00.123456789Z") == module.platform_epoch(
        "2026-09-09T09:00:00.123456+0800"
    )
