import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import business_materials as bm
from business_materials_example import example


@pytest.fixture
def case(tmp_path):
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    path, digest = example(tmp_path / "fixture")
    return path, digest


def edit(case, change):
    path, _ = case
    spec = json.loads(path.read_bytes())
    change(spec)
    raw = bm.encode(spec)
    path.write_bytes(raw)
    return path, bm.sha(raw)


def test_family_materialized_and_repeat_verify_read_only(case):
    path, digest = case
    report = bm.run(path, digest)
    assert report == dict(status="MATERIALS_READBACK_VERIFIED", files=16,
                         service_sha256=bm.sha((path.parent / "output/service.json").read_bytes()), execution_authorized=False)
    spec = json.loads(path.read_bytes())
    root = path.parent / "output"
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
    assert bm.run(path, digest, verify_only=True) == report
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
    assert (root / "api_key.asset").read_bytes() == Path(spec["sources"]["api_key"]["path"]).read_bytes()
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.iterdir())
    assert not json.loads((root / "service.json").read_bytes())["approved"]
    with pytest.raises(FileExistsError):
        bm.run(path, digest)


@pytest.mark.parametrize("name", sorted(bm.ASSETS))
def test_missing_source_never_creates_output(case, name):
    spec = json.loads(case[0].read_bytes())
    Path(spec["sources"][name]["path"]).unlink()
    with pytest.raises(OSError): bm.run(*case)
    assert not Path(spec["output_directory"]).exists()


@pytest.mark.parametrize("problem", ["missing_pin", "wrong_pin", "newline", "mode", "parent_mode", "symlink", "fifo"])
def test_source_contract(case, problem):
    spec = json.loads(case[0].read_bytes())
    src = Path(spec["sources"]["api_key"]["path"])
    if problem == "missing_pin":
        case = edit(case, lambda s: s["sources"]["api_key"].pop("sha256"))
    elif problem == "wrong_pin":
        case = edit(case, lambda s: s["sources"]["api_key"].update(sha256="0" * 64))
    elif problem == "newline":
        src.write_bytes(src.read_bytes() + b"\n")
        case = edit(case, lambda s: s["sources"]["api_key"].update(sha256=bm.sha(src.read_bytes())))
    elif problem == "mode": src.chmod(0o644)
    elif problem == "parent_mode": src.parent.chmod(0o755)
    elif problem == "symlink":
        src.unlink(); src.symlink_to(src.parent / "broker_token")
    elif problem == "fifo":
        src.unlink(); os.mkfifo(src, 0o600)
    with pytest.raises(bm.MaterialError): bm.run(*case)
    assert not Path(spec["output_directory"]).exists()


@pytest.mark.parametrize("problem", ["literal_file", "bad_reference", "cycle", "bad_digest", "bad_proxy", "bad_window", "extra_field", "approval_drift", "batch", "inline_key"])
def test_cross_file_failures_before_output(case, problem):
    def change(s):
        docs = s["documents"]
        if problem == "literal_file": docs["precreate"]["api_key_file"] = "/private/never-covered"
        elif problem == "bad_reference": docs["precreate"]["api_key_file"] = {"$file": "ca"}
        elif problem == "cycle": docs["approval"]["plan"]["request_sha256"] = {"$sha256": "approval"}
        elif problem == "bad_digest": docs["installation"]["api_key_sha256"] = "0" * 64
        elif problem == "bad_proxy": docs["session"]["control"]["proxy_port"] = 13080
        elif problem == "bad_window": docs["window"]["writers"][0]["confirmed_epoch"] = 2100000000
        elif problem == "extra_field": docs["service"]["extra"] = True
        elif problem == "approval_drift": docs["service"]["approved"] = True
        elif problem == "batch": docs["installation"]["batch_sequence"] = docs["installation"]["batch_sequence"][:1]
        elif problem == "inline_key": docs["session"]["control"]["api_key"] = "SECRET-DO-NOT-PRINT"
    case = edit(case, change)
    with pytest.raises(bm.MaterialError): bm.run(*case)
    assert not (case[0].parent / "output").exists()


@pytest.mark.parametrize("name", ["api_key.asset", "precreate.json", "session.json", "service.json"])
def test_verify_does_not_self_attest_changed_output(case, name):
    bm.run(*case)
    target = case[0].parent / "output" / name
    target.write_bytes(b"tampered")
    with pytest.raises(bm.MaterialError, match="output_digest"): bm.run(*case, verify_only=True)


def test_deleted_materialization_and_output_permissions(case):
    bm.run(*case)
    root = case[0].parent / "output"
    (root / "api_key.asset").chmod(0o644)
    with pytest.raises(bm.MaterialError, match="file_permissions"): bm.run(*case, verify_only=True)
    (root / "api_key.asset").unlink()
    with pytest.raises(bm.MaterialError, match="output_inventory"): bm.run(*case, verify_only=True)


def test_readback_catches_writer_omission_preserves_failure(case, monkeypatch):
    original = bm.write_new
    def omit(path, raw):
        if path.name != "api_key.asset": original(path, raw)
    monkeypatch.setattr(bm, "write_new", omit)
    with pytest.raises(bm.MaterialError, match="output_inventory"): bm.run(*case)
    assert (case[0].parent / "output/session.json").exists()


def test_source_changes_after_writes_not_ready(case, monkeypatch):
    original = bm.write_new
    def mutate(path, raw):
        original(path, raw)
        if path.name == "service.json":
            (case[0].parent / "sources/api_key").write_bytes(b"changed-source")
    monkeypatch.setattr(bm, "write_new", mutate)
    with pytest.raises(bm.MaterialError, match="source_digest"): bm.run(*case)


def test_cli_failure_is_nonzero_sanitized(case):
    path, pin = edit(case, lambda s: s["documents"]["session"]["control"].update(api_key="SECRET-DO-NOT-PRINT"))
    result = subprocess.run([sys.executable, "-I", str(Path(bm.__file__).resolve()), "--input", str(path), "--sha256", pin],
                            capture_output=True, text=True)
    assert result.returncode == 2 and not result.stderr
    assert "SECRET" not in result.stdout and "READY" not in result.stdout and str(path) not in result.stdout
    assert json.loads(result.stdout)["status"] == "BLOCKED"


def test_input_pin_and_duplicate_key(case):
    with pytest.raises(bm.MaterialError, match="input_digest"): bm.run(case[0], "0" * 64)
    with pytest.raises(bm.MaterialError, match="duplicate_key"): bm.decode(b'{"x":1,"x":2}')


def test_no_runtime_import_or_process_network_control():
    import ast
    tree = ast.parse(Path(bm.__file__).read_text())
    imports = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    assert imports <= {"argparse", "hashlib", "json", "os", "re", "stat", "sys"}
    assert "agentseek_execution" not in Path(bm.__file__).read_text()


@pytest.mark.parametrize("name", sorted(bm.IDENTITY_FILES))
def test_runtime_identity_file_drift_is_not_self_attested(case, name):
    spec = json.loads(case[0].read_bytes())
    Path(spec["identity_files"][name]["path"]).write_bytes(b"changed")
    with pytest.raises(bm.MaterialError, match="identity_digest"): bm.run(*case)


def test_exact_existing_readers_accept_generated_structure_without_network(case, monkeypatch):
    from agentseek_execution.business_service import load_config
    from agentseek_execution.business_cube_session import ApprovedCsvSessionFactory
    from agentseek_execution.m3_create_worker import CreatePlan
    from agentseek_execution.m3_supervisor_identity import IdentityPins
    from agentseek_execution.m3_batch_quota import BatchQuota
    from dataclasses import asdict
    from types import SimpleNamespace
    from agentseek_execution import m3_exclusive_window as windows, m3_create_worker as approvals
    from agentseek_execution.m3_supervisor_snapshot import ClockSample
    # Synthetic explicit approval is test data, not a site authorization. These
    # APIs only parse files/construct objects; no server or create entry is called.
    def approve(s):
        docs = s["documents"]
        for name, flags in {"approval": ["approved", "w0_accepted"], "window": ["accepted"],
                            "session": ["csv_approved", "termination_approved"],
                            "service": ["approved"], "permits": ["approved"]}.items():
            for flag in flags: docs[name][flag] = True
    path, digest = edit(case, approve)
    result = bm.run(path, digest)
    root = path.parent / "output"
    load = lambda name: json.loads((root / (name + ".json")).read_bytes())
    service, permits, plans = load_config(root / "service.json", result["service_sha256"])
    assert len(permits) == len(plans) == 1
    installed, pre = load("installation"), load("precreate")
    session = ApprovedCsvSessionFactory(root / "session.json", bm.sha((root / "session.json").read_bytes()))
    session.validate_installation(installed, pre)
    binding = CreatePlan(**pre["plan"]).binding(installed["approval_sha256"])
    assert asdict(binding) == load("session")["expected_create"]
    IdentityPins(**pre["supervisor_identity"]).validate()
    BatchQuota(Path(installed["quota_directory"]), tuple(CreatePlan(**p) for p in installed["batch_sequence"]))
    # Isolate only the root gate, not config_bytes or file/digest readers; W0's
    # clock is explicitly synthetic. No claim of a live Linux identity/window.
    monkeypatch.setattr(windows, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0))
    monkeypatch.setattr(approvals, "os", SimpleNamespace(getuid=lambda: 0))
    w = load("window")
    window = windows.read_window(root / "window.json", pinned_digest=bm.sha((root / "window.json").read_bytes()),
                                plan=CreatePlan(**pre["plan"]), creator_id=w["creator_id"],
                                expected_writers=tuple(x["id"] for x in w["writers"]),
                                now=ClockSample(w["boot_id"], w["starts_epoch"] + 1, 100.0, 100.0))
    assert window.aggregate_ready is False
    approvals.read_create_approval(CreatePlan(**pre["plan"]), root / "approval.json",
                                   pinned_digest=installed["approval_sha256"])
    # Parsing must not reserve a quota, touch state or create anything.
    spec = json.loads(path.read_bytes())
    assert all(not list(Path(p).iterdir()) for p in spec["directories"].values())


def test_cli_synthetic_generation_and_verify(case):
    command = [sys.executable, "-I", str(Path(bm.__file__).resolve()), "--input", str(case[0]), "--sha256", case[1]]
    for suffix in ([], ["--verify-only"]):
        result = subprocess.run(command + suffix, capture_output=True, text=True)
        assert result.returncode == 0 and not result.stderr
        assert json.loads(result.stdout)["execution_authorized"] is False
