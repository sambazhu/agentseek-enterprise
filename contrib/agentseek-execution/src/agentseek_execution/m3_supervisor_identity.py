"""Read-only Linux/systemd supervisor identity checks for trusted R1 collector.

Fixed systemctl show, bounded output, no service control. Private command-line
contents are hashed locally and never returned. Pins must be installation-owned.
This checks current identity, not future liveness or guest termination success.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .file_io import read_regular
from .m3_supervisor_snapshot import PreCreateSupervisorSnapshot, SupervisorSnapshot, system_clock
from .models import Code, ContractError, require

UNIT = "agentseek-m3-r1-supervisor.service"
PROPERTIES = ("ActiveState", "SubState", "MainPID", "ControlGroup", "FragmentPath", "DropInPaths")


def _validate_unit(unit: str) -> None:
    require(type(unit) is str and len(unit) <= 255
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.service", unit) is not None, Code.DENIED)


def _show(unit: str = UNIT) -> dict[str, str]:
    """Same process group as enclosing bounded collector, never a detached child."""
    _validate_unit(unit)
    process = subprocess.Popen(  # noqa: S603 -- validated single unit, read-only command
        ["/usr/bin/systemctl", "show", "--no-pager", "--property=" + ",".join(PROPERTIES), "--", unit],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        close_fds=True,
    )
    try:
        if process.stdout is None:
            raise ContractError(Code.DENIED)  # noqa: TRY301 -- narrow optional pipe while keeping cleanup in finally
        os.set_blocking(process.stdout.fileno(), False)
        deadline = time.monotonic() + 2
        body = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                require(remaining > 0, Code.DENIED)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(process.stdout)
                    else:
                        require(len(body) + len(chunk) <= 16384, Code.DENIED)
                        body.extend(chunk)
            remaining = deadline - time.monotonic()
            require(remaining > 0 and process.wait(timeout=remaining) == 0, Code.DENIED)
        result = {}
        for line in body.decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            require(bool(separator) and key not in result and key in PROPERTIES, Code.DENIED)
            result[key] = value
        require(set(result) == set(PROPERTIES), Code.DENIED)
    except Exception:
        raise ContractError(Code.DENIED) from None
    else:
        return result
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        finally:
            if process.stdout is not None:
                process.stdout.close()


@dataclass(frozen=True)
class IdentityPins:
    executable: str
    executable_sha256: str
    script: str
    script_sha256: str
    unit_file: str
    unit_sha256: str
    cmdline_sha256: str
    unit: str = UNIT

    def validate(self) -> None:
        _validate_unit(self.unit)
        for text in (self.executable, self.script, self.unit_file):
            require(type(text) is str and Path(text).is_absolute() and Path(text).resolve() == Path(text), Code.DENIED)
        for digest in (self.executable_sha256, self.script_sha256, self.unit_sha256, self.cmdline_sha256):
            require(type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest) is not None, Code.DENIED)


def _pinned_file(path: str, digest: str) -> None:
    source = Path(path)
    info = source.lstat()
    require(info.st_uid == 0 and stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022, Code.DENIED)
    raw = read_regular(source.parent, source.name, max_bytes=32 * 1024 * 1024)
    require(hashlib.sha256(raw).hexdigest() == digest, Code.DENIED)


def _proc(pid: int, name: str) -> bytes:
    require(name in {"stat", "cmdline", "cgroup"})
    fd = os.open(f"/proc/{pid}/{name}", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        require(os.fstat(fd).st_uid == 0, Code.DENIED)
        raw = os.read(fd, 16385)
        require(len(raw) <= 16384, Code.DENIED)
        return raw
    finally:
        os.close(fd)


def _start(raw: bytes, pid: int) -> int:
    # comm can contain spaces and ')'; stat fields after final ')' are stable.
    prefix, separator, tail = raw.rpartition(b") ")
    require(bool(separator) and prefix.startswith(str(pid).encode() + b" ("), Code.DENIED)
    fields = tail.split()
    require(len(fields) >= 20 and fields[0] not in {b"Z", b"X", b"x"}, Code.DENIED)
    require(fields[19].isdigit(), Code.DENIED)
    return int(fields[19])


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    observed_mono: float
    unit: str = UNIT


def verify_identity(snapshot: SupervisorSnapshot | PreCreateSupervisorSnapshot, pins: IdentityPins) -> ProcessIdentity:
    pins.validate()
    require(type(snapshot.heartbeat_pid) is int and snapshot.heartbeat_pid > 0, Code.DENIED)
    pid = snapshot.heartbeat_pid
    before = _show(pins.unit)
    require(
        before["ActiveState"] == "active"
        and before["SubState"] == "running"
        and before["MainPID"] == str(pid)
        and before["DropInPaths"] == ""
        and before["FragmentPath"] == pins.unit_file,
        Code.DENIED,
    )
    group = before["ControlGroup"]
    require(group == "/system.slice/" + pins.unit, Code.DENIED)
    start = _start(_proc(pid, "stat"), pid)
    clock = system_clock()
    ticks = os.sysconf("SC_CLK_TCK")
    require(clock.boot_id == snapshot.boot_id and type(ticks) is int and ticks > 0, Code.DENIED)
    heartbeat_age = clock.monotonic - snapshot.heartbeat_mono
    require(0 <= heartbeat_age <= 31 and 0 <= start / ticks <= clock.uptime - heartbeat_age, Code.DENIED)
    require(os.readlink(f"/proc/{pid}/exe") == pins.executable, Code.DENIED)
    command = _proc(pid, "cmdline")
    require(hashlib.sha256(command).hexdigest() == pins.cmdline_sha256, Code.DENIED)
    args = command.rstrip(b"\x00").split(b"\x00")
    require(pins.script.encode() in args, Code.DENIED)
    require(
        not any(arg in {b"--once", b"--gate-check", b"--register-run", b"--register-sandbox"} for arg in args),
        Code.DENIED,
    )
    groups = _proc(pid, "cgroup").decode("ascii").splitlines()
    require(groups == ["0::" + group], Code.DENIED)
    for path, digest in (
        (pins.executable, pins.executable_sha256),
        (pins.script, pins.script_sha256),
        (pins.unit_file, pins.unit_sha256),
    ):
        _pinned_file(path, digest)
    # Detect PID reuse or service changes during the read window.
    require(_start(_proc(pid, "stat"), pid) == start and _show(pins.unit) == before, Code.DENIED)
    require(_proc(pid, "cmdline") == command and os.readlink(f"/proc/{pid}/exe") == pins.executable, Code.DENIED)
    now = time.monotonic()
    require(0 <= now - snapshot.observed_mono <= 2 and 0 <= now - snapshot.heartbeat_mono <= 31, Code.DENIED)
    return ProcessIdentity(pid, start, now, pins.unit)
