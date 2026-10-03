"""Real process-boundary failures; synthetic transport, never platform access."""

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from agentseek_execution.m3_create_receipt import CreateBinding, CreateReceiptVault
from agentseek_execution.models import Code, ContractError
from agentseek_execution.worker_process import run_worker

# The child deliberately stalls/exits after a real durable vault reservation.
# This tests the existing process supervisor, not a deployable create launcher.
CHILD = r"""
import json, os, sys, time
from pathlib import Path

value = json.loads(sys.stdin.buffer.read())
sys.path.insert(0, value["test_source"])
from agentseek_execution.m3_create_receipt import CreateBinding, CreateReceiptVault
vault = CreateReceiptVault(Path(value["directory"]), bytes.fromhex(value["test_key"]))
binding = vault.reserve_now(CreateBinding(**value["binding"]))
if value["mode"] == "sealed_then_exit":
    vault.seal_response(binding, json.dumps({
        "sandboxID": "synthetic-vm", "templateID": binding.template_id,
        "domain": binding.domain, "trafficAccessToken": "synthetic-private-token",
        "envdAccessToken": None,
    }).encode())
    os._exit(23)
if value["mode"] == "exit":
    os._exit(23)
if value["mode"] == "descendant":
    if os.fork() != 0:
        os._exit(0)
if value["mode"] == "partial_output":
    sys.stdout.write('{"schema":')
    sys.stdout.flush()
time.sleep(60)
"""


@pytest.mark.parametrize("mode", ["timeout", "exit", "partial_output", "descendant", "sealed_then_exit"])
def test_real_child_failure_cannot_reopen_create_slot(tmp_path, mode):
    directory = tmp_path.resolve() / "vault"
    directory.mkdir(mode=0o700)
    binding = CreateBinding(
        "run", "create", "tpl", "boot", "a" * 64, "b" * 64, "c" * 64, "0" * 64, "example.invalid", True
    )
    payload = json.dumps({
        "directory": str(directory),
        "binding": asdict(binding),
        "test_key": (b"k" * 32).hex(),
        "mode": mode,
        "test_source": str(Path(__file__).resolve().parents[1] / "src"),
    }).encode()
    # Test-only synthetic key crosses stdin; production installation must pass
    # private key references instead. The test pins this checkout's source path;
    # this import override is not exposed by any production launcher.
    started = time.monotonic()
    with pytest.raises(ContractError) as error:
        run_worker([sys.executable, "-I", "-c", CHILD], payload, budget=2, environment={})
    assert time.monotonic() - started < 5  # includes launch and group cleanup allowance
    assert error.value.code == Code.UNKNOWN
    assert "synthetic" not in str(error.value)
    assert len(list(directory.glob("*.intent"))) == 1
    assert len(list(directory.glob("*.receipt"))) == (1 if mode == "sealed_then_exit" else 0)
    # Reopen with a new vault instance; no reset/retry path is allowed even if
    # no usable receipt reference reached the parent or the response was sealed.
    with pytest.raises(FileExistsError):
        CreateReceiptVault(directory, b"k" * 32).reserve_now(binding)
    for path in directory.iterdir():
        assert b"synthetic-private-token" not in path.read_bytes()
