"""Internal R1 single-request worker, NOT an approved operator entry point.

Caller must verify approval/receipt/supervision immediately before calling.
There is no create, retry, environment credential lookup or default live gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .file_io import read_regular
from .m3_auth_probe import Credentials, build_request, observe
from .models import Code, canonical, require
from .worker_process import run_worker


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def _decode(raw: bytes) -> dict:
    require(len(raw) <= 65536)
    value = json.loads(raw, object_pairs_hook=_unique)
    require(type(value) is dict)
    return value


def config_bytes(path: Path) -> bytes:
    """Bounded private read; no network operation."""
    require(path.is_absolute() and path.resolve() == path, Code.DENIED)
    parent = path.parent.stat()
    require(parent.st_uid == os.getuid() and stat.S_IMODE(parent.st_mode) == 0o700, Code.DENIED)
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600, Code.DENIED
    )
    return read_regular(path.parent, path.name, max_bytes=65536)


def prepare(path: Path, *, expected_digest: str | None = None):
    raw = config_bytes(path)
    if expected_digest is not None:
        require(hashlib.sha256(raw).hexdigest() == expected_digest, Code.DENIED)
    value = _decode(raw)
    base = {"schema", "endpoint", "host", "proxy_port", "credentials", "layer", "state", "replacement"}
    require(type(value.get("schema")) is int and value["schema"] in {1, 2})
    require(set(value) == (base if value["schema"] == 1 else base | {"traffic_alias"}))
    if value["schema"] == 1:
        # Retain the frozen v1 contract; new optional-envd semantics require v2.
        require(
            type(value.get("credentials")) is dict
            and type(value["credentials"].get("envd")) is str
            and bool(value["credentials"]["envd"]),
            Code.DENIED,
        )
    credentials = value.pop("credentials")
    require(type(credentials) is dict and set(credentials) == {"api_key", "traffic", "envd"})
    value.pop("schema")
    return build_request(**value, credentials=Credentials(**credentials))


def observe_isolated(config_path: Path, *, verified_deadline: float, expected_digest: str | None = None) -> dict:
    """Trusted caller supplies same-host monotonic guest stop time, not a TTL.

    10s worker + up to 1s cleanup; launch overhead is not an OS hard bound.
    The number is not itself approval or proof that a supervisor is running.
    """
    require(type(verified_deadline) in {float, int} and math.isfinite(verified_deadline))
    require(verified_deadline - time.monotonic() >= 11, Code.DENIED)
    require(config_path.is_absolute() and config_path.resolve() == config_path, Code.DENIED)
    payload = {"path": str(config_path), "deadline": verified_deadline}
    if expected_digest is not None:
        payload["digest"] = expected_digest
    raw = run_worker(
        [sys.executable, "-I", "-m", "agentseek_execution.m3_probe_process"],
        canonical(payload).encode(),
        budget=10,
        environment={},
    )
    result = _decode(raw)
    require(set(result) == {"status", "body_size", "body_sha256", "complete", "reason", "protocol"}, Code.UNKNOWN)
    return result


def perform(value: dict) -> dict:
    require(type(value) is dict and set(value) in ({"path", "deadline"}, {"path", "deadline", "digest"}))
    require(type(value["path"]) is str)
    deadline = value["deadline"]
    require(type(deadline) in {int, float} and math.isfinite(deadline))
    require(deadline - time.monotonic() >= 10, Code.DENIED)
    if "digest" in value:
        require(type(value["digest"]) is str and len(value["digest"]) == 64)
    request = prepare(Path(value["path"]), expected_digest=value.get("digest"))
    # Recheck after disk I/O. Reading config is covered by the parent deadline.
    return asdict(observe(request, remaining_seconds=deadline - time.monotonic()))


def main() -> None:
    try:
        result = perform(_decode(sys.stdin.buffer.read(65537)))
        sys.stdout.write(canonical(result))
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
