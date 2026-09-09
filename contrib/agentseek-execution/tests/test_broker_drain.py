import json
import socket
import threading
import time
from itertools import pairwise

import pytest
from agentseek_execution import broker_wire


def test_response_precedes_half_close_and_unread_tail_drain():
    events = []
    pending = bytearray(b" " * (broker_wire.MAX_REQUEST + 1) + b"\n{}\n")

    class Connection:
        def settimeout(self, value):
            assert value > 0

        def recv(self, size):
            events.append("recv")
            result = bytes(pending[:size])
            del pending[:size]
            return result

        def sendall(self, response):
            assert pending  # original Linux reset trigger: unread bytes remain
            assert json.loads(response) == {"ok": False, "error": "request_rejected"}
            events.append("send")

        def shutdown(self, how):
            assert how == socket.SHUT_WR
            events.append("half-close")

    calls = []
    broker_wire.serve_connection(Connection(), lambda request: calls.append(request) or {})
    assert pending == b"" and calls == []
    assert events.index("send") < events.index("half-close") < len(events) - 1
    assert events[events.index("half-close") + 1] == "recv"


def test_drain_byte_cap_with_continuous_input(monkeypatch):
    monkeypatch.setattr(broker_wire.time, "monotonic", lambda: 100.0)
    sizes = []

    class Connection:
        def settimeout(self, value):
            assert 0 < value <= broker_wire.DRAIN_GRACE + 0.00001

        def recv(self, size):
            sizes.append(size)
            return b"x" * size

    broker_wire._drain_after_response(Connection(), 102.0)
    assert sum(sizes) == broker_wire.MAX_DRAIN


@pytest.mark.parametrize("remaining", [0.012, 2.0])
def test_drain_uses_smaller_original_deadline_or_grace(monkeypatch, remaining):
    now = [100.0]
    monkeypatch.setattr(broker_wire.time, "monotonic", lambda: now[0])
    timeouts = []

    class Connection:
        def settimeout(self, value):
            timeouts.append(value)

        def recv(self, size):
            now[0] += 0.004
            return b"x"

    broker_wire._drain_after_response(Connection(), 100.0 + remaining)
    expected = min(remaining, broker_wire.DRAIN_GRACE)
    assert timeouts[0] == pytest.approx(expected)
    assert all(b < a for a, b in pairwise(timeouts))
    assert now[0] - 100.0 <= expected + 0.0041


def test_half_close_unblocks_peer_waiting_for_eof_before_peer_close():
    client, server = socket.socketpair()
    errors = []

    def run():
        try:
            with server:
                broker_wire.serve_connection(server, lambda request: {}, budget=0.3)
        except BaseException as exc:
            errors.append(type(exc).__name__)

    worker = threading.Thread(target=run)
    worker.start()
    started = time.monotonic()
    try:
        client.settimeout(1)
        client.sendall(b" " * (broker_wire.MAX_REQUEST + 1) + b"\n" + b"x" * 8192)
        response = bytearray()
        while block := client.recv(4096):
            response.extend(block)
        assert json.loads(response) == {"ok": False, "error": "request_rejected"}
        # Keep the read-complete peer open: bounded grace must still terminate.
        worker.join(timeout=0.5)
        assert not worker.is_alive() and not errors
        assert time.monotonic() - started < 0.8
    finally:
        client.close()
        worker.join(timeout=1)


def test_drain_peer_disconnect_is_safe():
    class Connection:
        def settimeout(self, value):
            pass

        def recv(self, size):
            raise ConnectionResetError

    broker_wire._drain_after_response(Connection(), time.monotonic() + 1)
