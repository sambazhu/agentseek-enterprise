import hashlib
import subprocess
import sys
from dataclasses import replace

import pytest
from agentseek_execution import m3_supervisor_identity as module
from agentseek_execution.m3_supervisor_snapshot import ClockSample, SupervisorSnapshot
from agentseek_execution.models import ContractError


def proc_stat(pid=42, ticks=100, state="S"):
    return (f"{pid} (name ) spaces) " + " ".join([state] + ["0"] * 18 + [str(ticks)])).encode()


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    paths = [tmp_path.resolve() / name for name in ("python", "supervisor.py", "unit.service")]
    command = (
        b"\x00".join([str(paths[0]).encode(), str(paths[1]).encode(), b"--manifest", b"/private/manifest"]) + b"\x00"
    )
    pins = module.IdentityPins(
        str(paths[0]), "a" * 64, str(paths[1]), "b" * 64, str(paths[2]), "c" * 64, hashlib.sha256(command).hexdigest()
    )
    snapshot = SupervisorSnapshot("run", "tpl", "vm", "boot", 100, 99, 42)
    properties = {
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": "42",
        "DropInPaths": "",
        "FragmentPath": str(paths[2]),
        "ControlGroup": "/system.slice/" + module.UNIT,
    }
    proc = {"stat": proc_stat(), "cmdline": command, "cgroup": ("0::" + properties["ControlGroup"] + "\n").encode()}
    monkeypatch.setattr(module, "_show", lambda: dict(properties))
    monkeypatch.setattr(module, "_proc", lambda pid, name: proc[name])
    monkeypatch.setattr(module.os, "readlink", lambda path: pins.executable)
    monkeypatch.setattr(module.os, "sysconf", lambda name: 100)
    monkeypatch.setattr(module, "system_clock", lambda: ClockSample("boot", 1000, 100, 100))
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    hashes = []
    monkeypatch.setattr(module, "_pinned_file", lambda path, digest: hashes.append((path, digest)))
    return pins, snapshot, properties, proc, hashes


def test_identity_requires_unit_proc_and_hashes(fixture):
    pins, snapshot, _, _, hashes = fixture
    result = module.verify_identity(snapshot, pins)
    assert result.pid == 42 and result.start_ticks == 100
    assert len(hashes) == 3


@pytest.mark.parametrize(
    "key,value",
    [
        ("ActiveState", "inactive"),
        ("SubState", "exited"),
        ("MainPID", "43"),
        ("DropInPaths", "/unapproved.conf"),
        ("FragmentPath", "/other"),
        ("ControlGroup", "/other"),
    ],
)
def test_unit_mismatch_rejected(fixture, key, value):
    pins, snapshot, properties, _, hashes = fixture
    properties[key] = value
    with pytest.raises(ContractError):
        module.verify_identity(snapshot, pins)
    assert not hashes


@pytest.mark.parametrize("mode", ["zombie", "pid_reused", "command", "once", "cgroup", "stale", "wrong_boot"])
def test_process_mismatch(fixture, monkeypatch, mode):
    pins, snapshot, _, proc, _ = fixture
    if mode == "zombie":
        proc["stat"] = proc_stat(state="Z")
    elif mode == "pid_reused":
        proc["stat"] = proc_stat(ticks=10000)
    elif mode == "command":
        proc["cmdline"] += b"extra\x00"
    elif mode == "once":
        proc["cmdline"] += b"--once\x00"
        pins = replace(pins, cmdline_sha256=hashlib.sha256(proc["cmdline"]).hexdigest())
    elif mode == "cgroup":
        proc["cgroup"] = b"0::/other\n"
    elif mode == "stale":
        snapshot = replace(snapshot, observed_mono=90)
    else:
        snapshot = replace(snapshot, boot_id="other")
    with pytest.raises(ContractError):
        module.verify_identity(snapshot, pins)


def test_pid_replaced_during_reads(fixture, monkeypatch):
    pins, snapshot, _, proc, _ = fixture
    stats = iter([proc_stat(ticks=100), proc_stat(ticks=101)])
    monkeypatch.setattr(module, "_proc", lambda pid, name: next(stats) if name == "stat" else proc[name])
    with pytest.raises(ContractError):
        module.verify_identity(snapshot, pins)


def test_bad_stat_fields_rejected():
    for raw in (b"", b"42 (comm) Z", proc_stat(pid=43), proc_stat(ticks=-1)):
        with pytest.raises(ContractError):
            module._start(raw, 42)


@pytest.mark.parametrize("mode", ["valid", "oversize", "timeout"])
def test_bounded_systemctl_capture_with_real_child(monkeypatch, mode):
    original = subprocess.Popen
    children = []
    output = "\n".join(key + "=value" for key in module.PROPERTIES) + "\n"
    script = {
        "valid": "print(" + repr(output) + ", end='')",
        "oversize": "print('x' * 20000)",
        "timeout": "import time; time.sleep(20)",
    }[mode]

    def spawn(command, **kwargs):
        assert command[0:3] == ["/usr/bin/systemctl", "show", "--no-pager"]
        assert command[-2:] == ["--", module.UNIT]
        assert "start_new_session" not in kwargs
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
        process = original([sys.executable, "-I", "-c", script], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    if mode == "valid":
        assert module._show() == dict.fromkeys(module.PROPERTIES, "value")
    else:
        with pytest.raises(ContractError):
            module._show()
    assert len(children) == 1 and children[0].poll() is not None
