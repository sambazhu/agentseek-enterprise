import errno
import fcntl
import os
from pathlib import Path

import pytest
from agentseek_execution.broker_daemon import read_config
from agentseek_execution.secure_ownership import load_key, load_service_key, private_file
from live_cube_recovery import run
from test_broker_daemon import _deployment


def test_config_and_all_credentials_open_readonly_on_simulated_readonly_mount(tmp_path, monkeypatch):
    config_path, config = _deployment(tmp_path)
    original = os.open
    observed = []

    def readonly_open(path, flags, *args, **kwargs):
        if Path(path).parent == tmp_path:
            observed.append(flags)
            if flags & os.O_ACCMODE != os.O_RDONLY:
                raise OSError(errno.EROFS, "synthetic read-only mount")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", readonly_open)
    assert read_config(config_path) == config
    assert len(load_key(Path(config["encryption_key_file"]))) == 32
    assert load_service_key(Path(config["cube_key_file"])) == "c" * 40
    for principal in config["principals"]:
        assert len(load_service_key(Path(principal["key_file"]))) == 40
    assert len(observed) == 5
    assert all(flags & os.O_NOFOLLOW for flags in observed)


def test_runtime_files_keep_readwrite_mode(tmp_path):
    descriptor = private_file(tmp_path / "lock", create=True)
    try:
        assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDWR
    finally:
        os.close(descriptor)


def test_live_fixture_cannot_run_while_broker_lock_held(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    descriptor = private_file(runtime / "broker.lock", create=True)
    destination = tmp_path / "new-fixture"
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            run({"runtime": str(runtime)}, destination, recover_only=False)
        assert not destination.exists()
    finally:
        os.close(descriptor)
