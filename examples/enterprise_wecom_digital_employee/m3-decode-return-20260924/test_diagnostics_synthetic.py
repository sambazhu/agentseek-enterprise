import json, logging
from agentseek_execution import business_cube_session as bcs
from agentseek_execution.execution_diagnostic import emit, error_kind, STAGES, EVENTS
from agentseek_execution.models import ContractError, Code

def test_diagnostics_enabled_default_off_seam():
    plan_off = {}
    plan_explicit_off = {"diagnostics_enabled": False}
    assert plan_off.get("diagnostics_enabled", False) is False
    assert plan_explicit_off.get("diagnostics_enabled", False) is False
    assert bcs.new_evidence() if plan_off.get("diagnostics_enabled", False) else None is None

def test_diagnostics_enabled_explicit_on_attaches_structure_only():
    plan = {"diagnostics_enabled": True}
    evidence = bcs.new_evidence() if plan.get("diagnostics_enabled", False) else None
    assert evidence is not None
    body = b"\x00\x00\x00\x00\x00"  # 非法流：截断帧
    try:
        bcs.decode_command(body, evidence)
        raise AssertionError("must raise")
    except Exception:
        pass
    bcs.validate_evidence(evidence)  # 结构白名单校验通过
    assert set(evidence) == set(bcs.new_evidence())
    assert all(isinstance(p, list) and all(isinstance(n, int) for n in p) for p in evidence["frames"])
    blob = json.dumps(evidence)
    assert not any(k in blob for k in ("SECRET", "password", "api_key"))

def test_protocol_program_classification_separated():
    assert bcs.classify_error(bcs.CommandProgramError()) == "program"
    assert bcs.classify_error(bcs.CommandProtocolError()) == "protocol"
    assert bcs.classify_error(ValueError("x")) == "protocol"
    assert bcs.classify_error(ContractError(Code.DENIED)) == "contract"
    try:
        bcs.end_code({"exitCode": 0, "exit_code": 0})
        raise AssertionError
    except bcs.CommandProtocolError: pass
    try:
        bcs.end_code({"error": "boom"})
        raise AssertionError
    except bcs.CommandProgramError: pass
    try:
        bcs.end_code({"status": "weird"})
        raise AssertionError
    except bcs.CommandProtocolError: pass

def test_emit_scalar_allowlist_no_exception_bodies(caplog):
    rec = {}
    class Ctx:
        def __init__(self): self._diagnostic_request = ["req"]
    with caplog.at_level(logging.WARNING, logger="agentseek_execution.execution_diagnostic"):
        emit("provider_run", "failed", exc=RuntimeError("SECRET-credential body /var/secret/x"), worker_id="a"*32, returncode=1)
        emit("bogus_stage", "failed")
        emit("provider_run", "bogus_event")
    lines = [r.getMessage() for r in caplog.records if "execution_stage" in r.getMessage()]
    assert len(lines) == 1, lines  # 白名单外 stage/event 被丢弃
    payload = json.loads(lines[0].split("execution_stage ", 1)[1])
    assert set(payload) == {"scope_id","request_sha256","stage","event","pid","thread_id",
                            "epoch_ms","mono_ms","elapsed_ms","error","worker_id","child_pid","returncode"}
    assert payload["error"] == "RuntimeError"  # 仅类型名，无正文
    assert "SECRET" not in lines[0] and "credential" not in lines[0]
    assert payload["stage"] in STAGES and payload["event"] in EVENTS

def test_materials_synthetic_stay_disabled():
    import sys
    sys.path.insert(0, "/root/m3-decode-handoff-20260924/tools")
    import business_materials as bm, business_materials_input as bmi
    def dg(): return "a"*64
    p = {
      "schema": 1,
      "output_directory": "/root/m3-decode-scratch-20260924/synth-out",
      "sources": {k: {"path": f"/tmp/{k}", "sha256": dg()} for k in bm.ASSETS},
      "identity_files": {k: {"path": f"/tmp/{k}", "sha256": dg()} for k in bm.IDENTITY_FILES},
      "directories": {k: f"/tmp/{k}" for k in bm.DIRECTORIES},
      "create": {"run_id": "r"*8+"x"*8, "create_token": "tok-A", "template_id": "tpl-x",
                 "boot_id": "b"*36, "candidate_sha256": dg(), "domain": "cube.app", "endpoint": "https://e"},
      "second_plan": {"create_token": "tok-B", "request_sha256": dg()},
      "window": dict({"accepted": False, "revoked": False, "creator_id": "creator-x",
                  "writers": [{"id": "w1"}, {"id": "w2"}]},
                 **{k: 1 for k in bm.FIELDS["window"].split() if k.endswith("epoch")},
                 **{k: dg() for k in bm.FIELDS["window"].split() if k.endswith("sha256")},
                 **{k: "x" for k in bm.FIELDS["window"].split()
                    if k not in ("accepted","revoked","creator_id","writers")
                    and not k.endswith("epoch") and not k.endswith("sha256")}),
      "supervisor_identity": {"executable": "/tmp/executable", "executable_sha256": dg(),
                              "script": "/tmp/script", "script_sha256": dg(),
                              "unit_file": "/tmp/unit_file", "unit_sha256": dg(),
                              "cmdline_sha256": dg(), "unit": "u.service"},
      "template_pins": {"template_id": "tpl-x", "node_ip": "192.0.2.1", "artifact_id": "a", "artifact_sha256": dg()},
      "request": {"request_id": dg(), "owner_id": dg(), "input_ref": "file_x", "instruction": "i"},
      "input_sha256": dg(), "instruction_sha256": bm.sha(b"i"),
      "listener": {"bind_host": "192.0.2.1", "bind_port": 13100},
    }
    spec = bmi.build_input(p)
    docs = spec["documents"]
    assert docs["approval"]["approved"] is False and docs["approval"]["w0_accepted"] is False
    assert docs["session"]["csv_approved"] is False and docs["session"]["termination_approved"] is False
    assert docs["permits"]["approved"] is False and docs["service"]["approved"] is False
    assert docs["window"]["accepted"] is False and docs["window"]["revoked"] is False
    assert docs["request"]["network"]["allowPublicTraffic"] is False
    assert spec["output_directory"] and "execution_authorized" not in json.dumps(spec)
