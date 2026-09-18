"""Resolve model proposals against a server-issued, exact-action grant.

Grant issuance is deliberately separate: neither model state nor a tool's
``confirmed`` argument can mint permission. The gateway must supply grants.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import time


def instruction_digest(instruction: str) -> str:
    return hashlib.sha256(instruction.encode("utf-8")).hexdigest()


def runtime_scope(runtime):
    context = runtime.context
    identity = context.get("enterprise") if isinstance(context, Mapping) else getattr(context, "enterprise", None)
    if not isinstance(identity, Mapping):
        raise ValueError("missing authenticated identity")
    scope = tuple(identity.get(k) for k in ("tenant_key", "user_key", "session_key"))
    if not all(isinstance(v, str) and v for v in scope):
        raise ValueError("incomplete authenticated identity")
    return scope


def scoped_owner(scope):
    return hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class SandboxGrant:
    request_id: str
    tenant_key: str
    user_key: str
    session_key: str
    input_ref: str
    instruction_sha256: str
    expires_epoch: float


class SandboxRequestResolver:
    """Server callbacks verify stored file scope and retrieve approved grants."""

    def __init__(self, *, grant_for, file_allowed, clock=time.time):
        self.grant_for = grant_for
        self.file_allowed = file_allowed
        self.clock = clock

    def __call__(self, runtime, input_ref: str, instruction: str):
        from agentseek_execution.business_execution import BusinessRequest

        if not isinstance(input_ref, str) or not input_ref or len(input_ref) > 256:
            raise ValueError("invalid file reference")
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 16000:
            raise ValueError("invalid instruction")
        scope = runtime_scope(runtime)
        grant = self.grant_for(runtime)
        if not isinstance(grant, SandboxGrant) or not grant.request_id:
            raise ValueError("approval required")
        if (grant.tenant_key, grant.user_key, grant.session_key) != scope:
            raise ValueError("approval scope mismatch")
        now = self.clock()
        if (type(grant.expires_epoch) not in (int, float) or not math.isfinite(grant.expires_epoch)
                or type(now) not in (int, float) or not math.isfinite(now)
                or not now < grant.expires_epoch):
            raise ValueError("approval expired")
        if grant.input_ref != input_ref or grant.instruction_sha256 != instruction_digest(instruction):
            raise ValueError("exact action approval required")
        if self.file_allowed(scope, input_ref) is not True:
            raise ValueError("file access denied")
        owner = scoped_owner(scope)
        return BusinessRequest(grant.request_id, owner, input_ref, instruction)


class ApprovedGrantCatalog:
    """Pinned private gateway grants. No model/state flag can grant permission.

    The controlled pilot has exactly one live grant per authenticated scope.
    Ambiguous matches are rejected rather than guessed from model arguments.
    """

    def __init__(self, path, digest):
        self.path, self.digest = path, digest

    def __call__(self, runtime):
        from agentseek_execution.m3_create_process import _path, _pinned
        from agentseek_execution.m3_probe_process import _decode
        value = _decode(_pinned(_path(str(self.path)), self.digest))
        if (set(value) != {"schema", "approved", "grants"} or type(value["schema"]) is not int
                or value["schema"] != 1 or value["approved"] is not True or type(value["grants"]) is not list):
            raise ValueError("unapproved grant catalog")
        scope, now = runtime_scope(runtime), time.time()
        matches = []
        for item in value["grants"]:
            grant = SandboxGrant(**item)
            if (grant.tenant_key, grant.user_key, grant.session_key) == scope and now < grant.expires_epoch:
                matches.append(grant)
        if len(matches) != 1:
            raise ValueError("exactly one live grant required")
        return matches[0]
