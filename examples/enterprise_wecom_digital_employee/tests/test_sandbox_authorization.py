from dataclasses import replace
from types import SimpleNamespace

import pytest

from enterprise_wecom_digital_employee.sandbox_authorization import (
    SandboxGrant, SandboxRequestResolver, instruction_digest,
)


def make(grant=None, allowed=True):
    grant = grant or SandboxGrant("request", "tenant", "user", "session", "file", instruction_digest("sum csv"), 200)
    resolver = SandboxRequestResolver(grant_for=lambda runtime: grant,
        file_allowed=lambda scope, ref: allowed, clock=lambda: 100)
    runtime = SimpleNamespace(context={"enterprise": dict(tenant_key="tenant", user_key="user", session_key="session")})
    return resolver, runtime, grant


def test_server_request_id_and_scoped_owner():
    resolver, runtime, _ = make()
    result = resolver(runtime, "file", "sum csv")
    assert result.request_id == "request" and result.owner_id != "user"


@pytest.mark.parametrize("change", [dict(tenant_key="other"), dict(user_key="other"),
    dict(session_key="other"), dict(expires_epoch=100), dict(expires_epoch=float("nan")),
    dict(expires_epoch=float("inf")), dict(input_ref="other"), dict(instruction_sha256="other")])
def test_mismatched_grant_denied(change):
    _, _, grant = make()
    resolver, runtime, _ = make(replace(grant, **change))
    with pytest.raises(ValueError):
        resolver(runtime, "file", "sum csv")


def test_revoked_file_denied():
    resolver, runtime, _ = make(allowed=False)
    with pytest.raises(ValueError):
        resolver(runtime, "file", "sum csv")


def test_model_state_cannot_supply_identity():
    resolver, runtime, _ = make()
    runtime.state = runtime.context
    runtime.context = None
    with pytest.raises(ValueError):
        resolver(runtime, "file", "sum csv")
