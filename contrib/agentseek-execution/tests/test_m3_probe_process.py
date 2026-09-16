import json
import os
import sys
import time

import pytest
from agentseek_execution import m3_probe_process as worker
from agentseek_execution.m3_auth_probe import Observation
from agentseek_execution.models import ContractError
from agentseek_execution.worker_process import run_worker


@pytest.fixture
def config(tmp_path):
    root = tmp_path.resolve() / "private"
    root.mkdir(mode=0o700)
    path = root / "probe.json"
    path.write_text(
        json.dumps({
            "schema": 1,
            "endpoint": "E1",
            "host": "vm.example.invalid",
            "proxy_port": 13080,
            "credentials": {"api_key": "synthetic-secret", "traffic": "traffic", "envd": "envd"},
            "layer": "envd",
            "state": "missing",
            "replacement": None,
        })
    )
    path.chmod(0o600)
    return path


def test_prepare_is_offline_and_omits_header(config):
    request = worker.prepare(config)
    assert "X-Access-Token" not in request.headers
    assert "synthetic-secret" not in repr(request)


@pytest.mark.parametrize("alias", ["e2b", "cube"])
def test_v2_private_config_envd_none_and_explicit_alias(config, alias):
    value = json.loads(config.read_text())
    value.update(schema=2, traffic_alias=alias, layer="traffic", state="correct")
    value["credentials"]["envd"] = None
    config.write_text(json.dumps(value))
    request = worker.prepare(config)
    assert "X-Access-Token" not in request.headers
    assert request.headers[alias + "-traffic-access-token"] == "traffic"
    other = "cube" if alias == "e2b" else "e2b"
    assert other + "-traffic-access-token" not in request.headers


@pytest.mark.parametrize("mode", ["v1_none", "v1_alias", "v2_no_alias", "v2_both", "v2_envd_test"])
def test_schema_versions_do_not_silently_change_semantics(config, mode):
    value = json.loads(config.read_text())
    if mode == "v1_none":
        value["credentials"]["envd"] = None
    elif mode == "v1_alias":
        value["traffic_alias"] = "cube"
    else:
        value["schema"] = 2
        if mode == "v2_both":
            value["traffic_alias"] = "both"
        elif mode == "v2_envd_test":
            value["traffic_alias"] = "cube"
            value["credentials"]["envd"] = None
    config.write_text(json.dumps(value))
    with pytest.raises(ContractError):
        worker.prepare(config)


@pytest.mark.parametrize("mode", ["public", "parent", "symlink", "hardlink", "duplicate", "extra", "oversize", "fifo"])
def test_private_config_rejects(config, mode):
    if mode == "public":
        config.chmod(0o644)
    elif mode == "parent":
        config.parent.chmod(0o755)
    elif mode == "symlink":
        link = config.parent / "link"
        link.symlink_to(config)
        config = link
    elif mode == "hardlink":
        os.link(config, config.parent / "link")
    elif mode == "duplicate":
        config.write_text('{"schema":1,"schema":1}')
    elif mode == "extra":
        value = json.loads(config.read_text())
        value["command"] = "id"
        config.write_text(json.dumps(value))
    elif mode == "oversize":
        config.write_bytes(b"x" * 65537)
    else:
        config.unlink()
        os.mkfifo(config, 0o600)
    with pytest.raises(ContractError):
        worker.prepare(config)


def test_launcher_fixed_stdin_and_budget(config, monkeypatch):
    deadline = time.monotonic() + 120
    expected = {
        "status": 403,
        "body_size": 0,
        "body_sha256": None,
        "complete": True,
        "reason": "response",
        "protocol": None,
    }

    def run(command, request, *, budget, environment):
        assert command == [sys.executable, "-I", "-m", "agentseek_execution.m3_probe_process"]
        assert json.loads(request) == {"path": str(config), "deadline": deadline}
        assert b"synthetic-secret" not in request
        assert budget == 10 and environment == {}
        return json.dumps(expected).encode()

    monkeypatch.setattr(worker, "run_worker", run)
    assert worker.observe_isolated(config, verified_deadline=deadline) == expected


def test_child_observes_once_without_returning_secrets(config, monkeypatch):
    calls = []

    def observe(request, *, remaining_seconds):
        calls.append(request)
        assert remaining_seconds >= 10
        return Observation(403, 0, None, True, "response")

    monkeypatch.setattr(worker, "observe", observe)
    result = worker.perform({"path": str(config), "deadline": time.monotonic() + 120})
    assert len(calls) == 1 and "synthetic-secret" not in json.dumps(result)


def test_stale_deadline_before_io(config, monkeypatch):
    monkeypatch.setattr(worker, "prepare", lambda _: pytest.fail("read config"))
    with pytest.raises(ContractError):
        worker.perform({"path": str(config), "deadline": time.monotonic() + 9})


def test_real_child_rejects_malformed_stdin_without_stdout():
    with pytest.raises(ContractError):
        run_worker(
            [sys.executable, "-I", "-m", "agentseek_execution.m3_probe_process"],
            b'{"path":"synthetic-secret","path":"duplicate"}',
            budget=2,
            environment={},
        )


def test_outer_timeout_never_retried(config, monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append(1)
        raise TimeoutError

    monkeypatch.setattr(worker, "run_worker", timeout)
    with pytest.raises(TimeoutError):
        worker.observe_isolated(config, verified_deadline=time.monotonic() + 120)
    assert calls == [1]
