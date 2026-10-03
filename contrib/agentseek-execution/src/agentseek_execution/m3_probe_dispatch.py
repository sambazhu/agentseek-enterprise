"""R1 trusted gate contract and durable at-most-once dispatch reservation.

Evidence must come from a separately implemented trusted collector, NOT a job
JSON. These checks do not authenticate approval or prove platform state.
No default collector, operator CLI, create/kill, or automatic reconciliation.
"""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .file_io import read_regular
from .m3_probe_process import config_bytes, observe_isolated, prepare
from .models import Code, ContractError, canonical, require


@dataclass(frozen=True)
class Binding:
    run_id: str
    create_token: str
    sandbox_id: str
    template_id: str
    artifact_sha256: str
    case_id: str
    config_sha256: str
    approval_ref: str
    candidate_sha256: str
    boot_id: str

    def validate(self) -> None:
        for value in asdict(self).values():
            require(type(value) is str and 0 < len(value) <= 256 and all(33 <= ord(c) <= 126 for c in value))
        for value in (self.artifact_sha256, self.config_sha256, self.candidate_sha256):
            require(re.fullmatch(r"[0-9a-f]{64}", value) is not None)


@dataclass(frozen=True)
class Evidence:
    approval: Binding
    receipt: Binding
    observed_mono: float
    heartbeat_mono: float
    approval_deadline: float
    guest_deadline: float
    supervisor_run_id: str
    boot_id: str
    running: bool
    alarms_clear: bool
    capacity_one: bool
    platform_count: int
    coordination_basis: str = "platform_capacity"
    window_accepted: bool = False

    def verify(self, binding: Binding, now: float) -> float:
        binding.validate()
        require(self.approval == binding and self.receipt == binding, Code.DENIED)
        require(self.boot_id == binding.boot_id and self.supervisor_run_id == binding.run_id, Code.DENIED)
        require(self.running is True and self.alarms_clear is True, Code.DENIED)
        require(
            (self.coordination_basis == "platform_capacity" and self.capacity_one is True)
            or (self.coordination_basis == "approved_window" and self.capacity_one is False and self.window_accepted is True),
            Code.DENIED,
        )
        require(type(self.platform_count) is int and self.platform_count == 1, Code.DENIED)
        for value in (now, self.observed_mono, self.heartbeat_mono, self.approval_deadline, self.guest_deadline):
            require(type(value) in {int, float} and math.isfinite(value), Code.DENIED)
        require(0 <= now - self.observed_mono <= 2 and 0 <= now - self.heartbeat_mono <= 31, Code.DENIED)
        deadline = min(self.approval_deadline, self.guest_deadline)
        require(deadline - now >= 11, Code.DENIED)
        return deadline


class DispatchDirectory:
    """Existing private directory; same-UID/root rollback is outside this model.

    O_EXCL intent creation burns the case even if the write/fsync later fails.
    Never delete/reset intents. A repeated case always requires manual review.
    """

    def __init__(self, path: Path):
        require(path.is_absolute() and path.resolve() == path, Code.DENIED)
        self.path = path

    def _open(self) -> int:
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def dispatch(self, binding: Binding, config_path: Path, *, collect: Callable[[], Evidence]) -> dict:
        """Internal composition only. Collector must be bounded by its caller.

        Directory flock serializes local requests. Two fresh checks bracket the
        durable claim; post-claim denial still burns the case, never re-dispatches.
        """
        binding.validate()
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            collect().verify(binding, time.monotonic())
            require(hashlib.sha256(config_bytes(config_path)).hexdigest() == binding.config_sha256, Code.DENIED)
            prepare(config_path, expected_digest=binding.config_sha256)
            self._claim(fd, binding)
            deadline = collect().verify(binding, time.monotonic())
            result = observe_isolated(config_path, verified_deadline=deadline, expected_digest=binding.config_sha256)
            _validate_result(result)
            self._result(fd, binding, result)
            return result
        finally:
            os.close(fd)

    def dispatch_receipted(
        self, binding: Binding, config_path: Path, *, source, collect: Callable[[], Evidence], observe=None
    ) -> dict:
        """Schema-3 composition; same independent gate and durable case namespace.

        Source is trusted installation state, not a config-supplied vault/key path.
        Does not bypass the existing gate or supply the missing real collector.
        """
        from .m3_receipt_probe import observe_receipted_isolated, prepare_receipted

        binding.validate()
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            collect().verify(binding, time.monotonic())
            prepare_receipted(config_path, binding=binding, source=source)
            self._claim(fd, binding)
            deadline = collect().verify(binding, time.monotonic())
            observer = observe_receipted_isolated if observe is None else observe
            result = observer(config_path, binding=binding, source=source, verified_deadline=deadline)
            _validate_result(result)
            self._result(fd, binding, result)
            return result
        finally:
            os.close(fd)

    @staticmethod
    def _index(binding: Binding) -> str:
        return hashlib.sha256(canonical([binding.run_id, binding.case_id]).encode()).hexdigest()

    @staticmethod
    def _claim(directory_fd: int, binding: Binding) -> None:
        # Stable across changed credentials/candidate/approval: same run+case
        # cannot evade the reservation by changing the rest of its binding.
        index = DispatchDirectory._index(binding)
        try:
            fd = os.open(
                index + ".intent", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd
            )
        except FileExistsError:
            raise ContractError(Code.UNKNOWN) from None
        try:
            raw = canonical({"schema": 1, "binding": asdict(binding), "state": "reserved"}).encode()
            offset = 0
            while offset < len(raw):
                count = os.write(fd, raw[offset:])
                require(count > 0, Code.UNKNOWN)
                offset += count
            os.fsync(fd)
            os.fsync(directory_fd)
        finally:
            os.close(fd)

    @staticmethod
    def _result(directory_fd: int, binding: Binding, result: dict) -> None:
        # Partial result files are intentionally retained and classified unknown.
        # No overwrite, rename, cleanup, or retry of a remote request.
        index = DispatchDirectory._index(binding)
        fd = os.open(
            index + ".result", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd
        )
        try:
            raw = canonical({"schema": 1, "binding": asdict(binding), "observation": result}).encode()
            offset = 0
            while offset < len(raw):
                count = os.write(fd, raw[offset:])
                require(count > 0, Code.UNKNOWN)
                offset += count
            os.fsync(fd)
            os.fsync(directory_fd)
        finally:
            os.close(fd)

    def inspect(self, binding: Binding) -> dict:
        """Read-only local evidence, not platform reconciliation or send authority.

        Must use the original binding. Missing/partial records never authorize a
        retry; 'unrecorded' is only a local observation, not proof of no execution.
        """
        from .m3_probe_process import _decode

        binding.validate()
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            index = self._index(binding)
            intent_path = self.path / (index + ".intent")
            # lstat distinguishes missing from dangling links; non-regular files
            # are handled by the no-follow bounded reader, never interpreted.
            try:
                intent_path.lstat()
            except FileNotFoundError:
                return {"state": "unrecorded"}
            try:
                intent = _decode(read_regular(self.path, index + ".intent", max_bytes=65536))
                require(intent == {"schema": 1, "binding": asdict(binding), "state": "reserved"}, Code.CONFLICT)
                record = _decode(read_regular(self.path, index + ".result", max_bytes=65536))
                require(set(record) == {"schema", "binding", "observation"})
                require(
                    type(record["schema"]) is int and record["schema"] == 1 and record["binding"] == asdict(binding)
                )
                _validate_result(record["observation"])
            except Exception:
                return {"state": "unknown"}
            return {"state": "observed", "observation": record["observation"]}
        finally:
            os.close(fd)


def _validate_result(result: dict) -> None:
    """Reject arbitrary worker output before it reaches audit storage/UI."""
    require(
        type(result) is dict and set(result) == {"status", "body_size", "body_sha256", "complete", "reason", "protocol"}
    )
    status = result["status"]
    require(status is None or (type(status) is int and 100 <= status <= 599))
    require(type(result["body_size"]) is int and 0 <= result["body_size"] <= 65536)
    digest = result["body_sha256"]
    require(digest is None or (type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest) is not None))
    require(type(result["complete"]) is bool)
    require(
        type(result["reason"]) is str
        and result["reason"]
        in {
            "response",
            "deadline",
            "encoding",
            "length",
            "body_limit",
            "length_mismatch",
            "transport_error",
        }
    )
    require(result["complete"] == (result["reason"] == "response"))
    require((digest is not None) == result["complete"])
    protocol = result["protocol"]
    if not result["complete"]:
        require(protocol is None)
        return
    require(type(protocol) is dict and set(protocol) == {"kind", "activity", "exit_code"})
    require(
        type(protocol["kind"]) is str
        and protocol["kind"]
        in {
            "invalid",
            "http_denial_signal",
            "http_other",
            "file_payload",
            "unknown",
            "rpc_denial_signal",
            "stat_payload",
            "malformed",
            "rpc_error",
            "incomplete_protocol",
            "denial_with_activity",
            "command_success",
            "command_not_positive",
        }
    )
    require(type(protocol["activity"]) is bool)
    require(protocol["exit_code"] is None or type(protocol["exit_code"]) is int)
    if protocol["kind"] == "command_success":
        require(status is not None and 200 <= status < 300 and protocol["exit_code"] == 0 and protocol["activity"])
