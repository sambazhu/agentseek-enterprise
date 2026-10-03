"""Strict T1 API projection; not artifact-byte or active-build verification.

Paths follow the September 9 response indexed by Linux CC on September 10.
No raw response, annotation map, URL or token is returned. Installation supplies
expected identifiers/digest; an API declaration is not a downloaded-file hash.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from .m3_probe_process import _decode
from .models import Code, ContractError, require

ARTIFACT_ID = "cube.master.rootfs.artifact.id"
ARTIFACT_SHA = "cube.master.rootfs.artifact.sha256"


@dataclass(frozen=True)
class TemplatePins:
    template_id: str
    node_ip: str
    artifact_id: str
    artifact_sha256: str

    def validate(self) -> None:
        for value in (self.template_id, self.artifact_id):
            require(type(value) is str and re.fullmatch(r"[A-Za-z0-9-]{1,256}", value) is not None, Code.DENIED)
        require(type(self.node_ip) is str and str(ipaddress.ip_address(self.node_ip)) == self.node_ip, Code.DENIED)
        require(
            type(self.artifact_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256) is not None,
            Code.DENIED,
        )


@dataclass(frozen=True)
class TemplateSnapshot:
    template_id: str
    node_ip: str
    artifact_id: str
    artifact_sha256: str
    replica_index: int
    container_index: int
    replica_count: int
    container_count: int
    artifact_bytes_verified: bool = False
    aggregate_ready: bool = False


def project_bytes(raw: bytes, pins: TemplatePins) -> TemplateSnapshot:
    try:
        require(type(raw) is bytes)
        return project(_decode(raw), pins)
    except Exception:
        raise ContractError(Code.DENIED) from None


def project(value: dict, pins: TemplatePins) -> TemplateSnapshot:
    """Select by node then artifact identity, never by an assumed array position."""
    try:
        pins.validate()
        require(
            type(value) is dict
            and value.get("templateID") == pins.template_id
            and value.get("status") == "READY"
            and value.get("version") == "v2",
            Code.DENIED,
        )
        replicas = value["replicas"]
        require(type(replicas) is list and 0 < len(replicas) <= 256, Code.DENIED)
        require(all(type(item) is dict for item in replicas), Code.DENIED)
        matches = [(i, item) for i, item in enumerate(replicas) if item.get("node_ip") == pins.node_ip]
        require(len(matches) == 1, Code.DENIED)
        replica_index, replica = matches[0]
        require(
            replica.get("node_id") == pins.node_ip
            and replica.get("phase") == "READY"
            and replica.get("status") == "READY"
            and replica.get("artifact_id") == pins.artifact_id,
            Code.DENIED,
        )
        request = value["createRequest"]
        require(type(request) is dict and type(request.get("annotations")) is dict, Code.DENIED)
        require(request["annotations"].get(ARTIFACT_ID) == pins.artifact_id, Code.DENIED)
        containers = request["containers"]
        require(type(containers) is list and 0 < len(containers) <= 256, Code.DENIED)
        candidates = []
        for i, container in enumerate(containers):
            require(type(container) is dict and type(container.get("image")) is dict, Code.DENIED)
            annotations = container["image"].get("annotations")
            require(type(annotations) is dict, Code.DENIED)
            if annotations.get(ARTIFACT_ID) == pins.artifact_id:
                candidates.append((i, annotations))
        require(len(candidates) == 1, Code.DENIED)
        container_index, annotations = candidates[0]
        require(annotations.get(ARTIFACT_SHA) == pins.artifact_sha256, Code.DENIED)
        return TemplateSnapshot(
            pins.template_id,
            pins.node_ip,
            pins.artifact_id,
            pins.artifact_sha256,
            replica_index,
            container_index,
            len(replicas),
            len(containers),
        )
    except Exception:
        raise ContractError(Code.DENIED) from None
