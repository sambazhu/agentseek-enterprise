"""R1 fixed-request primitive; trusted composition only, NOT a live runner.

No create/kill, SDK auto-auth, arbitrary URL, retries, or PASS adjudication.
The caller must bind approval, fresh receipt, supervisor and remaining lifetime,
and run this inside a whole-operation bounded process before live use.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from .m3_probe_protocol import Signal, inspect
from .models import require

MAX_BODY = 65536
MARKER_COMMAND = "printf '%s\\n' agentseek-m3-r1-auth-ok"
TOKEN_HEADERS = {"traffic": "e2b-traffic-access-token", "envd": "X-Access-Token"}
TRAFFIC_ALIASES = {"e2b": "e2b-traffic-access-token", "cube": "cube-traffic-access-token"}


@dataclass(frozen=True, repr=False)
class Credentials:
    api_key: str
    traffic: str | None
    envd: str | None


@dataclass(frozen=True)
class Request:
    endpoint: str
    method: str
    url: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class Observation:
    """No raw response/header/exception text or credentials leave this API."""

    status: int | None
    body_size: int
    body_sha256: str | None
    complete: bool
    reason: str
    protocol: Signal | None = None


def _token(value: str | None, *, optional: bool = False) -> None:
    require((optional and value is None) or (type(value) is str and 0 < len(value) <= 4096))
    if value is not None:
        require(all(33 <= ord(char) <= 126 for char in value))


def build_request(
    *,
    endpoint: str,
    host: str,
    proxy_port: int,
    credentials: Credentials,
    layer: str,
    state: str,
    replacement: str | None = None,
    traffic_alias: str = "e2b",
) -> Request:
    """Build E1/E2/E3 for explicit loopback proxy, varying one token only.

    Replacement provenance (invalid random or terminated donor) is a caller
    obligation. Missing reference credentials are BLOCKED, not guessed here.
    """
    require(type(endpoint) is str and endpoint in {"E1", "E2", "E3"})
    require(type(layer) is str and layer in TOKEN_HEADERS)
    require(type(traffic_alias) is str and traffic_alias in TRAFFIC_ALIASES)
    require(type(state) is str and state in {"correct", "missing", "wrong", "cross_guest"})
    require(
        type(host) is str and len(host) <= 253 and re.fullmatch(r"[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9]+)*", host) is not None
    )
    require(type(proxy_port) is int and 1024 <= proxy_port <= 65535)
    require(type(credentials) is Credentials)
    _token(credentials.api_key)
    _token(credentials.traffic)
    # Platform v0.7.0 returns no envd token. This is valid for proxy-layer
    # observations, but cannot create a reference token for envd four-state tests.
    _token(credentials.envd, optional=layer == "traffic")
    if state in {"wrong", "cross_guest"}:
        _token(replacement)
        require(replacement != getattr(credentials, layer))
    else:
        require(replacement is None)
    headers = {
        "Host": host,
        "X-API-Key": credentials.api_key,
        "Accept-Encoding": "identity",
        TRAFFIC_ALIASES[traffic_alias]: str(credentials.traffic),
    }
    if credentials.envd is not None:
        headers["X-Access-Token"] = credentials.envd
    target = TRAFFIC_ALIASES[traffic_alias] if layer == "traffic" else TOKEN_HEADERS[layer]
    if state == "missing":
        del headers[target]
    elif state in {"wrong", "cross_guest"}:
        headers[target] = str(replacement)
    body = b""
    if endpoint == "E1":
        path = "/process.Process/Start"
        payload = json.dumps({
            "process": {"cmd": "/bin/bash", "args": ["-l", "-c", MARKER_COMMAND], "envs": {}},
            "stdin": False,
        }).encode()
        body = b"\x00" + struct.pack(">I", len(payload)) + payload
        headers.update({
            "Authorization": "Basic cm9vdDo=",
            "Content-Type": "application/connect+json",
            "Connect-Protocol-Version": "1",
            "Connect-Content-Encoding": "identity",
            "Connect-Timeout-Ms": "2000",
        })
    elif endpoint == "E2":
        path = "/files?path=%2Fetc%2Fhostname&username=root"
    else:
        path = "/filesystem.Filesystem/Stat"
        body = b'{"path":"/etc/hostname"}'
        headers.update({"Content-Type": "application/json", "Connect-Protocol-Version": "1"})
    # Header values have all been validated above; no SDK object can refill them.
    return Request(
        endpoint, "GET" if endpoint == "E2" else "POST", f"http://127.0.0.1:{proxy_port}{path}", headers, body
    )


def observe(request: Request, *, remaining_seconds: float) -> Observation:
    """One request, no retry. Per-I/O 2s and checked total 5s; outer kill required.

    A blocked I/O can outlast the checked deadline by up to its I/O timeout.
    This deliberately does not expose a CLI or claim a hard 5s process bound.
    """
    import httpx

    require(type(request) is Request)
    require(type(remaining_seconds) in {int, float} and math.isfinite(remaining_seconds) and remaining_seconds >= 10)
    with httpx.Client(
        transport=httpx.HTTPTransport(retries=0, trust_env=False),
        timeout=httpx.Timeout(2),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        return _observe(client, request)


def _observe(client: Any, request: Request) -> Observation:
    deadline = time.monotonic() + 5
    status = None
    size = 0
    digest = hashlib.sha256()
    body = bytearray()
    try:
        with client.stream(
            request.method, request.url, headers=request.headers, content=request.body, follow_redirects=False
        ) as response:
            status = response.status_code
            if time.monotonic() >= deadline:
                return Observation(status, 0, None, False, "deadline")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                return Observation(status, 0, None, False, "encoding")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > MAX_BODY):
                return Observation(status, 0, None, False, "length")
            for chunk in response.iter_raw(chunk_size=4096):
                if time.monotonic() >= deadline:
                    return Observation(status, size, None, False, "deadline")
                if size + len(chunk) > MAX_BODY:
                    return Observation(status, size, None, False, "body_limit")
                size += len(chunk)
                digest.update(chunk)
                body.extend(chunk)
            if time.monotonic() >= deadline:
                return Observation(status, size, None, False, "deadline")
            if length is not None and size != int(length):
                return Observation(status, size, None, False, "length_mismatch")
            protocol = inspect(request.endpoint, status, response.headers.get("Content-Type", ""), bytes(body))
            return Observation(status, size, digest.hexdigest(), True, "response", protocol)
    except Exception:
        return Observation(status, size, None, False, "transport_error")
