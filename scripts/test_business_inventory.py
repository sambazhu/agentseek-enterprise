from pathlib import Path

from business_inventory import business_inventory
from m3_r1_inventory import inventory, stage_package

ROOT = Path(__file__).resolve().parents[1]


def test_business_package_is_distinct_and_excludes_gateway_and_unrelated_work(tmp_path):
    manifest = business_inventory(ROOT)
    assert manifest["entry_modules"] == ["business_service", "business_cube_session"]
    names = {Path(f["path"]).name for f in manifest["files"]}
    assert {"business_workspace.py", "business_http.py", "test_business_http.py"} <= names
    assert not names & {"ledger.py", "s3_content.py", "guest_pipeline.py", "sandbox_remote.py"}
    stage_package(ROOT, tmp_path / "package", manifest, template=Path("scripts/business_package.toml"))
    assert 'name = "agentseek-execution-business"' in (tmp_path / "package/pyproject.toml").read_text()
    assert inventory(ROOT)["entry_modules"][-1] == "m3_continuous"
