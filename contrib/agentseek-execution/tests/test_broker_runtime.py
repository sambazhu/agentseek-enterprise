import json
import os
import socket
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from agentseek_execution.broker_access import AccessGate, CredentialRegistry, Principal, Resource
from agentseek_execution.broker_clock import BrokerClock
from agentseek_execution.broker_wire import Dispatcher, serve_connection, serve_unix
from agentseek_execution.models import ContractError, Scope
from agentseek_execution.secure_ledger import SecureLedger
from agentseek_execution.secure_ownership import SecureOwnership


def test_clock_rollback_forward_jump_and_restart(tmp_path):
    key = os.urandom(32)
    ledger = SecureLedger(tmp_path / "ledger", key)
    ticks = [100.0, 10.0]
    clock = BrokerClock(ledger, wall=lambda: ticks[0], monotonic=lambda: ticks[1])
    ticks[:] = [90, 15]
    assert clock.now() == 105
    ticks[:] = [200, 16]
    assert clock.now() == 200
    ticks[:] = [100, 20]
    assert clock.now() == 204
    ledger.close()
    reopened = SecureLedger(tmp_path / "ledger", key)
    try:
        with pytest.raises(ContractError):
            BrokerClock(reopened, wall=lambda: 203, monotonic=lambda: 0)
        new = BrokerClock(reopened, wall=lambda: 205, monotonic=lambda: 1)
        assert new.now() == 205
    finally:
        reopened.close()


def test_clock_tamper_and_monotonic_rollback_fail(tmp_path):
    ledger = SecureLedger(tmp_path / "ledger", os.urandom(32))
    mono = [10.0]
    clock = BrokerClock(ledger, wall=lambda: 100, monotonic=lambda: mono[0])
    try:
        mono[0] = 9
        with pytest.raises(ContractError):
            clock.now()
        mono[0] = 11
        ledger.db.execute("UPDATE broker_clock SET sealed='broken'")
        with pytest.raises(ContractError):
            clock.now()
    finally:
        ledger.close()


def exchange(payload, dispatch, budget=0.5):
    client, server = socket.socketpair()

    def run():
        with server:
            serve_connection(server, dispatch, budget=budget)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        client.settimeout(2)
        client.sendall(payload)
        response = bytearray()
        while block := client.recv(4096):
            response.extend(block)
        return json.loads(response)
    finally:
        client.close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize("payload", [b'{"key":"a","key":"b"}\n', b"[]\n", b"not-json\n", b"{}\n{}\n"])
def test_bad_framing_does_not_invoke_dispatch(payload):
    calls = []
    result = exchange(payload, lambda value: calls.append(value) or {})
    assert not result["ok"]
    assert calls == []


def test_handler_exception_never_leaks_secret():
    def broken(request):
        raise RuntimeError("private-credential-and-provider-handle")

    result = exchange(b"{}\n", broken)
    assert result == {"ok": False, "error": "request_rejected"}


def test_oversized_request_and_response_rejected():
    calls = []
    result = exchange(b" " * 65537 + b"\n", lambda value: calls.append(value) or {})
    assert result["ok"] is False and calls == []
    result = exchange(b"{}\n", lambda value: {"large": "x" * 65536})
    assert result == {"ok": False, "error": "request_rejected"}


def test_slow_unterminated_request_is_bounded():
    client, server = socket.socketpair()
    start = time.monotonic()

    def run():
        with server:
            serve_connection(server, lambda request: {}, budget=0.15)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        for _ in range(30):
            try:
                client.sendall(b" ")
            except OSError:
                break
            time.sleep(0.015)
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert time.monotonic() - start < 0.8
    finally:
        client.close()


def test_discovery_rejects_identity_time_and_unimplemented_commands(tmp_path):
    scope = Scope("tenant", "de", "chat", "user")
    registry = CredentialRegistry()
    key = os.urandom(32).hex()
    registry.register(key, Principal("a", frozenset({scope})))
    store = SecureOwnership(tmp_path / "ownership", os.urandom(32))
    store.register(Resource("public", "a", scope, "never-return-platform-handle"))
    dispatch = Dispatcher(AccessGate(registry, store))
    try:
        assert dispatch({"key": key, "method": "list"}) == {"resource_ids": ("public",)}
        assert dispatch({"key": key, "method": "inspect", "resource_id": "public"}) == {"resource_id": "public"}
        for field in ("now", "tenant", "requester", "template", "allow_internet_access"):
            with pytest.raises(ContractError):
                dispatch({"key": key, "method": "list", field: "forged"})
        for method in ("create", "execute", "delete", "raw_proxy", "volume"):
            with pytest.raises(ContractError):
                dispatch({"key": key, "method": method})
    finally:
        store.close()


@pytest.fixture
def socket_directory():
    # Keep AF_UNIX path below macOS/Linux limits without skipping the oracle.
    with TemporaryDirectory(prefix="m2-", dir="/tmp") as directory:
        yield Path(directory).resolve()


def test_unix_listener_real_request_and_cleanup(socket_directory):
    directory = socket_directory
    path = directory / "b.sock"
    stop = threading.Event()
    worker = threading.Thread(target=serve_unix, args=(path, lambda request: {"pong": True}, stop))
    worker.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        deadline = time.monotonic() + 2
        while True:
            try:
                client.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                assert time.monotonic() < deadline
                time.sleep(0.01)
        client.settimeout(1)
        client.sendall(b"{}\n")
        assert json.loads(client.recv(1024))["result"] == {"pong": True}
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        client.close()
        stop.set()
        worker.join(timeout=3)
    assert not worker.is_alive() and not path.exists()
