"""Read-only observation of a fenced create, not UNKNOWN resolution.

An empty list cannot exclude a queued late create. A terminal API state cannot
prove node termination. Neither releases the fence or authorizes another batch.
Only the original trusted plan binding is accepted, never an operator-selected
sandbox ID or a reconstructed create receipt. No token import, mutation or retry.
Run inside a trusted outer process watchdog; synchronous DNS/TLS is not bounded
by the cooperative five-second checks in this function alone.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .m3_create_fence import CreateFence
from .m3_create_receipt import CreateBinding
from .m3_platform_evidence import PlatformReader
from .models import Code, ContractError, require


@dataclass(frozen=True)
class ReconciliationObservation:
    state: str
    sandbox_id: str | None
    observed_mono: float
    fence_release_allowed: bool = False
    node_termination_proven: bool = False
    aggregate_ready: bool = False


def observe_pending(
    reader: PlatformReader, fence: CreateFence, binding: CreateBinding
) -> ReconciliationObservation:
    """GET full list then at most one exact detail; all ambiguity stays fenced.

    A multi-item list violates the single-guest contract: do not pick a target.
    Exact identity requires ID, template, run and create token to agree in detail.
    Detail credentials and raw metadata are never returned or stored.
    """
    import httpx

    try:
        require(type(reader) is PlatformReader and type(fence) is CreateFence, Code.DENIED)
        deadline = time.monotonic() + 5
        fence.verify_pending(binding)
        require(binding.domain == reader._domain, Code.DENIED)
        with httpx.Client(
            transport=httpx.HTTPTransport(verify=reader._tls, retries=0, trust_env=False),
            timeout=2,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            items = reader._get(client, "/sandboxes", deadline)
            require(type(items) is list and len(items) <= 256, Code.DENIED)
            ids = []
            for item in items:
                require(type(item) is dict, Code.DENIED)
                sid = item.get("sandboxID")
                require(type(sid) is str and re.fullmatch(r"[A-Za-z0-9-]{1,256}", sid) is not None, Code.DENIED)
                require(sid not in ids, Code.DENIED)
                ids.append(sid)
            state, target = "empty_unresolved", None
            if len(ids) > 1:
                state = "multiple_unresolved"
            elif ids:
                info = reader._get(client, "/sandboxes/" + ids[0], deadline)
                require(type(info) is dict and info.get("sandboxID") == ids[0], Code.DENIED)
                metadata = info.get("metadata")
                exact = (
                    info.get("templateID") == binding.template_id
                    and type(metadata) is dict
                    and metadata.get("agentseek_run_id") == binding.run_id
                    and metadata.get("agentseek_create_token") == binding.create_token
                    and info.get("domain", binding.domain) == binding.domain
                )
                state = "identity_conflict"
                if exact:
                    target = ids[0]
                    state = "exact_unresolved"
                    if info.get("state") in ("terminated", "removed"):
                        state = "exact_terminal_observed"
                    elif info.get("state") == "running":
                        state = "exact_running_observed"
        fence.verify_pending(binding)
        observed = time.monotonic()
        require(observed < deadline, Code.DENIED)
        return ReconciliationObservation(state, target, observed)
    except Exception:
        raise ContractError(Code.DENIED) from None
