from dataclasses import asdict
import hashlib
import json

import pytest

from agentseek_execution.business_service import load_config
from test_business_broker import broker_case


def setup_config(s):
    def write(name, data):
        path = s.path / name
        path.write_text(json.dumps(data)); path.chmod(0o600)
        return str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest()
    permits, permits_sha = write("permits.json", dict(schema=1, approved=True, permits=[asdict(s.permit)]))
    life, life_sha = write("life.json", {"synthetic": True})
    plan, plan_sha = write("plan.json", {"synthetic": True})
    cert, _ = write("cert", {"synthetic": True})
    key, _ = write("key", {"synthetic": True})
    config = dict(schema=1, approved=True, bind_host="127.0.0.1", bind_port=18443,
        certificate_file=cert, private_key_file=key, permits_file=permits, permits_sha256=permits_sha,
        store_directory=str(s.path), workspace_directory=str(s.path / "workspace"),
        plans=[dict(owner_id=s.request.owner_id, request_id=s.request.request_id,
                    lifecycle_file=life, lifecycle_sha256=life_sha, business_file=plan, business_sha256=plan_sha)])
    return config, write


def test_static_service_config_has_no_startup_side_effects(broker_case):
    config, write = setup_config(broker_case)
    path, digest = write("server.json", config)
    loaded, permits, plans = load_config(broker_case.path / "server.json", digest)
    assert loaded == config and permits == (broker_case.permit,)
    assert set(plans) == {(broker_case.request.owner_id, broker_case.request.request_id)}
    assert not broker_case.provider.events


@pytest.mark.parametrize("key,value", [("approved", False), ("bind_host", "0.0.0.0"),
                                      ("bind_port", True), ("plans", [])])
def test_unapproved_or_ambiguous_service_config_rejected(broker_case, key, value):
    config, write = setup_config(broker_case)
    config[key] = value
    path, digest = write("server.json", config)
    with pytest.raises(ValueError): load_config(broker_case.path / "server.json", digest)
    assert not broker_case.provider.events
