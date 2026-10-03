import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import business_materials as bm
import business_preflight as pf
from business_materials_example import example


def save(path, value):
    path.write_bytes(bm.encode(value))
    path.chmod(0o600)
    return bm.sha(path.read_bytes())


@pytest.fixture
def case(tmp_path):
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    inp, digest = example(tmp_path / "synthetic")
    bm.run(inp, digest)
    spec = json.loads(inp.read_bytes())
    dirs = {k: Path(v) for k, v in spec["directories"].items()}
    dbpath = dirs["store"] / "business.sqlite"
    with sqlite3.connect(dbpath) as db:
        db.execute("CREATE TABLE attempts(id TEXT PRIMARY KEY,owner TEXT,request TEXT,state TEXT,artifact TEXT)")
    dbpath.chmod(0o600)
    manifest = dirs["supervisor"] / "manifest.json"
    save(manifest, dict(run_id="synthetic-business", template_id="synthetic-template", sandboxes={}))
    value = dict(schema=1, role="node", owner_id="synthetic-owner", request_id="c" * 64,
                 store_file=str(dbpath), materials_input=str(inp), materials_sha256=digest,
                 manifest_file=str(manifest))
    path = tmp_path / "preflight.json"
    return path, value, dirs, save(path, value)


def snapshot(root):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
            for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("role", ["node", "gateway"])
def test_readonly_success_and_cli(case, role):
    path, value, dirs, _ = case
    if role == "gateway":
        for key in ("materials_input", "materials_sha256", "manifest_file"): value.pop(key)
        value["role"] = role
    pin = save(path, value)
    before = snapshot(path.parent)
    result = pf.run(path, pin)
    assert result["status"] == "LOCAL_PREPARATION_PASS"
    assert result["execution_authorized"] is False and result["live_gate_required"] is True
    proc = subprocess.run([sys.executable, "-I", pf.__file__, "--input", str(path), "--sha256", pin], capture_output=True, text=True)
    assert proc.returncode == 0 and not proc.stderr
    assert snapshot(path.parent) == before


@pytest.mark.parametrize("problem", ["tracking", "lifecycle", "quota", "fence", "manifest_mode",
    "manifest_nonempty", "manifest_run", "asset_missing", "asset_changed", "db_missing", "db_mode", "wal", "pin", "request"])
def test_blocks_without_repair(case, problem):
    path, value, dirs, pin = case
    if problem in {"tracking", "lifecycle", "quota", "fence"}: (dirs[problem] / "old").write_text("preserve")
    elif problem == "manifest_mode": Path(value["manifest_file"]).chmod(0o644)
    elif problem.startswith("manifest_"):
        p = Path(value["manifest_file"]); data = json.loads(p.read_bytes())
        data["sandboxes" if problem == "manifest_nonempty" else "run_id"] = {"old": 1} if problem == "manifest_nonempty" else "other"
        save(p, data)
    elif problem in {"asset_missing", "asset_changed"}:
        p = path.parent / "synthetic/output/api_key.asset"
        if problem == "asset_missing": p.unlink()
        else: p.write_bytes(b"secret-not-to-log")
    elif problem == "db_missing": Path(value["store_file"]).unlink()
    elif problem == "db_mode": Path(value["store_file"]).chmod(0o644)
    elif problem == "wal": Path(value["store_file"] + "-wal").write_bytes(b"")
    elif problem == "pin": pin = "0" * 64
    elif problem == "request": value["request_id"] = "d" * 64; pin = save(path, value)
    before = snapshot(path.parent)
    with pytest.raises((bm.MaterialError, OSError)): pf.run(path, pin)
    proc = subprocess.run([sys.executable, "-I", pf.__file__, "--input", str(path), "--sha256", pin], capture_output=True, text=True)
    assert proc.returncode == 2 and not proc.stderr and "secret-not-to-log" not in proc.stdout
    assert str(path) not in proc.stdout and snapshot(path.parent) == before


@pytest.mark.parametrize("state,request_id,blocked", [("failed", "c" * 64, True), ("succeeded", "c" * 64, True),
    ("reconciling", "d" * 64, True), ("reserved", "d" * 64, True), ("unknown", "d" * 64, True), ("failed", "d" * 64, False)])
def test_history_and_unresolved(case, state, request_id, blocked):
    path, value, _, pin = case
    with sqlite3.connect(value["store_file"]) as db:
        db.execute("INSERT INTO attempts VALUES ('old','synthetic-owner',?,?,NULL)",
                   (json.dumps(dict(owner_id="synthetic-owner", request_id=request_id)), state))
    before = snapshot(path.parent)
    if blocked:
        with pytest.raises(bm.MaterialError): pf.run(path, pin)
    else: assert pf.run(path, pin)["terminal_rows"] == 1
    assert snapshot(path.parent) == before


def test_runtime_reader_rejects_0644_manifest(case):
    from agentseek_execution.m3_supervisor_snapshot import SupervisorReader
    from agentseek_execution.models import ContractError
    _, value, dirs, _ = case
    reader = object.__new__(SupervisorReader)
    reader.directory, reader.owner_uid = dirs["supervisor"], os.geteuid()
    Path(value["manifest_file"]).chmod(0o644)
    with pytest.raises(ContractError): reader._private("manifest.json")
    assert Path(value["manifest_file"]).stat().st_mode & 0o777 == 0o644
