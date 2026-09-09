import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from agentseek_execution.broker_daemon import read_config
from agentseek_execution.broker_probe import exchange
from agentseek_execution.models import ContractError
from agentseek_execution.secure_ownership import load_service_key


@pytest.fixture
def deployment():
    with tempfile.TemporaryDirectory(prefix="m2-", dir="/tmp") as directory:
        yield _deployment(Path(directory).resolve())


def _deployment(tmp_path):
    def private(name, content):
        path = tmp_path / name
        path.write_bytes(content)
        path.chmod(0o600)
        return str(path)

    config = {
        "runtime": str(tmp_path / "runtime"),
        "encryption_key_file": private("key", os.urandom(32)),
        "cube_key_file": private("cube", b"c" * 40),
        "api_url": "https://cube.invalid:13000",
        "ca_file": private("ca", b"test-only"),
        "template_id": "tpl-test",
        "proxy_port": 13080,
        "sandbox_domain": "cube.invalid",
        "run_id": "test-only",
        "preflight_command": ["/usr/bin/false"],
        "principals": [
            {
                "id": label,
                "key_file": private(label, label.encode() * 40),
                "scope": {"tenant": "test", "digital_employee": "test", "conversation": label, "requester": label},
            }
            for label in ("a", "b")
        ],
    }
    path = Path(private("config.json", json.dumps(config).encode()))
    return path, config


def launch(config_path):
    root = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.Popen(  # noqa: S603 -- fixed interpreter/module, synthetic private config
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0,sys.argv.pop(1)); "
            "from agentseek_execution.broker_daemon import main; main()",
            root,
            "--config",
            str(config_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def await_socket(process, path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("daemon exited before listen")
        try:
            if exchange(path, {"key": "a" * 40, "method": "list"}, budget=0.2).get("ok"):
                return
        except (OSError, ContractError):
            time.sleep(0.02)
    pytest.fail("daemon did not listen")


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
    return process.communicate()


def test_daemon_real_socket_auth_singleton_and_clean_shutdown(deployment):
    path, config = deployment
    socket_path = Path(config["runtime"]) / "broker.sock"
    process = launch(path)
    try:
        await_socket(process, socket_path)
        assert socket_path.stat().st_mode & 0o777 == 0o600
        assert exchange(socket_path, {"key": "b" * 40, "method": "list"}) == {"ok": True, "result": {"resources": []}}
        assert exchange(socket_path, {"key": "z" * 40, "method": "list"})["ok"] is False
        second = launch(path)
        assert second.wait(timeout=5) == 2
        stop(second)
        assert socket_path.exists()
    finally:
        stdout, stderr = stop(process)
    assert process.returncode == 0 and not socket_path.exists()
    assert stdout == stderr == b""


def test_daemon_hard_exit_reclaims_only_recorded_socket(deployment):
    path, config = deployment
    socket_path = Path(config["runtime"]) / "broker.sock"
    process = launch(path)
    try:
        await_socket(process, socket_path)
        process.kill()
        process.wait(timeout=5)
    finally:
        stop(process)
    assert socket_path.exists()
    restarted = launch(path)
    try:
        await_socket(restarted, socket_path)
    finally:
        stop(restarted)
    socket_path.write_text("foreign-file")
    refused = launch(path)
    assert refused.wait(timeout=5) == 2
    stop(refused)
    assert socket_path.read_text() == "foreign-file"


@pytest.mark.parametrize(
    "update",
    [
        {"api_url": "http://cube.invalid"},
        {"api_url": "https://user:secret@cube.invalid"},
        {"proxy_port": 80},
        {"preflight_command": ["relative-script"]},
        {"arbitrary": True},
    ],
)
def test_daemon_config_rejects_unsafe_settings(deployment, update):
    path, config = deployment
    config.update(update)
    path.write_text(json.dumps(config))
    with pytest.raises(ContractError):
        read_config(path)


def test_daemon_config_permissions_rejected(deployment):
    path, _ = deployment
    path.chmod(0o644)
    with pytest.raises(ContractError):
        read_config(path)


@pytest.mark.parametrize("content", [b"short", b"a" * 257, b"a" * 40 + b"\n\n", b"a" * 39 + b" ", b"a" * 39 + b"\x00"])
def test_service_key_shape_rejected(tmp_path, content):
    path = tmp_path / "key"
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(ContractError):
        load_service_key(path)


def test_service_key_single_newline_accepted_but_loose_permissions_denied(tmp_path):
    path = tmp_path / "key"
    path.write_bytes(b"a" * 40 + b"\n")
    path.chmod(0o600)
    assert load_service_key(path) == "a" * 40
    path.chmod(0o644)
    with pytest.raises(ContractError):
        load_service_key(path)
