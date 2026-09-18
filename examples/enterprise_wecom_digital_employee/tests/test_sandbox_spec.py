"""Opt-in composition only; never opens a connection or starts a gateway."""

import hashlib
import json

import pytest

from enterprise_wecom_digital_employee import sandbox_spec


def test_missing_opt_in_configuration_refused(monkeypatch):
    monkeypatch.delenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG", raising=False)
    with pytest.raises(KeyError):
        sandbox_spec.build_spec()


@pytest.mark.parametrize("approved", [False, "true", 1])
def test_unapproved_gateway_configuration_refused(tmp_path, monkeypatch, approved):
    config = dict(schema=1, approved=approved, endpoint="https://unused.invalid:8443",
                  ca_file="/unused/ca", broker_token_file="/unused/token",
                  grants_file="/unused/grants", grants_sha256="0" * 64,
                  mirror_directory="/unused/mirror")
    path = tmp_path / "config.json"
    data = json.dumps(config).encode()
    path.write_bytes(data)
    path.chmod(0o600)
    monkeypatch.setenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG", str(path))
    monkeypatch.setenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG_SHA256", hashlib.sha256(data).hexdigest())
    with pytest.raises(ValueError, match="unapproved"):
        sandbox_spec.build_spec()
