import json
from pathlib import Path
import subprocess
import sys

import pytest

import business_materials as bm
import business_materials_input as adapter
from business_materials_example import example_parameters


@pytest.fixture
def case(tmp_path):
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    path, digest = example_parameters(tmp_path / "synthetic")
    return path, digest, path.parent / "mapped-input.json"


def edit(case, mutate):
    path, _, output = case
    params = json.loads(path.read_bytes())
    mutate(params)
    raw = bm.encode(params)
    path.write_bytes(raw)
    return path, bm.sha(raw), output


def test_mapping_matches_existing_synthetic_contract_and_disables_everything(case):
    path, digest, output = case
    report = adapter.prepare(*case, generate=True)
    assert report["status"] == "DISABLED_MATERIALS_READBACK_VERIFIED"
    assert report["materials"]["files"] == 16 and report["execution_authorized"] is False
    assert json.loads(output.read_bytes()) == json.loads((path.parent / "input.json").read_bytes())
    assert bm.run(output, report["input_sha256"], verify_only=True) == report["materials"]
    for filename, fields in {"approval": ["approved", "w0_accepted"], "window": ["accepted"],
                             "session": ["csv_approved", "termination_approved"], "service": ["approved"],
                             "permits": ["approved"]}.items():
        value = json.loads((path.parent / "output" / (filename + ".json")).read_bytes())
        assert all(value[k] is False for k in fields)


def test_preparation_does_not_materialize_or_mutate_runtime_directories(case):
    report = adapter.prepare(*case)
    assert report["status"] == "DISABLED_INPUT_PREPARED"
    assert not (case[0].parent / "output").exists()
    params = json.loads(case[0].read_bytes())
    assert all(not list(Path(p).iterdir()) for p in params["directories"].values())


@pytest.mark.parametrize("problem", ["service_as_input", "missing_owner", "instruction_mismatch", "pins_mismatch", "approved_window", "missing_window", "missing_source", "missing_digest", "wrong_template", "same_token", "plain_secret", "proxy_override"])
def test_site_input_fails_closed_without_guessing(case, problem):
    def mutate(p):
        if problem == "service_as_input":
            p.clear(); p.update(schema=2, approved=False, bind_host="127.0.0.1")
        elif problem == "missing_owner": p["request"].pop("owner_id")
        elif problem == "instruction_mismatch": p["instruction_sha256"] = "0" * 64
        elif problem == "pins_mismatch": p["supervisor_identity"]["script_sha256"] = "0" * 64
        elif problem == "approved_window": p["window"]["accepted"] = True
        elif problem == "missing_window": p["window"]["writers"][0]["confirmed_epoch"] = None
        elif problem == "missing_source": Path(p["sources"]["api_key"]["path"]).unlink()
        elif problem == "missing_digest": p["sources"]["api_key"].pop("sha256")
        elif problem == "wrong_template": p["template_pins"]["template_id"] = "wrong"
        elif problem == "same_token": p["second_plan"]["create_token"] = p["create"]["create_token"]
        elif problem == "plain_secret": p["api_key"] = "DO-NOT-PRINT-SECRET"
        elif problem == "proxy_override": p["proxy_port"] = 13080
    case = edit(case, mutate)
    with pytest.raises((bm.MaterialError, OSError)): adapter.prepare(*case, generate=True)
    assert not case[2].exists() and not (case[0].parent / "output").exists()


def test_changed_bindings_propagate_without_manual_documents(case):
    def mutate(p):
        p["request"].update(request_id="1" * 64, owner_id="different-owner", input_ref="other-input", instruction="Different CSV task")
        p["instruction_sha256"] = bm.sha(p["request"]["instruction"].encode())
        p["input_sha256"] = "2" * 64
        p["create"].update(run_id="other-run", create_token="other-token", candidate_sha256="3" * 64)
        p["window"].update(run_id="other-run", candidate_sha256="3" * 64)
        p["listener"].update(bind_host="192.10.50.172")
    case = edit(case, mutate)
    adapter.prepare(*case, generate=True)
    root = case[0].parent / "output"
    service, permits = [json.loads((root / (name + ".json")).read_bytes()) for name in ("service", "permits")]
    assert service["plans"][0]["request_id"] == permits["permits"][0]["request"]["request_id"] == "1" * 64
    assert service["plans"][0]["owner_id"] == "different-owner"
    assert service["approved_bind_host"] == "192.10.50.172"


def test_cli_isolated_end_to_end_and_no_overwrite(case):
    command = [sys.executable, "-I", str(Path(adapter.__file__).resolve()), "--parameters", str(case[0]),
               "--sha256", case[1], "--output-input", str(case[2]), "--generate"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0 and not result.stderr
    assert json.loads(result.stdout)["status"] == "DISABLED_MATERIALS_READBACK_VERIFIED"
    again = subprocess.run(command, capture_output=True, text=True)
    assert again.returncode == 2 and not again.stderr and str(case[0]) not in again.stdout


def test_wrong_parameter_pin_has_no_writes(case):
    with pytest.raises(bm.MaterialError, match="parameters_digest"):
        adapter.prepare(case[0], "0" * 64, case[2], generate=True)
    assert not case[2].exists()
