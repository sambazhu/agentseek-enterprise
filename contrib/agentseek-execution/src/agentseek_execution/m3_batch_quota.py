"""Append-only local create-slot consumption; not approval or guest convergence.

Trusted installation pins a single directory and sequence. No refunds, reset or
automatic directory creation. UNKNOWN consumes its slot. Host-owner rollback is
outside the journal's guarantees. Reservation alone never authorizes sending.
"""

import fcntl
import hashlib
import os
import stat
from dataclasses import asdict

from .m3_probe_process import config_bytes
from .models import Code, ContractError, canonical, require


class BatchQuota:
    def __init__(self, directory, sequence):
        require(directory.is_absolute() and directory.resolve() == directory, Code.DENIED)
        require(type(sequence) is tuple and len(sequence) in {2, 4}, Code.DENIED)
        for index, plan in enumerate(sequence):
            plan.binding("0" * 64)
            require(plan.restricted is (index < 2), Code.DENIED)
            require((plan.run_id, plan.boot_id, plan.candidate_sha256, plan.template_id, plan.endpoint, plan.domain) ==
                    (sequence[0].run_id, sequence[0].boot_id, sequence[0].candidate_sha256,
                     sequence[0].template_id, sequence[0].endpoint, sequence[0].domain), Code.DENIED)
        require(len({p.create_token for p in sequence}) == len(sequence), Code.DENIED)
        self.directory, self.sequence = directory, sequence
        self.digest = hashlib.sha256(canonical([asdict(p) for p in sequence]).encode()).hexdigest()

    def _record(self, index):
        return canonical({"schema": 1, "sequence_sha256": self.digest, "slot": index,
                          "plan_sha256": hashlib.sha256(canonical(asdict(self.sequence[index])).encode()).hexdigest()}).encode()

    def reserve(self, plan):
        """Consume exactly the next slot before send; partial writes remain spent.

        Caller must check prior closeout and current approval separately. The
        directory lock serializes quota writers only, not Cube platform clients.
        """
        require(plan in self.sequence, Code.DENIED)
        index = self.sequence.index(plan)
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for previous in range(index):
                require(config_bytes(self.directory / f"create-slot-{previous}") == self._record(previous), Code.DENIED)
            # Fixed names prohibit changing the sequence digest to reset slots.
            for future in range(index, 4):
                try:
                    os.stat(f"create-slot-{future}", dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                require(False, Code.UNKNOWN)
            fd = os.open(f"create-slot-{index}", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            try:
                raw = self._record(index)
                offset = 0
                while offset < len(raw):
                    written = os.write(fd, raw[offset:])
                    require(written > 0, Code.UNKNOWN)
                    offset += written
                os.fsync(fd)
                os.fsync(directory)
            finally:
                os.close(fd)
        except Exception:
            raise ContractError(Code.UNKNOWN) from None
        finally:
            os.close(directory)
