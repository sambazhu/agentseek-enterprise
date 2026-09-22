"""Only synthetic local children; no real create, approvals, or node access."""

import hashlib
import json
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentseek_execution import execution_diagnostic as diagnostic
from agentseek_execution import worker_process as worker
from agentseek_execution import business_lifecycle_provider as lifecycle
from agentseek_execution.models import ContractError
from test_business_lifecycle_provider import provider


def events(caplog):
    return [json.loads(r.getMessage().split("execution_stage ", 1)[1])
            for r in caplog.records if r.getMessage().startswith("execution_stage ")]


def test_real_worker_pid_cleanup_and_redaction(caplog):
    secret = "synthetic-private-payload-not-for-log"
    with diagnostic.phase("provider_create", ("owner", "request")):
        result = worker.run_worker([sys.executable, "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            secret.encode(), budget=2, environment={"PRIVATE": secret})
    assert result == secret.encode() and secret not in caplog.text
    records = events(caplog)
    assert len({r["scope_id"] for r in records}) == 1
    assert {r["request_sha256"] for r in records} == {
        hashlib.sha256(json.dumps(["owner", "request"]).encode()).hexdigest()}
    spawned = next(r for r in records if r["event"] == "spawned")
    reaped = next(r for r in records if r["event"] == "reaped")
    assert spawned["child_pid"] > 0 and spawned["child_pid"] != os.getpid()
    assert spawned["worker_id"] == reaped["worker_id"] and reaped["returncode"] == 0
    assert records[-1]["stage"] == "provider_create" and records[-1]["event"] == "complete"
    assert all(set(r) == {"scope_id", "request_sha256", "stage", "event", "pid", "thread_id",
        "epoch_ms", "mono_ms", "elapsed_ms", "error", "worker_id", "child_pid", "returncode"} for r in records)


def test_real_timeout_reaps_child(caplog):
    with pytest.raises(ContractError):
        worker.run_worker([sys.executable, "-c", "import time; time.sleep(20)"],
                          b"", budget=.15, environment={})
    records = events(caplog)
    assert any(r["event"] == "kill_sent" for r in records)
    assert any(r["event"] == "reaped" and r["returncode"] < 0 for r in records)
    assert records[-1]["event"] == "failed"


def test_spawn_failure_preserves_exception_and_no_fake_cleanup(monkeypatch, caplog):
    failure = OSError("synthetic-sensitive-path")
    def fail(*a, **k): raise failure
    monkeypatch.setattr(worker.subprocess, "Popen", fail)
    with pytest.raises(OSError) as caught:
        worker.run_worker(["synthetic-command"], b"", budget=1, environment={})
    assert caught.value is failure
    assert "synthetic-sensitive-path" not in caplog.text
    assert any(r["stage"] == "worker_spawn" and r["error"] == "OSError" for r in events(caplog))
    assert not any(r["stage"] == "worker_cleanup" for r in events(caplog))


@pytest.mark.parametrize("failure", [RuntimeError("private"), KeyboardInterrupt("private")])
def test_exchange_failure_cleanup_and_original_category(monkeypatch, caplog, failure):
    def fail(*a): raise failure
    monkeypatch.setattr(worker, "_exchange", fail)
    expected = ContractError if isinstance(failure, Exception) else KeyboardInterrupt
    with pytest.raises(expected):
        worker.run_worker([sys.executable, "-c", "import time; time.sleep(20)"], b"", budget=1, environment={})
    assert any(r["event"] == "reaped" for r in events(caplog))
    assert any(r["error"] == type(failure).__name__ for r in events(caplog))
    assert "private" not in caplog.text


@pytest.mark.parametrize("failure", [PermissionError("secret"), subprocess.TimeoutExpired("secret", 1)])
def test_cleanup_failure_is_not_reported_reaped(monkeypatch, caplog, failure):
    class Process:
        pid = 999999
        stdin = stdout = None
        def wait(self, timeout): raise failure
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(worker, "_exchange", lambda *a: b"ok")
    def kill(*a):
        if isinstance(failure, PermissionError): raise failure
    monkeypatch.setattr(worker.os, "killpg", kill)
    expected = PermissionError if isinstance(failure, PermissionError) else ContractError
    with pytest.raises(expected):
        worker.run_worker(["synthetic"], b"", budget=1, environment={})
    assert not any(r["event"] == "reaped" for r in events(caplog))
    assert any(r["stage"] == "worker_cleanup" and r["event"] == "failed" for r in events(caplog))
    assert "secret" not in caplog.text


def test_provider_creation_failure_and_no_session_are_distinct(monkeypatch, caplog):
    p = provider(monkeypatch)
    p.validated = True
    p._diagnostic_request = ("owner", "request")
    def fail(*a): raise RuntimeError("private-binding")
    monkeypatch.setattr(lifecycle, "execute", fail)
    with pytest.raises(RuntimeError): p.create("private-attempt")
    assert p.destroy("private-attempt") is False
    records = events(caplog)
    assert any(r["stage"] == "provider_create" and r["event"] == "failed" for r in records)
    assert any(r["stage"] == "provider_destroy" and r["event"] == "skipped" for r in records)
    assert not any(r["stage"] in {"session_reconstruct", "provider_run"} for r in records)
    assert "private-" not in caplog.text


def test_parallel_scopes_are_independent(caplog):
    def run(index):
        with diagnostic.phase("provider_create", ("owner", str(index))):
            diagnostic.emit("intent_save", "complete")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(4)))
    records = events(caplog)
    assert len({r["scope_id"] for r in records}) == 4
    for scope in {r["scope_id"] for r in records}:
        assert len({r["request_sha256"] for r in records if r["scope_id"] == scope}) == 1
    assert diagnostic._scope.get() is None


def test_unknown_fields_and_logger_failure_do_not_leak_or_change_result(monkeypatch, caplog):
    diagnostic.emit("private-stage", "complete")
    diagnostic.emit("launcher", "private-event")
    diagnostic.emit("launcher", "complete", worker_id="private-id")
    assert events(caplog) == []
    def fail(*a, **k): raise RuntimeError("logger unavailable")
    monkeypatch.setattr(logging.getLogger(diagnostic.__name__), "warning", fail)
    with diagnostic.phase("launcher"):
        result = worker.run_worker([sys.executable, "-c", "print('ok')"], b"", budget=2, environment={})
    assert result == b"ok\n" and diagnostic._scope.get() is None
