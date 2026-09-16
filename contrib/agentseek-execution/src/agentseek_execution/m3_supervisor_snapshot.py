"""Read-only R1 supervisor file snapshot, not aggregate readiness.

Mirrors existing node_supervisor JSON without instantiating ManifestStore (which
opens/creates a lock file). No writes, lock creation, process control or network.
Root-owned inputs are required in production. Fresh heartbeat is not proof of a
live process, systemd identity, platform capacity, or approval authenticity.
"""

from __future__ import annotations

import fcntl
import math
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .file_io import read_regular
from .m3_probe_process import _decode
from .models import Code, require


@dataclass(frozen=True)
class ClockSample:
    boot_id: str
    epoch: float
    monotonic: float
    uptime: float


def _proc_text(path: str) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        raw = os.read(fd, 257)
        require(len(raw) <= 256, Code.DENIED)
        return raw.decode("ascii").strip()
    finally:
        os.close(fd)


def system_clock() -> ClockSample:
    """Linux only, actual kernel boot ID and uptime; no caller-supplied JSON."""
    boot = _proc_text("/proc/sys/kernel/random/boot_id")
    uptime = float(_proc_text("/proc/uptime").split()[0])
    return ClockSample(boot, time.time(), time.monotonic(), uptime)


@dataclass(frozen=True)
class SupervisorSnapshot:
    run_id: str
    template_id: str
    sandbox_id: str
    boot_id: str
    observed_mono: float
    heartbeat_mono: float
    heartbeat_pid: int
    aggregate_ready: bool = False


@dataclass(frozen=True)
class PreCreateSupervisorSnapshot:
    run_id: str
    template_id: str
    boot_id: str
    observed_mono: float
    heartbeat_mono: float
    heartbeat_pid: int
    aggregate_ready: bool = False


def _clock_valid(value: ClockSample) -> None:
    require(type(value) is ClockSample)
    require(
        type(value.boot_id) is str
        and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value.boot_id) is not None
    )
    for number in (value.epoch, value.monotonic, value.uptime):
        require(type(number) in {float, int} and math.isfinite(number) and number >= 0, Code.DENIED)


class SupervisorReader:
    """Pinned clock anchor detects wall-clock steps across repeated reads.

    Production default owner is root. Alternate owner/clock exist only for
    trusted offline tests, not operator-configurable settings. No missing-file
    fallback, no automatic permission repair. Caller must provide outer budget.
    """

    def __init__(self, directory: Path, *, owner_uid: int = 0, clock=system_clock):
        require(directory.is_absolute() and directory.resolve() == directory, Code.DENIED)
        self.directory = directory
        self.owner_uid = owner_uid
        self.clock = clock
        self.anchor = clock()
        _clock_valid(self.anchor)

    def _sample(self) -> ClockSample:
        sample = self.clock()
        _clock_valid(sample)
        require(sample.boot_id == self.anchor.boot_id, Code.DENIED)
        elapsed = sample.monotonic - self.anchor.monotonic
        require(elapsed >= 0 and sample.uptime >= self.anchor.uptime, Code.DENIED)
        require(abs((sample.epoch - self.anchor.epoch) - elapsed) <= 0.5, Code.DENIED)
        require(abs((sample.uptime - self.anchor.uptime) - elapsed) <= 0.5, Code.DENIED)
        return sample

    def _private(self, name: str) -> None:
        info = (self.directory / name).lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == self.owner_uid and info.st_nlink == 1, Code.DENIED)
        require(stat.S_IMODE(info.st_mode) == 0o600, Code.DENIED)

    def _json(self, name: str) -> dict:
        self._private(name)
        return _decode(read_regular(self.directory, name, max_bytes=65536))

    def _no_alarm(self, fd: int) -> None:
        try:
            os.stat("alarm", dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        require(False, Code.DENIED)  # even an empty file or dangling symlink blocks

    def read(self, *, run_id: str, template_id: str, sandbox_id: str) -> SupervisorSnapshot:
        require(type(sandbox_id) is str and bool(sandbox_id), Code.DENIED)
        return self._read(run_id=run_id, template_id=template_id, sandbox_id=sandbox_id)

    def read_empty(self, *, run_id: str, template_id: str) -> PreCreateSupervisorSnapshot:
        """Pre-create observation: require an empty manifest, never invent an ID."""
        snapshot = self._read(run_id=run_id, template_id=template_id, sandbox_id=None)
        return PreCreateSupervisorSnapshot(
            snapshot.run_id,
            snapshot.template_id,
            snapshot.boot_id,
            snapshot.observed_mono,
            snapshot.heartbeat_mono,
            snapshot.heartbeat_pid,
        )

    def read_registered(self, *, run_id: str, template_id: str, registered_ids: tuple[str, ...]):
        """Exact trusted historical IDs; platform liveness must be checked separately."""
        require(type(registered_ids) is tuple and len(set(registered_ids)) == len(registered_ids), Code.DENIED)
        require(all(type(sid) is str and bool(sid) for sid in registered_ids), Code.DENIED)
        return self._read(run_id=run_id, template_id=template_id, sandbox_id=None, registered_ids=registered_ids)

    def _read(self, *, run_id: str, template_id: str, sandbox_id: str | None, registered_ids=None) -> SupervisorSnapshot:
        start = self._sample()
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            require(info.st_uid == self.owner_uid and stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
            self._private("manifest.json.lock")
            lock = os.open("manifest.json.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                self._no_alarm(fd)
                manifest = self._json("manifest.json")
                beat = self._json("manifest.json.heartbeat")
                self._validate(manifest, beat, run_id, template_id, sandbox_id, registered_ids)
                end = self._sample()
                require(0 <= end.monotonic - start.monotonic <= 2, Code.DENIED)
                self._no_alarm(fd)
                linked = self.directory.stat()
                require((linked.st_dev, linked.st_ino) == (info.st_dev, info.st_ino), Code.DENIED)
                age = end.epoch - beat["ts"]
                require(0 <= age <= 31 and age <= end.uptime, Code.DENIED)
                return SupervisorSnapshot(
                    run_id, template_id, sandbox_id or "", end.boot_id, end.monotonic, end.monotonic - age, beat["pid"]
                )
            finally:
                os.close(lock)
        finally:
            os.close(fd)

    @staticmethod
    def _validate(manifest: dict, beat: dict, run_id: str, template_id: str, sandbox_id: str | None, registered_ids=None) -> None:
        require(set(manifest) == {"run_id", "template_alias", "template_id", "run_started_at", "sandboxes"})
        require(manifest["run_id"] == run_id and manifest["template_id"] == template_id, Code.DENIED)
        require(type(manifest["template_alias"]) is str and bool(manifest["template_alias"]))
        require(type(manifest["sandboxes"]) is dict, Code.DENIED)
        if registered_ids is not None:
            require(set(manifest["sandboxes"]) == set(registered_ids), Code.DENIED)
        else:
            require(manifest["sandboxes"] == {} if sandbox_id is None else sandbox_id in manifest["sandboxes"], Code.DENIED)
        require(set(beat) == {"run_id", "ts", "pid"} and beat["run_id"] == run_id, Code.DENIED)
        require(type(beat["pid"]) is int and beat["pid"] > 0, Code.DENIED)
        stamps = [manifest["run_started_at"], beat["ts"]]
        for entry in manifest["sandboxes"].values():
            require(type(entry) is dict and set(entry) == {"registered_at"})
            stamps.append(entry["registered_at"])
        for stamp in stamps:
            require(type(stamp) in {int, float} and math.isfinite(stamp) and 0 < stamp <= beat["ts"], Code.DENIED)
