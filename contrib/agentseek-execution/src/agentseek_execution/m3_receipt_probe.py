"""Internal receipt-only probe adapter and bounded child, not a live launcher.

Source paths/bindings come from trusted installation, never the probe config.
AEAD authenticates local storage, not root integrity or creation approval. The
caller still needs independent approval, current identity/lifetime/supervision,
and a durable case claim. No creation, donor lifecycle or public-token fallback.
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .m3_auth_probe import Credentials, Request, build_request, observe
from .m3_create_receipt import CreateBinding, CreateReceiptVault
from .m3_probe_dispatch import Binding, _validate_result
from .m3_probe_process import _decode, config_bytes
from .models import Code, ContractError, canonical, require
from .worker_process import run_worker


@dataclass(frozen=True)
class ReceiptProbeSource:
    """Trusted installation descriptor; contains references, not credential values."""

    create: CreateBinding
    vault_directory: Path
    vault_key_file: Path
    api_key_file: Path
    domain: str
    proxy_port: int

    def reference(self) -> str:
        self.create.validate()
        return hashlib.sha256(canonical(asdict(self.create)).encode()).hexdigest()

    def wire(self) -> dict:
        value = asdict(self)
        for key in ("vault_directory", "vault_key_file", "api_key_file"):
            value[key] = str(value[key])
        return value

    @classmethod
    def from_wire(cls, value: dict) -> ReceiptProbeSource:
        require(
            type(value) is dict
            and set(value) == {"create", "vault_directory", "vault_key_file", "api_key_file", "domain", "proxy_port"}
        )
        fields = dict(value)
        require(type(fields["create"]) is dict)
        fields["create"] = CreateBinding(**fields["create"])
        for key in ("vault_directory", "vault_key_file", "api_key_file"):
            require(type(fields[key]) is str)
            fields[key] = Path(fields[key])
        return cls(**fields)


def prepare_receipted(path: Path, *, binding: Binding, source: ReceiptProbeSource) -> Request:
    """Schema 3: approved selectors plus one pinned non-secret receipt reference.

    Correct token is read only from the vault. Wrong token is fresh randomness,
    never user data or a derivative of the real token. Both aliases are omitted
    for missing. Cross-guest and envd rows block until independent evidence exists.
    """
    try:
        require(os.getuid() == 0, Code.DENIED)
        binding.validate()
        require(type(source) is ReceiptProbeSource, Code.DENIED)
        reference = source.reference()
        require(source.domain == source.create.domain, Code.DENIED)
        for key in ("run_id", "create_token", "template_id", "candidate_sha256", "boot_id"):
            require(getattr(binding, key) == getattr(source.create, key), Code.DENIED)
        raw = config_bytes(path)
        require(hashlib.sha256(raw).hexdigest() == binding.config_sha256, Code.DENIED)
        value = _decode(raw)
        require(set(value) == {"schema", "receipt_ref", "endpoint", "layer", "state", "traffic_alias"})
        require(type(value["schema"]) is int and value["schema"] == 3, Code.DENIED)
        require(value["receipt_ref"] == reference and value["layer"] == "traffic", Code.DENIED)
        require(type(value["state"]) is str and value["state"] in {"correct", "missing", "wrong"}, Code.DENIED)
        # Strict private file reads happen again inside the bounded child.
        key = config_bytes(source.vault_key_file)
        api_key = config_bytes(source.api_key_file).decode("ascii")
        vault = CreateReceiptVault(source.vault_directory, key)
        receipt = vault.read(source.create)
        require(receipt.sandbox_id == binding.sandbox_id and receipt.domain == source.domain, Code.DENIED)
        require(receipt.envd_state in {"null", "absent"}, Code.DENIED)
        replacement = secrets.token_urlsafe(32) if value["state"] == "wrong" else None
        return build_request(
            endpoint=value["endpoint"],
            host=f"49983-{receipt.sandbox_id}.{source.domain}",
            proxy_port=source.proxy_port,
            credentials=Credentials(api_key, receipt.traffic_token, None),
            layer="traffic",
            state=value["state"],
            replacement=replacement,
            traffic_alias=value["traffic_alias"],
        )
    except Exception:
        raise ContractError(Code.DENIED) from None


def observe_receipted_isolated(
    path: Path, *, binding: Binding, source: ReceiptProbeSource, verified_deadline: float
) -> dict:
    """Trusted parent only; pins and private references cross stdin, not secrets.

    10s child budget plus up to 1s group cleanup; process creation is not a hard
    realtime bound. A timeout never authorizes a retry or proves remote stopping.
    """
    require(type(verified_deadline) in {int, float} and math.isfinite(verified_deadline))
    require(verified_deadline - time.monotonic() >= 11, Code.DENIED)
    require(path.is_absolute() and path.resolve() == path, Code.DENIED)
    binding.validate()
    raw = run_worker(
        [sys.executable, "-I", "-m", "agentseek_execution.m3_receipt_probe"],
        canonical({
            "schema": 1,
            "path": str(path),
            "binding": asdict(binding),
            "source": source.wire(),
            "deadline": verified_deadline,
        }).encode(),
        budget=10,
        environment={},
    )
    result = _decode(raw)
    _validate_result(result)
    return result


def perform(value: dict) -> dict:
    require(type(value) is dict and set(value) == {"schema", "path", "binding", "source", "deadline"})
    require(type(value["schema"]) is int and value["schema"] == 1 and type(value["path"]) is str)
    require(type(value["binding"]) is dict)
    deadline = value["deadline"]
    require(type(deadline) in {int, float} and math.isfinite(deadline), Code.DENIED)
    require(deadline - time.monotonic() >= 10, Code.DENIED)
    request = prepare_receipted(
        Path(value["path"]), binding=Binding(**value["binding"]), source=ReceiptProbeSource.from_wire(value["source"])
    )
    # Disk and AEAD work consume the same child budget; recheck before sending.
    remaining = deadline - time.monotonic()
    require(remaining >= 10, Code.DENIED)
    result = asdict(observe(request, remaining_seconds=remaining))
    _validate_result(result)
    return result


def main() -> None:
    try:
        result = perform(_decode(sys.stdin.buffer.read(65537)))
        sys.stdout.write(canonical(result))
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
