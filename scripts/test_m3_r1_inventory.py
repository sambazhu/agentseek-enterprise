"""Inventory isolation and staging regression tests (no candidate imports)."""
# ruff: noqa: S101 -- pytest assertions are the test contract

import ast
import json
from pathlib import Path

import pytest
from m3_r1_inventory import dependencies, inventory, stage, stage_package

ROOT = Path(__file__).resolve().parents[1]


def test_real_closure_includes_fixtures_and_excludes_file_pipeline():
    result = inventory(ROOT)
    paths = {item["path"] for item in result["files"]}
    assert "contrib/agentseek-execution/src/agentseek_execution/file_io.py" in paths
    assert "contrib/agentseek-execution/tests/test_m3_create_process.py" in paths
    assert "examples/enterprise_wecom_digital_employee/sandbox_poc/node_supervisor.py" in paths
    assert not any(Path(path).name in {"ledger.py", "s3_content.py", "guest_pipeline.py"} for path in paths)
    assert result == inventory(ROOT)


def test_stage_is_hash_verified_and_never_overwrites(tmp_path):
    result = inventory(ROOT)
    destination = tmp_path / "isolated"
    stage(ROOT, destination, result)
    assert json.loads((destination / "R1_INVENTORY.json").read_text()) == result
    for item in result["files"]:
        assert (destination / item["path"]).read_bytes() == (ROOT / item["path"]).read_bytes()
    with pytest.raises(FileExistsError):
        stage(ROOT, destination, result)


def test_changed_source_stops_staging(tmp_path):
    result = inventory(ROOT)
    result["files"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source changed"):
        stage(ROOT, tmp_path / "changed", result)


def test_missing_explicit_local_import_is_not_silently_omitted():
    tree = ast.parse("from .missing import value")
    with pytest.raises(ValueError, match="missing local module"):
        list(dependencies(tree, "agentseek_execution", {}, set()))


def test_package_contains_only_runtime_closure(tmp_path):
    result = inventory(ROOT)
    destination = tmp_path / "package"
    stage_package(ROOT, destination, result)
    files = list((destination / "src/agentseek_execution").glob("*.py"))
    assert len(files) == len(result["runtime_modules"])
    assert not (destination / "src/agentseek_execution/m3_create_watchdog.py").exists()
    assert not (destination / "src/agentseek_execution/ledger.py").exists()
    assert (destination / "pyproject.toml").read_bytes() == (ROOT / "scripts/m3_r1_package.toml").read_bytes()
    with pytest.raises(FileExistsError):
        stage_package(ROOT, destination, result)
