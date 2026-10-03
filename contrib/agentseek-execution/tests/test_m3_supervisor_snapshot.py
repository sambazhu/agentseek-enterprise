import fcntl
import json
import os
from dataclasses import replace

import pytest
from agentseek_execution import m3_supervisor_snapshot as module
from agentseek_execution.models import ContractError


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path.resolve() / "supervisor"
    root.mkdir(mode=0o700)
    documents = {
        "manifest.json": {
            "run_id": "run",
            "template_alias": "alias",
            "template_id": "tpl",
            "run_started_at": 990,
            "sandboxes": {"vm": {"registered_at": 991}},
        },
        "manifest.json.heartbeat": {"run_id": "run", "ts": 999, "pid": 42},
    }
    for name, value in documents.items():
        path = root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    lock = root / "manifest.json.lock"
    lock.touch(mode=0o600)
    clock = module.ClockSample("00000000-0000-0000-0000-000000000001", 1000, 100, 100)
    return root, clock


def reader(root, clock):
    return module.SupervisorReader(root, owner_uid=os.getuid(), clock=lambda: clock)


def read(value):
    return value.read(run_id="run", template_id="tpl", sandbox_id="vm")


def test_actual_files_read_without_mutation(fixture):
    root, clock = fixture
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
    result = read(reader(root, clock))
    assert result.heartbeat_mono == 99 and result.aggregate_ready is False
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}


def test_precreate_empty_manifest_has_no_sandbox_identity(fixture):
    root, clock = fixture
    path = root / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["sandboxes"] = {}
    path.write_text(json.dumps(manifest))
    value = reader(root, clock)
    result = value.read_empty(run_id="run", template_id="tpl")
    assert type(result) is module.PreCreateSupervisorSnapshot and not hasattr(result, "sandbox_id")
    assert result.heartbeat_pid == 42 and not result.aggregate_ready
    with pytest.raises(ContractError):
        read(value)
    with pytest.raises(ContractError):
        value.read(run_id="run", template_id="tpl", sandbox_id="")


@pytest.mark.parametrize("ids", [("vm",), (), ("other",), ("vm", "other"), ("vm", "vm")])
def test_registered_history_is_exact_and_read_only(fixture, ids):
    root, clock = fixture
    value = reader(root, clock)
    before = (root / "manifest.json").read_bytes()
    if ids == ("vm",):
        result = value.read_registered(run_id="run", template_id="tpl", registered_ids=ids)
        assert result.sandbox_id == ""
    else:
        with pytest.raises(ContractError):
            value.read_registered(run_id="run", template_id="tpl", registered_ids=ids)
    assert (root / "manifest.json").read_bytes() == before


@pytest.mark.parametrize(
    "mode",
    [
        "missing_lock",
        "alarm",
        "alarm_link",
        "heartbeat_link",
        "public",
        "root_public",
        "duplicate",
        "unknown_vm",
        "foreign_run",
        "future",
        "stale",
        "boolean_pid",
        "preboot",
    ],
)
def test_fail_closed(fixture, mode):  # noqa: C901 -- explicit independent failure injections
    root, clock = fixture
    path = root / "manifest.json.heartbeat"
    if mode == "missing_lock":
        (root / "manifest.json.lock").unlink()
    elif mode == "alarm":
        (root / "alarm").touch()
    elif mode == "alarm_link":
        (root / "alarm").symlink_to(root / "missing")
    elif mode == "heartbeat_link":
        path.unlink()
        path.symlink_to(root / "missing")
    elif mode == "public":
        path.chmod(0o644)
    elif mode == "root_public":
        root.chmod(0o755)
    elif mode == "duplicate":
        path.write_text('{"run_id":"run","run_id":"run"}')
    elif mode == "unknown_vm":
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["sandboxes"] = {}
        (root / "manifest.json").write_text(json.dumps(manifest))
    elif mode == "preboot":
        clock = replace(clock, uptime=0.5)
    else:
        beat = json.loads(path.read_text())
        if mode == "foreign_run":
            beat["run_id"] = "other"
        elif mode == "future":
            beat["ts"] = 1001
        elif mode == "stale":
            clock = replace(clock, epoch=1040)
        else:
            beat["pid"] = True
        path.write_text(json.dumps(beat))
    with pytest.raises((ContractError, FileNotFoundError)):
        read(reader(root, clock))


@pytest.mark.parametrize(
    "change", [{"epoch": 1002}, {"epoch": 998}, {"boot_id": "00000000-0000-0000-0000-000000000002"}, {"uptime": 103}]
)
def test_clock_step_or_boot_change(fixture, change):
    root, clock = fixture
    samples = iter([clock, replace(clock, **change)])
    value = module.SupervisorReader(root, owner_uid=os.getuid(), clock=lambda: next(samples))
    with pytest.raises(ContractError):
        read(value)


def test_existing_exclusive_lock_denies_without_wait(fixture):
    root, clock = fixture
    with (root / "manifest.json.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            read(reader(root, clock))


def test_alarm_appearing_during_read_blocks(fixture, monkeypatch):
    root, clock = fixture
    value = reader(root, clock)
    original = value._json

    def injected(name):
        result = original(name)
        if name.endswith("heartbeat"):
            (root / "alarm").touch()
        return result

    monkeypatch.setattr(value, "_json", injected)
    with pytest.raises(ContractError):
        read(value)


def test_linux_clock_uses_fixed_proc_paths(monkeypatch):
    calls = []

    def proc(path):
        calls.append(path)
        return "00000000-0000-0000-0000-000000000001" if path.endswith("boot_id") else "100.0 50.0"

    monkeypatch.setattr(module, "_proc_text", proc)
    sample = module.system_clock()
    assert sample.uptime == 100
    assert calls == ["/proc/sys/kernel/random/boot_id", "/proc/uptime"]
