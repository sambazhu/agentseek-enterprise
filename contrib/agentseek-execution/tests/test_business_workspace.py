from dataclasses import replace
import hashlib
import json

import pytest

from agentseek_execution.business_execution import BusinessRequest
from agentseek_execution.business_workspace import CsvWorkspace


def fixture(tmp_path):
    tmp_path.chmod(0o700)
    workspace = CsvWorkspace(tmp_path.resolve())
    request = BusinessRequest("request", "owner", "input", "sum")
    attempt = hashlib.sha256(json.dumps([request.owner_id, request.request_id]).encode()).hexdigest()
    data = b"group,total\nA,3\n"
    digest = hashlib.sha256(data).hexdigest()
    artifact = "artifact_" + hashlib.sha256((attempt + digest).encode()).hexdigest()
    return workspace, request, attempt, artifact, data


def test_immutable_workspace_survives_reopen_and_same_publish(tmp_path):
    workspace, request, attempt, artifact, data = fixture(tmp_path)
    result = workspace.publish(request, attempt, artifact, data)
    assert result["filename"] == "summary.csv" and result["sha256"] == hashlib.sha256(data).hexdigest()
    assert workspace.publish(request, attempt, artifact, data) == result
    assert CsvWorkspace(tmp_path.resolve()).reference(request, attempt, artifact) == result
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in tmp_path.rglob("*.csv"))
    with pytest.raises(ValueError): workspace.reference(replace(request, owner_id="other"), attempt, artifact)


def test_changed_workspace_bytes_never_advertised(tmp_path):
    workspace, request, attempt, artifact, data = fixture(tmp_path)
    workspace.publish(request, attempt, artifact, data)
    next(tmp_path.rglob("summary.csv")).write_bytes(b"group,total\nA,9\n")
    with pytest.raises(ValueError): workspace.reference(request, attempt, artifact)
    with pytest.raises(ValueError): workspace.publish(request, attempt, artifact, data)


def test_workspace_refuses_symlink_root(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError): CsvWorkspace(link.absolute())
