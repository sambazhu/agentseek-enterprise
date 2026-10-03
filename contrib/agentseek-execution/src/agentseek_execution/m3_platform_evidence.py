"""R1 read-only CubeAPI snapshot and independently pinned approval reader.

No defaults for endpoint/key/CA/approval digest; no create, kill or operator CLI.
Trusted installation supplies pins, never request JSON. This is partial evidence,
not aggregate readiness: template artifact, admission deadline, supervisor unit,
network mode and donor-token lifecycle still need independent verification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import ssl
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .file_io import read_regular
from .m3_create_receipt import CreateBinding, CreateReceiptVault
from .m3_lifetime_evidence import platform_epoch
from .m3_probe_dispatch import Binding
from .m3_probe_process import _decode, _unique, config_bytes, prepare
from .m3_template_evidence import TemplatePins, TemplateSnapshot, project
from .models import Code, ContractError, require


@dataclass(frozen=True)
class ApprovalSnapshot:
    binding: Binding
    expires_epoch: float
    document_sha256: str


def read_approval(
    path: Path, *, pinned_digest: str, binding: Binding, now_epoch: float, owner_uid: int = 0
) -> ApprovalSnapshot:
    """Hash is an out-of-band trust anchor, not a signature or self-approval.

    Re-read every dispatch check. Expired, modified, missing or revoked material
    blocks; a replacement requires a newly approved installation pin.
    """
    binding.validate()
    require(type(pinned_digest) is str and re.fullmatch(r"[0-9a-f]{64}", pinned_digest) is not None)
    require(type(now_epoch) in {int, float} and math.isfinite(now_epoch))
    require(path.is_absolute() and path.resolve() == path, Code.DENIED)
    parent = path.parent.stat()
    info = path.lstat()
    require(parent.st_uid == owner_uid and stat.S_IMODE(parent.st_mode) == 0o700, Code.DENIED)
    require(
        info.st_uid == owner_uid and stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600, Code.DENIED
    )
    raw = read_regular(path.parent, path.name, max_bytes=65536)
    require(hmac.compare_digest(hashlib.sha256(raw).hexdigest(), pinned_digest), Code.DENIED)
    value = _decode(raw)
    require(set(value) == {"schema", "binding", "approved", "expires_epoch"})
    require(type(value["schema"]) is int and value["schema"] == 1 and value["approved"] is True, Code.DENIED)
    require(value["binding"] == asdict(binding), Code.DENIED)
    expiry = value["expires_epoch"]
    require(type(expiry) in {int, float} and math.isfinite(expiry) and expiry - now_epoch >= 11, Code.DENIED)
    return ApprovalSnapshot(binding, expiry, pinned_digest)


@dataclass(frozen=True)
class PlatformSnapshot:
    sandbox_id: str
    run_id: str
    create_token: str
    template_id: str
    observed_mono: float
    platform_count: int
    aggregate_ready: bool = False
    started_epoch: float | None = None
    end_epoch: float | None = None


class PlatformReader:
    """Explicit HTTPS CA/hostname validation; only GET list and exact detail.

    Synchronous I/O checks a 5s total and has 2s I/O timeouts. A trusted outer
    process budget remains mandatory (DNS/TLS blocking is not forcibly bounded
    by this class). No automatic retry, redirect, proxy environment or SDK auth.
    """

    def __init__(self, *, endpoint: str, api_key: str, ca_file: Path, domain: str, proxy_port: int):
        parsed = urlsplit(endpoint)
        require(
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.path in {"", "/"}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment,
            Code.DENIED,
        )
        require(type(api_key) is str and 0 < len(api_key) <= 4096 and all(33 <= ord(c) <= 126 for c in api_key))
        require(type(domain) is str and re.fullmatch(r"[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9]+)*", domain) is not None)
        # The approved same-host business path uses CubeProxy's HTTP port 80.
        # Keep existing R1 high ports; do not admit arbitrary privileged ports.
        require(type(proxy_port) is int and (proxy_port == 80 or 1024 <= proxy_port <= 65535))
        require(ca_file.is_absolute() and ca_file.resolve() == ca_file and ca_file.is_file(), Code.DENIED)
        self._tls = ssl.create_default_context(cafile=str(ca_file))
        require(self._tls.check_hostname and self._tls.verify_mode == ssl.CERT_REQUIRED, Code.DENIED)
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._domain = domain
        self._proxy_port = proxy_port

    def collect_template(self, pins: TemplatePins) -> tuple[TemplateSnapshot, float]:
        """Exact GET with the known SDK limit; no URL/artifact follow-up fetch."""
        import httpx

        try:
            pins.validate()
            deadline = time.monotonic() + 5
            with httpx.Client(
                transport=httpx.HTTPTransport(verify=self._tls, retries=0, trust_env=False),
                timeout=2,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                value = self._get(client, "/templates/" + pins.template_id + "?limit=1", deadline)
                result = project(value, pins)
                observed = time.monotonic()
                require(observed < deadline, Code.DENIED)
                return result, observed
        except Exception:
            raise ContractError(Code.DENIED) from None

    def collect_empty(self) -> float:
        """Require an empty list now; return observation time, not capacity policy."""
        import httpx

        try:
            deadline = time.monotonic() + 5
            with httpx.Client(
                transport=httpx.HTTPTransport(verify=self._tls, retries=0, trust_env=False),
                timeout=2,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                items = self._get(client, "/sandboxes", deadline)
                require(type(items) is list and not items, Code.DENIED)
                observed = time.monotonic()
                require(observed < deadline, Code.DENIED)
                return observed
        except Exception:
            raise ContractError(Code.DENIED) from None

    def collect_created(self, binding: CreateBinding, vault: CreateReceiptVault) -> PlatformSnapshot:
        """Identity/lifetime observation using a trusted worker's sealed receipt.

        Not dispatch authorization: no probe config or caller-supplied tokens.
        Existing collect() remains frozen until receipt-only probe wiring exists.
        Missing detail tokens are expected; they are never credential sources.
        """
        import httpx

        try:
            receipt = vault.read(binding)
            require(receipt.domain == self._domain, Code.DENIED)
            deadline = time.monotonic() + 5
            with httpx.Client(
                transport=httpx.HTTPTransport(verify=self._tls, retries=0, trust_env=False),
                timeout=2,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                items = self._get(client, "/sandboxes", deadline)
                require(
                    type(items) is list
                    and len(items) == 1
                    and type(items[0]) is dict
                    and items[0].get("sandboxID") == receipt.sandbox_id,
                    Code.DENIED,
                )
                info = self._get(client, "/sandboxes/" + receipt.sandbox_id, deadline)
                require(
                    type(info) is dict
                    and info.get("sandboxID") == receipt.sandbox_id
                    and info.get("templateID") == binding.template_id
                    and info.get("state") == "running",
                    Code.DENIED,
                )
                metadata = info.get("metadata")
                require(
                    type(metadata) is dict
                    and metadata.get("agentseek_run_id") == binding.run_id
                    and metadata.get("agentseek_create_token") == binding.create_token,
                    Code.DENIED,
                )
                if "domain" in info:
                    require(info["domain"] == receipt.domain, Code.DENIED)
                started = platform_epoch(info.get("startedAt"))
                end = platform_epoch(info["endAt"]) if info.get("endAt") is not None else None
                observed = time.monotonic()
                require(observed < deadline, Code.DENIED)
                return PlatformSnapshot(
                    receipt.sandbox_id,
                    binding.run_id,
                    binding.create_token,
                    binding.template_id,
                    observed,
                    1,
                    started_epoch=started,
                    end_epoch=end,
                )
        except Exception:
            raise ContractError(Code.DENIED) from None

    def collect(self, binding: Binding, config_path: Path) -> PlatformSnapshot:
        import httpx

        binding.validate()
        require(re.fullmatch(r"[A-Za-z0-9-]+", binding.sandbox_id) is not None, Code.DENIED)
        raw = config_bytes(config_path)
        require(hashlib.sha256(raw).hexdigest() == binding.config_sha256, Code.DENIED)
        config = _decode(raw)
        prepare(config_path, expected_digest=binding.config_sha256)
        # Donor termination/provenance is not implemented: never silently accept
        # a replacement token as cross-guest evidence.
        require(config["state"] != "cross_guest", Code.DENIED)
        deadline = time.monotonic() + 5
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(verify=self._tls, retries=0, trust_env=False),
                timeout=2,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                items = self._get(client, "/sandboxes", deadline)
                require(type(items) is list and len(items) == 1, Code.DENIED)
                require(type(items[0]) is dict and items[0].get("sandboxID") == binding.sandbox_id, Code.DENIED)
                info = self._get(client, "/sandboxes/" + binding.sandbox_id, deadline)
                self._bind(info, binding, config)
                started = platform_epoch(info.get("startedAt"))
                end = platform_epoch(info["endAt"]) if info.get("endAt") is not None else None
                observed = time.monotonic()
                require(observed < deadline, Code.DENIED)
                return PlatformSnapshot(
                    binding.sandbox_id,
                    binding.run_id,
                    binding.create_token,
                    binding.template_id,
                    observed,
                    1,
                    started_epoch=started,
                    end_epoch=end,
                )
        except Exception:
            raise ContractError(Code.DENIED) from None

    def _get(self, client, path: str, deadline: float):
        require(time.monotonic() < deadline, Code.DENIED)
        with client.stream(
            "GET",
            self._endpoint + path,
            headers={"X-API-Key": self._api_key, "Accept-Encoding": "identity"},
            follow_redirects=False,
        ) as response:
            require(response.status_code == 200, Code.DENIED)
            require(response.headers.get("Content-Encoding", "identity").lower() == "identity", Code.DENIED)
            require(
                response.headers.get("Content-Type", "").partition(";")[0].strip().lower() == "application/json",
                Code.DENIED,
            )
            body = bytearray()
            for chunk in response.iter_raw(chunk_size=4096):
                require(time.monotonic() < deadline and len(body) + len(chunk) <= 65536, Code.DENIED)
                body.extend(chunk)
            return json.loads(body, object_pairs_hook=_unique)

    def _bind(self, info: dict, binding: Binding, config: dict) -> None:
        require(
            type(info) is dict
            and info.get("sandboxID") == binding.sandbox_id
            and info.get("templateID") == binding.template_id
            and info.get("state") == "running",
            Code.DENIED,
        )
        metadata = info.get("metadata")
        require(
            type(metadata) is dict
            and metadata.get("agentseek_run_id") == binding.run_id
            and metadata.get("agentseek_create_token") == binding.create_token,
            Code.DENIED,
        )
        require(info.get("domain") == self._domain, Code.DENIED)
        require(
            config["host"] == f"49983-{binding.sandbox_id}.{self._domain}" and config["proxy_port"] == self._proxy_port,
            Code.DENIED,
        )
        credentials = config["credentials"]
        require(hmac.compare_digest(credentials["api_key"], self._api_key), Code.DENIED)
        for field, key in (("trafficAccessToken", "traffic"), ("envdAccessToken", "envd")):
            value = info.get(field)
            require(type(value) is str and bool(value) and hmac.compare_digest(value, credentials[key]), Code.DENIED)
