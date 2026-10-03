import copy
import json
from dataclasses import asdict, replace

import pytest
from agentseek_execution import m3_template_evidence as module
from agentseek_execution.models import ContractError


@pytest.fixture
def template():
    pins = module.TemplatePins("tpl", "192.10.50.172", "rfs-pinned", "b" * 64)
    value = {
        "templateID": "tpl",
        "status": "READY",
        "version": "v2",
        "replicas": [
            {
                "node_id": pins.node_ip,
                "node_ip": pins.node_ip,
                "phase": "READY",
                "status": "READY",
                "artifact_id": pins.artifact_id,
            }
        ],
        "createRequest": {
            "annotations": {module.ARTIFACT_ID: pins.artifact_id},
            "containers": [
                {
                    "image": {
                        "annotations": {
                            module.ARTIFACT_ID: pins.artifact_id,
                            module.ARTIFACT_SHA: pins.artifact_sha256,
                            "cube.master.rootfs.artifact.token": "synthetic-private-token",
                            "cube.master.rootfs.artifact.url": "https://unused.invalid/?secret=synthetic",
                        }
                    }
                }
            ],
        },
    }
    return value, pins


def test_projection_whitelist_and_position_independence(template):
    value, pins = template
    value["replicas"].insert(0, {"node_ip": "192.10.50.171"})
    value["createRequest"]["containers"].insert(0, {"image": {"annotations": {}}})
    before = copy.deepcopy(value)
    result = module.project_bytes(json.dumps(value).encode(), pins)
    assert result.replica_index == result.container_index == 1
    assert result.replica_count == result.container_count == 2
    assert not result.artifact_bytes_verified and not result.aggregate_ready
    assert "synthetic" not in json.dumps(asdict(result)) and value == before


@pytest.mark.parametrize(
    "path,value",
    [
        (("templateID",), "foreign"),
        (("status",), "BUILDING"),
        (("version",), "v3"),
        (("replicas",), []),
        (("replicas",), {}),
        (("replicas", 0, "node_ip"), "192.10.50.171"),
        (("replicas", 0, "node_id"), "foreign"),
        (("replicas", 0, "phase"), "BUILDING"),
        (("replicas", 0, "status"), "BUILDING"),
        (("replicas", 0, "artifact_id"), "other"),
        (("createRequest", "annotations", module.ARTIFACT_ID), "other"),
        (("createRequest", "containers"), []),
        (("createRequest", "containers"), None),
        (("createRequest", "containers", 0, "image", "annotations", module.ARTIFACT_ID), "other"),
        (("createRequest", "containers", 0, "image", "annotations", module.ARTIFACT_SHA), "c" * 64),
        (("createRequest", "containers", 0, "image", "annotations"), None),
    ],
)
def test_inconsistent_or_missing_fields_deny_without_echo(template, path, value):
    data, pins = template
    current = data
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value
    with pytest.raises(ContractError) as error:
        module.project_bytes(json.dumps(data).encode(), pins)
    assert "synthetic" not in str(error.value)


@pytest.mark.parametrize("kind", ["replica", "replica_not_ready", "container", "container_wrong_sha"])
def test_ambiguous_identity_cannot_be_hidden_by_status_or_digest(template, kind):
    data, pins = template
    if kind.startswith("replica"):
        duplicate = copy.deepcopy(data["replicas"][0])
        if kind.endswith("not_ready"):
            duplicate["phase"] = "BUILDING"
        data["replicas"].append(duplicate)
    else:
        duplicate = copy.deepcopy(data["createRequest"]["containers"][0])
        if kind.endswith("wrong_sha"):
            duplicate["image"]["annotations"][module.ARTIFACT_SHA] = "c" * 64
        data["createRequest"]["containers"].append(duplicate)
    with pytest.raises(ContractError):
        module.project(data, pins)


@pytest.mark.parametrize(
    "raw", [b'{"templateID":"tpl","templateID":"other"}', b"x" * 65537, b"[]", b"synthetic-secret"]
)
def test_raw_input_limits_and_duplicate_keys(template, raw):
    with pytest.raises(ContractError) as error:
        module.project_bytes(raw, template[1])
    assert "synthetic" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("template_id", "../bad"),
        ("node_ip", "not-an-ip"),
        ("artifact_id", "bad\nheader"),
        ("artifact_sha256", "B" * 64),
    ],
)
def test_invalid_installation_pins(template, field, value):
    with pytest.raises(ContractError):
        module.project(template[0], replace(template[1], **{field: value}))
