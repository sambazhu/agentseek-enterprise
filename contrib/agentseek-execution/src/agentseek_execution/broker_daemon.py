"""Explicit PoC daemon. No import-time configuration or gateway integration."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import stat
import sys
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit

from .broker_access import CredentialRegistry, Principal
from .broker_clock import BrokerClock
from .broker_lifecycle import Lifecycle
from .broker_wire import serve_unix
from .cube_journal import CubeJournal
from .models import Code, Scope, canonical, require
from .secure_ledger import SecureLedger
from .secure_ownership import load_key, load_service_key, private_file
from .worker_process import run_worker


def read_config(path: Path) -> dict:
    fd = private_file(path, readonly=True)
    try:
        raw = os.read(fd, 65537)
    finally:
        os.close(fd)
    require(len(raw) <= 65536)
    config = json.loads(raw)
    required = {
        "runtime",
        "encryption_key_file",
        "cube_key_file",
        "api_url",
        "ca_file",
        "template_id",
        "proxy_port",
        "sandbox_domain",
        "run_id",
        "principals",
        "preflight_command",
    }
    require(type(config) is dict and set(config) == required)
    parsed = urlsplit(config["api_url"])
    require(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )
    require(type(config["proxy_port"]) is int and 1024 <= config["proxy_port"] <= 65535)
    for name in ("template_id", "sandbox_domain", "run_id"):
        require(type(config[name]) is str and 0 < len(config[name]) <= 256)
    require(type(config["preflight_command"]) is list and bool(config["preflight_command"]))
    require(all(type(arg) is str and bool(arg) for arg in config["preflight_command"]))
    require(Path(config["preflight_command"][0]).is_absolute())
    require(type(config["principals"]) is list and len(config["principals"]) == 2)
    for name in ("runtime", "encryption_key_file", "cube_key_file", "ca_file"):
        require(Path(config[name]).is_absolute())
    require(Path(config["ca_file"]).is_file(), Code.DENIED)
    return config


def serve(config: dict) -> None:
    runtime = Path(config["runtime"])
    encryption = load_key(Path(config["encryption_key_file"]))
    control = load_service_key(Path(config["cube_key_file"]))
    require(control.encode() != encryption and control != encryption.hex(), Code.DENIED)
    ledger = SecureLedger(runtime, encryption)
    lock_path = runtime / "broker.lock"
    try:
        lock = private_file(lock_path, create=True)
    except FileExistsError:
        lock = private_file(lock_path)
    journal = None
    old_signals = {}
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = CubeJournal(runtime, encryption)
        socket_path = runtime / "broker.sock"
        if socket_path.exists() or socket_path.is_symlink():
            info = socket_path.lstat()
            previous = journal.socket_identity()
            require(
                stat.S_ISSOCK(info.st_mode)
                and info.st_uid == os.getuid()
                and previous == {"dev": info.st_dev, "ino": info.st_ino},
                Code.DENIED,
            )
            socket_path.unlink()  # exclusive daemon lock + exact persisted owned inode
        clock = BrokerClock(ledger)
        credentials = CredentialRegistry()
        ids = set()
        for entry in config["principals"]:
            require(set(entry) == {"id", "key_file", "scope"})
            require(entry["id"] not in ids)
            ids.add(entry["id"])
            key = load_service_key(Path(entry["key_file"]))
            require(key != control and key != encryption.hex() and key.encode() != encryption, Code.DENIED)
            credentials.register(key, Principal(entry["id"], frozenset({Scope(**entry["scope"])})))
        worker_env = {"REQUESTS_CA_BUNDLE": config["ca_file"], "SSL_CERT_FILE": config["ca_file"]}

        def worker(resource_id: str, operation: str) -> dict:
            payload = canonical({
                "config": {k: v for k, v in config.items() if k not in {"principals", "preflight_command"}},
                "resource_id": resource_id,
                "operation": operation,
            }).encode()
            result = run_worker(
                [sys.executable, "-I", "-m", "agentseek_execution.cube_worker"],
                payload,
                budget=10,
                environment=worker_env,
            )
            value = json.loads(result)
            require(type(value) is dict, Code.UNKNOWN)
            return value

        def gate() -> None:
            result = run_worker(config["preflight_command"], b"", budget=10, environment={"PATH": "/usr/bin:/bin"})
            require(result.strip() == b"READY", Code.DENIED)

        controller = Lifecycle(
            credentials,
            journal,
            ledger,
            clock,
            worker,
            gate,
            deployment=config["run_id"],
            template_id=config["template_id"],
        )

        def on_bound(dev: int, ino: int) -> None:
            journal.socket_identity({"dev": dev, "ino": ino})

        stop = Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_signals[sig] = signal.signal(sig, lambda *_args: stop.set())
        controller.recover()
        try:
            serve_unix(socket_path, controller, stop, request_budget=25, tick=controller.tick, on_bound=on_bound)
        finally:
            controller.recover()
    finally:
        for sig, handler in old_signals.items():
            signal.signal(sig, handler)
        if journal is not None:
            journal.close()
        ledger.close()
        os.close(lock)


def main() -> None:
    parser = argparse.ArgumentParser(description="M2 synthetic Cube Broker; no production Agent integration")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        if args.check_config:
            print("CONFIG_SCHEMA_OK")
        else:
            serve(config)
    except Exception:
        print("BROKER_START_OR_RUNTIME_FAILED", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
