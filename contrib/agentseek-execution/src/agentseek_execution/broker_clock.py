"""Server clock: monotonic progress plus an encrypted persisted high-water mark.

Restart refuses a wall clock behind the persisted mark. This does not attest NTP
or survive rollback of the entire database; those are operational trust boundaries.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from .models import Code, ContractError, require
from .secure_ledger import SecureLedger


class BrokerClock:
    def __init__(
        self,
        ledger: SecureLedger,
        *,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._ledger = ledger
        self._wall = wall
        self._monotonic = monotonic
        self._epoch = float(wall())
        self._anchor = float(monotonic())
        require(math.isfinite(self._epoch) and math.isfinite(self._anchor), Code.UNKNOWN)
        with ledger.transaction():
            ledger.db.execute(
                "CREATE TABLE IF NOT EXISTS broker_clock (id INTEGER PRIMARY KEY CHECK(id=1), sealed TEXT NOT NULL)"
            )
            prior = self._read()
            require(prior is None or self._epoch >= prior, Code.UNKNOWN)
            self._write(self._epoch)

    def _read(self) -> float | None:
        row = self._ledger.db.execute("SELECT sealed FROM broker_clock WHERE id=1").fetchone()
        if row is None:
            return None
        value = float(self._ledger._reveal(row[0], "broker-clock-v1"))
        require(math.isfinite(value), Code.UNKNOWN)
        return value

    def _write(self, value: float) -> None:
        self._ledger.db.execute(
            "INSERT INTO broker_clock VALUES(1,?) ON CONFLICT(id) DO UPDATE SET sealed=excluded.sealed",
            (self._ledger._protect(repr(value), "broker-clock-v1"),),
        )

    def now(self) -> float:
        wall, mono = float(self._wall()), float(self._monotonic())
        require(math.isfinite(wall) and math.isfinite(mono) and mono >= self._anchor, Code.UNKNOWN)
        candidate = max(wall, self._epoch + mono - self._anchor)
        with self._ledger.transaction():
            prior = self._read()
            if prior is None:
                raise ContractError(Code.UNKNOWN)
            value = max(candidate, prior)
            self._write(value)
        # Re-anchor after a forward wall-clock jump, then subsequent rollback
        # cannot freeze progress until the old wall time catches up.
        self._epoch, self._anchor = value, mono
        return value
