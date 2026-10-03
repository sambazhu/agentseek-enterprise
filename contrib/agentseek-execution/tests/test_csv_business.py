"""Synthetic sandbox executes only the shipped fixed program, never user code."""

import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys

import pytest

from agentseek_execution.business_execution import BusinessRequest, run_business
from agentseek_execution.csv_business import BusinessStore, CsvBusinessBackend, csv_command, decode_csv_result


class SyntheticSandboxProvider:
    """TEST ONLY local process; this is not a security boundary or Cube proof."""

    def __init__(self, store, owner, fail=None):
        self.store, self.owner, self.fail = store, owner, fail
        self.events = []

    def validate_request(self, request, data):
        pass  # Test-only provider, no production approval is fabricated.

    def create(self, attempt):
        self.events.append("create")
        if self.fail == "create":
            raise ValueError("unknown create")

    def run(self, attempt, command, timeout):
        self.events.append("execute")
        if self.fail == "execute":
            raise ValueError("remote failure")
        args = shlex.split(command)
        assert args[:3] == ["python3", "-I", "-c"]
        # Script and encoded fixture input only; no shell and no model code.
        result = subprocess.run([sys.executable, *args[1:]], capture_output=True,
                                timeout=timeout, check=True, env={})
        return result.stdout.decode()

    def destroy(self, attempt):
        self.events.append("destroy")
        with sqlite3.connect(self.store.path) as db:
            row = db.execute("SELECT artifact FROM attempts WHERE id=?", (attempt,)).fetchone()
        if self.fail is None:
            assert row[0] and self.store.read(self.owner, row[0])
        return self.fail not in {"destroy", "create"}


def setup_backend(tmp_path, fail=None, data=b"group,amount\nA,1.25\nB,2\nA,3.75\n"):
    tmp_path.chmod(0o700)
    store = BusinessStore(tmp_path.resolve())
    request = BusinessRequest("request", "owner", "input", "sum amount by group")
    provider = SyntheticSandboxProvider(store, request.owner_id, fail)
    backend = CsvBusinessBackend(request=request, store=store, provider=provider,
                                 authorize=lambda r: True, load_input=lambda r: data)
    return store, request, provider, backend


def test_real_csv_bytes_persist_before_cleanup_and_survive_reopen(tmp_path):
    store, request, provider, backend = setup_backend(tmp_path)
    result = run_business(request, backend)
    assert result.state == "succeeded" and result.cleanup_confirmed
    assert provider.events == ["create", "execute", "destroy"]
    assert BusinessStore(tmp_path.resolve()).read("owner", result.artifact_ref) == b"group,total\nA,5.00\nB,2\n"
    with pytest.raises(ValueError):
        store.read("other", result.artifact_ref)
    with pytest.raises(ValueError):
        run_business(request, backend)
    assert provider.events.count("create") == 1


@pytest.mark.parametrize("failure,state", [("create", "reconciling"), ("execute", "failed"), ("destroy", "reconciling")])
def test_remote_failures_keep_truthful_state(tmp_path, failure, state):
    store, request, provider, backend = setup_backend(tmp_path, failure)
    result = run_business(request, backend)
    assert result.state == state
    assert provider.events[-1] == "destroy"
    if state == "reconciling":
        with pytest.raises(ValueError):
            store.reserve(BusinessRequest("new", "owner", "input", "sum"))
    if failure == "destroy":
        assert store.read("owner", result.artifact_ref)


@pytest.mark.parametrize("data", [b"", b"x" * 65537])
def test_oversize_input_rejected_before_create(tmp_path, data):
    _, request, provider, backend = setup_backend(tmp_path, data=data)
    with pytest.raises(ValueError):
        run_business(request, backend)
    assert provider.events == []


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "1e9999", "0.0000001", "not a number"])
def test_bad_amount_fails_and_cleans_up(tmp_path, amount):
    _, request, provider, backend = setup_backend(tmp_path, fail="execute", data=f"group,amount\nA,{amount}\n".encode())
    # Test the shipped guest program itself, not a fake parser.
    args = shlex.split(csv_command(backend._load_input(request)))
    result = subprocess.run([sys.executable, *args[1:]], capture_output=True, timeout=5, env={})
    assert result.returncode != 0


def test_csv_formula_label_escaped(tmp_path):
    store, request, _, backend = setup_backend(tmp_path, data=b"group,amount\n=SUM(1),2\n")
    result = run_business(request, backend)
    assert b"'=SUM(1),2" in store.read("owner", result.artifact_ref)


def test_crashed_reservation_blocks_new_create(tmp_path):
    store, request, _, _ = setup_backend(tmp_path)
    store.reserve(request)
    reopened = BusinessStore(tmp_path.resolve())
    with pytest.raises(ValueError):
        reopened.reserve(BusinessRequest("new", "owner", "input", "sum"))


def test_artifact_corruption_detected(tmp_path):
    store, request, _, backend = setup_backend(tmp_path)
    outcome = run_business(request, backend)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE artifacts SET data=?", (b"corrupt",))
    with pytest.raises(ValueError):
        store.read("owner", outcome.artifact_ref)
