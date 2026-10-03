"""One authenticated business request, all lifecycle stages local to the node.

No endpoint accepts commands, paths, credentials, arbitrary recipients or grants.
Permits and providers are supplied by trusted node composition, never a client.
"""

import base64
from dataclasses import asdict, dataclass
import hashlib
import hmac
import math
import time

from .business_execution import BusinessRequest, run_business
from .csv_business import CsvBusinessBackend, MAX_CSV_BYTES

WIRE_LIMIT = 120000


@dataclass(frozen=True)
class BusinessPermit:
    request: BusinessRequest
    input_sha256: str
    principal_sha256: str
    expires_epoch: float

    def __post_init__(self):
        for digest in (self.input_sha256, self.principal_sha256):
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("invalid digest")
        if type(self.expires_epoch) not in (int, float) or not math.isfinite(self.expires_epoch):
            raise ValueError("invalid expiry")


def load_approved_permits(path, digest):
    """Read a private operator-approved catalog; never accepts a network grant."""
    from .m3_create_process import _path, _pinned
    from .m3_probe_process import _decode
    value = _decode(_pinned(_path(str(path)), digest))
    if (set(value) != {"schema", "approved", "permits"} or type(value["schema"]) is not int
            or value["schema"] != 1 or value["approved"] is not True or type(value["permits"]) is not list):
        raise ValueError("unapproved catalog")
    return tuple(BusinessPermit(**dict(p, request=BusinessRequest(**p["request"]))) for p in value["permits"])


class BusinessBroker:
    def __init__(self, *, permits, store, workspace, provider_for, clock=time.time):
        permits = tuple(permits)
        self.permits = {(p.request.owner_id, p.request.request_id): p for p in permits}
        if len(self.permits) != len(permits):
            raise ValueError("duplicate permit")
        self.store, self.provider_for, self.clock = store, provider_for, clock
        self.workspace = workspace

    def dispatch(self, authorization, payload):
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise ValueError("authentication required")
        token = authorization[7:]
        if not 32 <= len(token) <= 256:
            raise ValueError("invalid credential")
        if type(payload) is not dict or set(payload) != {"operation", "request", "input"}:
            raise ValueError("invalid envelope")
        request = BusinessRequest(**payload["request"])
        permit = self.permits.get((request.owner_id, request.request_id))
        principal = hashlib.sha256(token.encode()).hexdigest()
        if permit is None or request != permit.request or not hmac.compare_digest(principal, permit.principal_sha256):
            raise ValueError("request not permitted")
        operation = payload["operation"]
        if operation not in {"execute", "result"}:
            raise ValueError("unsupported operation")
        if operation == "result":
            if payload["input"] != "":
                raise ValueError("result is read only")
        else:
            encoded = payload["input"]
            if type(encoded) is not str or len(encoded) > 44000:
                raise ValueError("invalid input")
            data = base64.b64decode(encoded, validate=True)
            if not 0 < len(data) <= MAX_CSV_BYTES or hashlib.sha256(data).hexdigest() != permit.input_sha256:
                raise ValueError("input not approved")
        # A repeated request reads its durable result, including uncertain state.
        # It never creates a replacement VM or reruns incomplete work.
        outcome = self.store.snapshot(request)
        if outcome is None and operation == "execute":
            if not self.clock() < permit.expires_epoch:
                raise ValueError("permit expired")
            backend = CsvBusinessBackend(request=request, store=self.store,
                provider=self.provider_for(permit), authorize=lambda r: r == permit.request and self.clock() < permit.expires_epoch,
                load_input=lambda r: data, workspace=self.workspace)
            outcome = run_business(request, backend)
        if outcome is None:
            return {"state": "not_found"}
        result = dict(outcome=asdict(outcome), request_id=request.request_id, owner_id=request.owner_id,
                      data=None, sha256=None, workspace=None)
        if outcome.artifact_ref:
            content = self.store.read(request.owner_id, outcome.artifact_ref)
            result.update(data=base64.b64encode(content).decode(), sha256=hashlib.sha256(content).hexdigest())
        if outcome.state == "succeeded":
            result["workspace"] = self.workspace.reference(request, outcome.attempt, outcome.artifact_ref)
        return result
