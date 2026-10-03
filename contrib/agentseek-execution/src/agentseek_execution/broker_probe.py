"""Trusted host PoC client: fixed operations, key from private file, no raw proxy."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

from .models import canonical, require
from .secure_ownership import load_service_key


def exchange(path: Path, request: dict, *, budget: float = 40) -> dict:
    deadline = time.monotonic() + budget
    data = bytearray()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(budget)
        client.connect(str(path))
        client.sendall((canonical(request) + "\n").encode())
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            require(remaining > 0 and len(data) <= 65536)
            client.settimeout(remaining)
            block = client.recv(min(4096, 65537 - len(data)))
            require(bool(block))
            data.extend(block)
    require(len(data) <= 65536)
    result = json.loads(data)
    require(type(result) is dict)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="M2 fixed synthetic Broker probe; no shell/Volume forwarding")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument(
        "method",
        choices=(
            "list",
            "create",
            "inspect",
            "connect",
            "execute",
            "slow",
            "read",
            "write",
            "cancel",
            "delete",
            "volume",
        ),
    )
    parser.add_argument("--request-id", help="UUID hex; reuse after lost reply, never silently generate another")
    parser.add_argument("--kind", choices=("direct", "work"))
    parser.add_argument("--resource-id")
    args = parser.parse_args()
    try:
        request = {"key": load_service_key(args.key_file), "method": args.method}
        if args.method == "create":
            require(args.request_id is not None and args.kind is not None and args.resource_id is None)
            request.update(request_id=args.request_id, kind=args.kind)
        elif args.method != "list":
            require(args.resource_id is not None and args.request_id is None and args.kind is None)
            request["resource_id"] = args.resource_id
        else:
            require(args.resource_id is None and args.request_id is None and args.kind is None)
        result = exchange(args.socket, request)
        print(canonical(result))
        if result.get("ok") is not True:
            sys.exit(3)
    except Exception:
        print("BROKER_PROBE_FAILED", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
