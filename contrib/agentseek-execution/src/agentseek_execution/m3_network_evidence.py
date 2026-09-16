"""Pinned create-request network intent, not proof of live proxy enforcement.

Read only the original request whose digest the creating worker sealed. Never
infer restricted mode from the presence of a traffic token or an egress flag.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .m3_create_receipt import CreateBinding, CreateReceiptVault
from .m3_probe_process import _decode, config_bytes
from .models import Code, ContractError, require


@dataclass(frozen=True)
class NetworkIntent:
    sandbox_id: str
    restricted: bool
    request_sha256: str
    enforcement_verified: bool = False
    aggregate_ready: bool = False


def read_network_intent(path: Path, *, create: CreateBinding, vault: CreateReceiptVault) -> NetworkIntent:
    """Bind original explicit boolean to the exact sealed guest; no HTTP or writes."""
    try:
        create.validate()
        receipt = vault.read(create)
        vault.read_intent(create)
        raw = config_bytes(path)
        require(hashlib.sha256(raw).hexdigest() == create.request_sha256, Code.DENIED)
        request = _decode(raw)
        require(type(request) is dict and request.get("templateID") == create.template_id, Code.DENIED)
        require(request.get("metadata") == {
            "agentseek_run_id": create.run_id, "agentseek_create_token": create.create_token,
        }, Code.DENIED)
        network = request.get("network")
        require(type(network) is dict and network.get("allowPublicTraffic") is (not create.restricted), Code.DENIED)
        require(config_bytes(path) == raw and vault.read(create) == receipt, Code.DENIED)
        return NetworkIntent(receipt.sandbox_id, create.restricted, create.request_sha256)
    except Exception:
        raise ContractError(Code.DENIED) from None
