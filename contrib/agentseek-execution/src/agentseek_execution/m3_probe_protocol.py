"""Bounded R1 protocol observations, never an authentication PASS oracle.

Response signals do not establish the rejecting layer or absence of side effects.
No raw server message, output or filename is returned to the caller.
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass, field

LIMIT = 65536
MARKER = b"agentseek-m3-r1-auth-ok\n"
DENIALS = {"unauthenticated", "permission_denied"}


@dataclass(frozen=True)
class Signal:
    kind: str
    activity: bool = False
    exit_code: int | None = None


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _json(raw: bytes) -> dict:
    value = json.loads(raw, object_pairs_hook=_object)
    if type(value) is not dict:
        raise ValueError
    return value


def inspect(endpoint: str, status: int, content_type: str, body: bytes) -> Signal:
    """Only complete, bounded HTTP bodies; caller handles truncation/deadlines."""
    if type(body) is not bytes or len(body) > LIMIT:
        return Signal("invalid")
    media = content_type.partition(";")[0].strip().lower()
    if endpoint == "E1" and media == "application/connect+json":
        # Inspect activity even when an intermediary reports a denial status.
        return _command(body, status)
    if status in {401, 403}:
        return Signal("http_denial_signal")
    if not 200 <= status < 300:
        return Signal("http_other")
    try:
        if endpoint == "E2":
            # Content alone is not proof it came from the requested guest/file.
            return Signal("file_payload" if body and media in {"text/plain", "application/octet-stream"} else "unknown")
        if endpoint == "E3" and media == "application/json":
            value = _json(body)
            if value.get("code") in DENIALS:
                return Signal("rpc_denial_signal")
            if type(value.get("entry")) is dict and value["entry"]:
                return Signal("stat_payload")
        return Signal("unknown")
    except Exception:
        return Signal("malformed")


def _frames(body: bytes):
    offset = 0
    stream_end = False
    while offset < len(body):
        if stream_end or len(body) - offset < 5:
            raise ValueError
        flags = body[offset]
        size = struct.unpack(">I", body[offset + 1 : offset + 5])[0]
        offset += 5
        if flags not in {0, 2} or size > LIMIT or offset + size > len(body):
            raise ValueError
        value = _json(body[offset : offset + size])
        offset += size
        stream_end = flags == 2
        yield flags, value


def _exit_code(end: object) -> int:
    if type(end) is not dict or end.get("error") or ("exitCode" in end and "exit_code" in end):
        raise ValueError
    # No default-zero or ambiguous status coercion.
    code = end.get("exitCode", end.get("exit_code"))
    if type(code) is not int:
        raise ValueError
    return code


@dataclass
class _CommandState:
    activity: bool = False
    ended: bool = False
    exit_code: int | None = None
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)

    def event(self, value: dict) -> None:
        event = value.get("event")
        if self.ended or type(event) is not dict or len(event) != 1:
            raise ValueError
        self.activity = True
        if "start" in event:
            return
        if "data" in event:
            self.data(event["data"])
        elif "end" in event:
            self.ended = True
            self.exit_code = _exit_code(event["end"])
        else:
            raise ValueError

    def data(self, data: object) -> None:
        if type(data) is not dict or not set(data) <= {"stdout", "stderr"}:
            raise ValueError
        for name, target in (("stdout", self.stdout), ("stderr", self.stderr)):
            encoded = data.get(name, "")
            if type(encoded) is not str:
                raise ValueError
            target.extend(base64.b64decode(encoded, validate=True))


def _command(body: bytes, status: int) -> Signal:
    state = _CommandState()
    stream_end = False
    denied = False
    try:
        for flags, value in _frames(body):
            if flags == 0:
                state.event(value)
                continue
            stream_end = True
            error = value.get("error")
            if error is not None:
                if type(error) is not dict or error.get("code") not in DENIALS:
                    return Signal("rpc_error", state.activity, state.exit_code)
                denied = True
        if not stream_end:
            return Signal("incomplete_protocol", state.activity, state.exit_code)
        if denied:
            return Signal(
                "denial_with_activity" if state.activity else "rpc_denial_signal", state.activity, state.exit_code
            )
        if not state.ended:
            return Signal("incomplete_protocol", state.activity, state.exit_code)
        if 200 <= status < 300 and state.exit_code == 0 and state.stdout == MARKER and not state.stderr:
            return Signal("command_success", True, 0)
        return Signal("command_not_positive", state.activity, state.exit_code)
    except Exception:
        return Signal("malformed", state.activity, state.exit_code)
