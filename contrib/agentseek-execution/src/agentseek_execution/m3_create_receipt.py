"""Private R1 create-response vault. Trusted creating worker ONLY.

This is storage, not an HTTP provenance oracle. The internal creating worker must
reserve before sending, then seal only the response of that exact HTTPS call.
No import CLI, approval writer, create API, retries, or dispatch authority here.
AEAD does not protect against same-UID/root forgery or filesystem rollback.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .file_io import read_regular
from .m3_probe_process import _decode, config_bytes
from .models import Code, ContractError, canonical, require


@dataclass(frozen=True)
class CreateBinding:
    run_id: str
    create_token: str
    template_id: str
    boot_id: str
    approval_sha256: str
    candidate_sha256: str
    request_sha256: str
    intent_sha256: str
    domain: str
    restricted: bool

    def validate(self) -> None:
        require(type(self.restricted) is bool)
        for key, value in asdict(self).items():
            if key == "restricted":
                continue
            require(type(value) is str and 0 < len(value) <= 256 and all(33 <= ord(c) <= 126 for c in value))
            if key.endswith("sha256"):
                require(re.fullmatch(r"[0-9a-f]{64}", value) is not None)
        require(re.fullmatch(r"[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*", self.domain) is not None)


@dataclass(frozen=True, repr=False)
class SealedReceipt:
    sandbox_id: str
    template_id: str
    domain: str
    traffic_token: str | None
    response_sha256: str
    envd_state: str


class CreateReceiptVault:
    def __init__(self, directory: Path, key: bytes):
        require(type(key) is bytes and len(key) == 32, Code.DENIED)
        require(directory.is_absolute() and directory.resolve() == directory, Code.DENIED)
        self._directory = directory
        self._cipher = AESGCM(key)

    def _name(self, binding: CreateBinding, kind: str) -> str:
        # Changing approval/template cannot obtain another reservation for the
        # same run/create token. No reset, expiry or overwrite operation exists.
        index = hashlib.sha256(
            canonical(["m3-create-index-v1", binding.run_id, binding.create_token]).encode()
        ).hexdigest()
        return index + "." + kind

    def _aad(self, binding: CreateBinding, kind: str) -> bytes:
        return canonical({"purpose": "m3-create-v1", "kind": kind, "binding": asdict(binding)}).encode()

    def _open(self) -> int:
        fd = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, Code.DENIED)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _write(self, binding: CreateBinding, kind: str, value: dict) -> None:
        directory = self._open()
        try:
            # O_EXCL happens first: partial failure burns this slot permanently.
            fd = os.open(
                self._name(binding, kind), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            try:
                nonce = os.urandom(12)
                raw = nonce + self._cipher.encrypt(nonce, canonical(value).encode(), self._aad(binding, kind))
                offset = 0
                while offset < len(raw):
                    written = os.write(fd, raw[offset:])
                    require(written > 0, Code.UNKNOWN)
                    offset += written
                os.fsync(fd)
                os.fsync(directory)
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def _read(self, binding: CreateBinding, kind: str) -> dict:
        directory = self._open()
        try:
            path = self._directory / self._name(binding, kind)
            info = path.lstat()
            require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600, Code.DENIED)
            raw = read_regular(self._directory, path.name, max_bytes=131072)
            value = _decode(self._cipher.decrypt(raw[:12], raw[12:], self._aad(binding, kind)))
        except Exception:
            raise ContractError(Code.DENIED) from None
        else:
            return value
        finally:
            os.close(directory)

    def reserve(self, binding: CreateBinding, intent_path: Path) -> None:
        """Must succeed before sending create; duplicate/uncertain means no send.

        Does not generate intent or prove that its timestamps are contemporaneous.
        Actual approval and request digest checks belong to the creating worker.
        """
        binding.validate()
        raw = config_bytes(intent_path)
        require(hashlib.sha256(raw).hexdigest() == binding.intent_sha256, Code.DENIED)
        intent = _decode(raw)
        require(set(intent) == {"schema", "run_id", "create_token", "boot_id", "sent_epoch", "sent_mono"})
        require(type(intent["schema"]) is int and intent["schema"] == 1)
        require(all(intent[key] == getattr(binding, key) for key in ("run_id", "create_token", "boot_id")), Code.DENIED)
        for key in ("sent_epoch", "sent_mono"):
            require(type(intent[key]) in {int, float} and math.isfinite(intent[key]) and intent[key] >= 0)
        self._write(binding, "intent", {"schema": 1, "intent": intent})

    def reserve_now(self, binding: CreateBinding) -> CreateBinding:
        """Worker-only fresh intent; return its digest-bound identity before send.

        No recovery/retry operation: an existing slot, even unreadable, blocks.
        Boot identity and admission are checked by the trusted calling worker.
        """
        binding.validate()
        intent = {
            "schema": 1,
            "run_id": binding.run_id,
            "create_token": binding.create_token,
            "boot_id": binding.boot_id,
            "sent_epoch": time.time(),
            "sent_mono": time.monotonic(),
        }
        bound = replace(binding, intent_sha256=hashlib.sha256(canonical(intent).encode()).hexdigest())
        self._write(bound, "intent", {"schema": 1, "intent": intent})
        return bound

    def seal_response(self, binding: CreateBinding, response: bytes) -> None:
        """Only trusted caller's exact create response. No arbitrary import route."""
        binding.validate()
        self._read(binding, "intent")
        require(type(response) is bytes and len(response) <= 65536)
        receipt = self._project(binding, response)
        self._write(binding, "receipt", {"schema": 1, "receipt": asdict(receipt)})

    def read(self, binding: CreateBinding) -> SealedReceipt:
        binding.validate()
        self._read(binding, "intent")
        value = self._read(binding, "receipt")
        require(set(value) == {"schema", "receipt"} and type(value["schema"]) is int and value["schema"] == 1)
        receipt = SealedReceipt(**value["receipt"])
        require(receipt.template_id == binding.template_id and receipt.domain == binding.domain, Code.DENIED)
        return receipt

    def read_intent(self, binding: CreateBinding) -> dict:
        """Authenticated non-secret send timestamps, never a replacement intent."""
        binding.validate()
        value = self._read(binding, "intent")
        require(set(value) == {"schema", "intent"} and type(value["schema"]) is int and value["schema"] == 1)
        intent = value["intent"]
        require(
            type(intent) is dict
            and set(intent) == {"schema", "run_id", "create_token", "boot_id", "sent_epoch", "sent_mono"}
        )
        require(type(intent["schema"]) is int and intent["schema"] == 1)
        require(all(intent[key] == getattr(binding, key) for key in ("run_id", "create_token", "boot_id")), Code.DENIED)
        for key in ("sent_epoch", "sent_mono"):
            require(type(intent[key]) in {int, float} and math.isfinite(intent[key]) and intent[key] >= 0)
        # reserve_now uses canonical bytes. Legacy reserve(path) does not promise
        # canonical input and is deliberately not accepted by this new reader.
        require(hashlib.sha256(canonical(intent).encode()).hexdigest() == binding.intent_sha256, Code.DENIED)
        return intent

    @staticmethod
    def _project(binding: CreateBinding, response: bytes) -> SealedReceipt:
        value = _decode(response)
        sandbox_id = value.get("sandboxID")
        require(type(sandbox_id) is str and re.fullmatch(r"[A-Za-z0-9-]{1,256}", sandbox_id) is not None)
        require(value.get("templateID") == binding.template_id and value.get("domain") == binding.domain, Code.DENIED)
        require(value.get("envdAccessToken") is None, Code.DENIED)
        token = value.get("trafficAccessToken")
        if token == "":
            token = None
        require(
            token is None or (type(token) is str and 0 < len(token) <= 4096 and all(33 <= ord(c) <= 126 for c in token))
        )
        require(not binding.restricted or token is not None, Code.DENIED)
        return SealedReceipt(
            str(sandbox_id),
            binding.template_id,
            binding.domain,
            token,
            hashlib.sha256(response).hexdigest(),
            "null" if "envdAccessToken" in value else "absent",
        )
