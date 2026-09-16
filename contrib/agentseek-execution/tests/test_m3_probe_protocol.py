import base64
import json
import struct

import pytest
from agentseek_execution.m3_probe_protocol import MARKER, Signal, inspect


def frame(value, flags=0):
    raw = json.dumps(value).encode()
    return bytes([flags]) + struct.pack(">I", len(raw)) + raw


DATA = frame({"event": {"data": {"stdout": base64.b64encode(MARKER).decode()}}})
END = frame({"event": {"end": {"exitCode": 0}}})
EOF = frame({}, 2)
DENY = frame({"error": {"code": "unauthenticated", "message": "secret"}}, 2)


def command(body, status=200):
    result = inspect("E1", status, "application/connect+json", body)
    assert "secret" not in repr(result)
    return result


def test_positive_requires_exact_output_and_both_endings():
    assert command(DATA + END + EOF) == Signal("command_success", True, 0)
    assert command(DATA + END).kind == "incomplete_protocol"
    assert command(DATA + EOF).kind == "incomplete_protocol"
    assert command(END + EOF).kind == "command_not_positive"
    assert command(DATA + END + EOF, 403).kind == "command_not_positive"


def test_denial_after_activity_never_clean_denial():
    assert command(DENY) == Signal("rpc_denial_signal")
    assert command(DATA + DENY).kind == "denial_with_activity"
    assert command(DATA + END + DENY).kind == "denial_with_activity"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        DATA[:-1],
        DATA + END + EOF + b"x",
        frame({}, 1),
        frame({}, 4),
        frame({"event": {"data": {"stdout": "%%%"}}}),
        DATA + frame({"event": {"end": {"exitCode": False}}}) + EOF,
        DATA + frame({"event": {"end": {"exitCode": 0, "exit_code": 1}}}) + EOF,
        DATA + END + END + EOF,
        DATA + END + DATA + EOF,
    ],
)
def test_malformed_or_incomplete_never_positive(body):
    assert command(body).kind in {"malformed", "incomplete_protocol"}


def test_duplicate_json_fields_rejected():
    raw = b'{"event":{"end":{"exitCode":1,"exitCode":0}}}'
    assert command(DATA + b"\x00" + struct.pack(">I", len(raw)) + raw + EOF).kind == "malformed"


def test_wrong_marker_stderr_and_nonzero_not_positive():
    for event in ({"data": {"stderr": "eA=="}}, {"data": {"stdout": "eA=="}}):
        assert command(DATA + frame({"event": event}) + END + EOF).kind == "command_not_positive"
    assert command(DATA + frame({"event": {"end": {"exitCode": 1}}}) + EOF).kind == "command_not_positive"


def test_file_and_stat_are_only_payload_signals():
    assert inspect("E2", 200, "text/html", b"login").kind == "unknown"
    assert inspect("E2", 200, "text/plain", b"host").kind == "file_payload"
    assert inspect("E3", 200, "application/json", b'{"entry":{"name":"hostname"}}').kind == "stat_payload"
    assert inspect("E3", 200, "application/json", b'{"entry":{}}').kind == "unknown"
    assert inspect("E3", 200, "application/json", b'{"code":"unauthenticated"}').kind == "rpc_denial_signal"
    assert inspect("E1", 401, "text/plain", b"secret").kind == "http_denial_signal"
