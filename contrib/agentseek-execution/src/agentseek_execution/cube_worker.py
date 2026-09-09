"""Pinned SDK worker. Private stdin protocol, never a public Broker route."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .cube_journal import CubeJournal
from .models import Code, ContractError, canonical, require
from .secure_ownership import load_key, load_service_key

PROBES = {
    "direct": "printf 'M2_DIRECT_OK'",
    "work": "printf 'M2_WORK_OK'",
    "slow": "sleep 130; printf 'M2_SLOW_DONE'",
    "write": "printf 'M2_FILE_OK' > /tmp/agentseek-m2.txt; printf 'M2_WRITE_OK'",
    "read": "cat /tmp/agentseek-m2.txt",
}


def _find_receipt(listing, marker: dict, template_id: str, config) -> dict:
    from cubesandbox import Sandbox
    from cubesandbox._exceptions import SandboxNotFoundError

    # Zero matches is not evidence of non-creation; never free capacity on it.
    require(type(listing) is list, Code.UNKNOWN)
    matches = []
    for item in listing:
        require(type(item) is dict, Code.UNKNOWN)
        if item.get("templateID") != template_id:
            continue
        candidate = Sandbox(item, config=config)
        try:
            info = candidate.get_info()  # list v1 does not promise metadata
        except SandboxNotFoundError:
            continue
        finally:
            candidate.close()
        metadata = info.get("metadata") or {}
        if all(metadata.get(key) == value for key, value in marker.items()):
            matches.append(item)
    require(len(matches) == 1, Code.UNKNOWN)
    return {"sandboxID": matches[0]["sandboxID"], "templateID": template_id}


def perform(request: dict) -> dict:
    # Import only inside the worker; the gateway/parent never imports the SDK.
    from cubesandbox import Config, Sandbox
    from cubesandbox._exceptions import SandboxNotFoundError

    config = request["config"]
    journal = CubeJournal(Path(config["runtime"]), load_key(Path(config["encryption_key_file"])))
    sandbox = None
    try:
        job = journal.require_job(request["resource_id"])
        cfg = Config(
            api_url=config["api_url"],
            api_key=load_service_key(Path(config["cube_key_file"])),
            template_id=config["template_id"],
            proxy_node_ip="127.0.0.1",
            proxy_port=config["proxy_port"],
            sandbox_domain=config["sandbox_domain"],
            request_timeout=5,
        )
        operation = request["operation"]
        marker = {"agentseek_run_id": config["run_id"], "agentseek_create_token": job["lease"]["create_token"]}
        receipt = job.get("receipt")
        if operation == "create":
            require(receipt is None and job["state"] == "creating", Code.CONFLICT)
            sandbox = Sandbox.create(
                template=config["template_id"],
                timeout=120,
                config=cfg,
                metadata=marker,
                allow_internet_access=False,
                network={"allow_public_traffic": False},
                lifecycle={"on_timeout": "kill", "auto_resume": False},
            )
            # Persist private SDK reconstruction material before acknowledging the
            # worker. Version-pinned _data seam covered by real-SDK HTTP tests.
            receipt = {
                key: sandbox._data[key]
                for key in ("sandboxID", "templateID", "domain", "envdAccessToken", "trafficAccessToken")
                if key in sandbox._data
            }
            journal.patch(job["resource_id"], receipt=receipt)
            require(bool(sandbox.traffic_access_token), Code.UNKNOWN)
            require(sandbox.template_id == config["template_id"], Code.UNKNOWN)
            return {"status": "created"}
        if receipt is None:
            require(operation == "delete", Code.UNKNOWN)
            # Lost creation response: only exact run + create-token matches are
            # candidates. Zero matches is NOT proof creation never occurred.
            receipt = _find_receipt(Sandbox.list(config=cfg), marker, config["template_id"], cfg)
            journal.patch(job["resource_id"], receipt=receipt)
        sandbox = Sandbox(receipt, config=cfg)
        try:
            info = sandbox.get_info()
        except SandboxNotFoundError:
            if operation in {"delete", "inspect", "connect"}:
                return {"status": "stopped"}
            raise ContractError(Code.UNKNOWN) from None
        metadata = info.get("metadata") or {}
        require(all(metadata.get(key) == value for key, value in marker.items()), Code.DENIED)
        require(info.get("templateID") == config["template_id"], Code.DENIED)
        if operation == "delete":
            try:
                sandbox.kill()
            except SandboxNotFoundError:
                return {"status": "stopped"}
            try:
                sandbox.get_info()
            except SandboxNotFoundError:
                return {"status": "stopped"}
            raise ContractError(Code.UNKNOWN)
        require(info.get("state") == "running", Code.UNKNOWN)
        if operation in {"inspect", "connect"}:
            # Do not call SDK connect(): it may resume a paused sandbox. No token
            # or host URL leaves the worker; only a verified state is returned.
            return {"status": "running"}
        require(operation in PROBES and bool(sandbox.traffic_access_token), Code.DENIED)
        result = sandbox.commands.run(PROBES[operation], timeout=5)
        require(result.exit_code == 0 and len(result.stdout.encode()) <= 4096, Code.UNKNOWN)
        return {"status": "executed", "stdout": result.stdout}
    finally:
        if sandbox is not None:
            sandbox.close()
        journal.close()


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read(65537))
        require(type(request) is dict)
        result = perform(request)
        sys.stdout.write(canonical(result))
    except Exception:
        # Worker protocol failure: no traceback, credential or provider text.
        sys.exit(2)


if __name__ == "__main__":
    main()
