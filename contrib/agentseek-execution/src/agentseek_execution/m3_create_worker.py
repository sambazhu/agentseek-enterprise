"""Internal one-shot create transport, not an operator entry point.

Trusted installation supplies approval digest, candidate/boot identity, CA/key,
vault, shared installation fence and a fresh admission collector. No JSON import,
CLI, retry or cleanup. The fence blocks subsequent runs/tokens until a future
verified convergence protocol exists; even successful creation does not clear it.
The collector must verify W0, count=0, template, alarms and the M3 node supervisor;
its return value is a same-host monotonic hard deadline, not platform idle TTL.
The internal LiveCreateAdmission provides live schema-3 collection; trusted
installation/launch composition is still required and deployment is not ready. An outer
trusted process watchdog remains mandatory to bound DNS/TLS and blocking I/O.
The internal m3_create_watchdog adapter provides that boundary for a dedicated
single-thread Linux parent; it does not implement the admission collector.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .m3_create_fence import CreateFence
from .m3_create_receipt import CreateBinding, CreateReceiptVault
from .m3_platform_evidence import PlatformReader
from .m3_probe_process import _decode, config_bytes
from .models import Code, ContractError, require


@dataclass(frozen=True)
class CreatePlan:
    run_id: str
    create_token: str
    template_id: str
    boot_id: str
    candidate_sha256: str
    request_sha256: str
    domain: str
    restricted: bool
    endpoint: str

    def binding(self, approval_digest: str) -> CreateBinding:
        fields = asdict(self)
        fields.pop("endpoint")
        binding = CreateBinding(**fields, approval_sha256=approval_digest, intent_sha256="0" * 64)
        binding.validate()
        return binding


def read_create_approval(plan: CreatePlan, path: Path, *, pinned_digest: str) -> float:
    """Root private approval, independently pinned; returns a monotonic expiry."""
    plan.binding(pinned_digest)
    require(os.getuid() == 0, Code.DENIED)
    raw = config_bytes(path)
    require(hmac.compare_digest(hashlib.sha256(raw).hexdigest(), pinned_digest), Code.DENIED)
    value = _decode(raw)
    require(set(value) == {"schema", "plan", "approved", "max_creates", "w0_accepted", "expires_epoch"})
    require(
        type(value["schema"]) is int
        and value["schema"] == 1
        and value["approved"] is True
        and value["w0_accepted"] is True
        and type(value["max_creates"]) is int
        and value["max_creates"] == 1
        and value["plan"] == asdict(plan),
        Code.DENIED,
    )
    expiry = value["expires_epoch"]
    require(type(expiry) in {int, float} and math.isfinite(expiry), Code.DENIED)
    remaining = expiry - time.time()
    require(remaining >= 11, Code.DENIED)
    return time.monotonic() + remaining


class CreateWorker:
    def __init__(
        self,
        *,
        plan: CreatePlan,
        approval_path: Path,
        approval_digest: str,
        installed_candidate_sha256: str,
        current_boot_id: str,
        api_key: str,
        ca_file: Path,
        vault: CreateReceiptVault,
        fence: CreateFence,
        collect_admission: Callable[[], float],
        batch_quota=None,
        previous_create: CreateBinding | None = None,
        collect_closeout=None,
    ):
        self._binding = plan.binding(approval_digest)
        require(plan.candidate_sha256 == installed_candidate_sha256 and plan.boot_id == current_boot_id, Code.DENIED)
        require(callable(collect_admission), Code.DENIED)
        require(type(fence) is CreateFence, Code.DENIED)
        if batch_quota is not None:
            from .m3_batch_quota import BatchQuota

            require(type(batch_quota) is BatchQuota and plan in batch_quota.sequence, Code.DENIED)
            index = batch_quota.sequence.index(plan)
            require((index == 0) == (previous_create is None), Code.DENIED)
        else:
            require(previous_create is None, Code.DENIED)
        require((previous_create is None and collect_closeout is None)
                or (type(previous_create) is CreateBinding and callable(collect_closeout)), Code.DENIED)
        # Share the fixed endpoint / CA / hostname validation, not SDK defaults.
        self._reader = PlatformReader(
            endpoint=plan.endpoint, api_key=api_key, ca_file=ca_file, domain=plan.domain, proxy_port=13080
        )
        self._plan = plan
        self._approval_path = approval_path
        self._vault = vault
        self._fence = fence
        self._collect_admission = collect_admission
        self._batch_quota = batch_quota
        self._previous_create = previous_create
        self._collect_closeout = collect_closeout

    def _authorize(self) -> float:
        # Installed pin is out-of-band. This module cannot write approval.
        return read_create_approval(self._plan, self._approval_path, pinned_digest=self._binding.approval_sha256)

    def _gate(self) -> float:
        approval_deadline = self._authorize()
        deadline = self._collect_admission()
        require(type(deadline) in {int, float} and math.isfinite(deadline), Code.DENIED)
        # A slow collector must not hide approval revocation/expiry during I/O.
        deadline = min(deadline, approval_deadline, self._authorize())
        require(deadline - time.monotonic() >= 11, Code.DENIED)
        return deadline

    def create(self, request: bytes) -> CreateBinding:
        """Return a non-secret sealed reference, never raw response or token.

        Any failure from quota reservation onward is UNKNOWN and burns any claimed slot. The caller
        must reconcile, never re-send or reconstruct a lost response from detail.
        """
        import httpx

        try:
            require(type(request) is bytes and len(request) <= 65536)
            require(hashlib.sha256(request).hexdigest() == self._plan.request_sha256, Code.DENIED)
            value = _decode(request)
            require(value.get("templateID") == self._plan.template_id, Code.DENIED)
            require(
                value.get("metadata")
                == {"agentseek_run_id": self._plan.run_id, "agentseek_create_token": self._plan.create_token},
                Code.DENIED,
            )
            network = value.get("network")
            require(
                type(network) is dict and network.get("allowPublicTraffic") is (not self._plan.restricted), Code.DENIED
            )
            self._gate()
        except Exception:
            raise ContractError(Code.DENIED) from None
        try:
            # Trusted batch composition opts in; old single-create installations
            # retain their contract. Quota is burned even if fence/intent or the
            # second gate fails. It never substitutes for successor closeout.
            if self._batch_quota is not None:
                self._batch_quota.reserve(self._plan)
            if self._previous_create is None:
                self._fence.claim(self._binding)
            else:
                claimed = self._fence.claim_successor(
                    self._previous_create, self._vault, self._plan, self._approval_path,
                    approval_digest=self._binding.approval_sha256,
                    sequence=self._batch_quota.sequence, collect_closeout=self._collect_closeout,
                )
                require(claimed == self._binding, Code.DENIED)
            binding = self._vault.reserve_now(self._binding)
            deadline = min(self._gate(), time.monotonic() + 5)
            with httpx.Client(
                transport=httpx.HTTPTransport(verify=self._reader._tls, retries=0, trust_env=False),
                timeout=2,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                require(time.monotonic() < deadline, Code.DENIED)
                with client.stream(
                    "POST",
                    self._reader._endpoint + "/sandboxes",
                    content=request,
                    headers={
                        "X-API-Key": self._reader._api_key,
                        "Content-Type": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    follow_redirects=False,
                ) as response:
                    require(response.status_code == 201, Code.DENIED)
                    require(response.headers.get("Content-Encoding", "identity").lower() == "identity", Code.DENIED)
                    require(
                        response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
                        == "application/json",
                        Code.DENIED,
                    )
                    body = bytearray()
                    for chunk in response.iter_raw(chunk_size=4096):
                        require(time.monotonic() < deadline and len(body) + len(chunk) <= 65536, Code.DENIED)
                        body.extend(chunk)
                    require(time.monotonic() < deadline, Code.DENIED)
                    self._vault.seal_response(binding, bytes(body))
        except Exception:
            raise ContractError(Code.UNKNOWN) from None
        else:
            return binding
