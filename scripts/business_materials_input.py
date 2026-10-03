"""Map pinned site parameters to DISABLED business-material input; never approve.

The service's 12-field config is an OUTPUT, not this tool's input. No secrets
in arguments or parameter JSON. No discovery, key minting, window synthesis,
directory reset, platform access, unit writes or service changes.
"""
import argparse
import copy
import json
from pathlib import Path
import sys

# -I deliberately ignores PYTHONPATH. Only the delivered, manifest-verified tool
# directory is added; no cwd or user-controlled import directory is consulted.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import business_materials as bm


PARAMETERS = {"schema", "output_directory", "sources", "identity_files", "directories", "create",
              "second_plan", "window", "supervisor_identity", "template_pins", "request",
              "input_sha256", "instruction_sha256", "listener"}


def build_input(parameters):
    p = copy.deepcopy(parameters)
    bm.need(type(p) is dict and set(p) in (PARAMETERS, PARAMETERS | {"diagnostics_enabled"}), "parameter_fields")
    bm.need(type(p.get("diagnostics_enabled", False)) is bool, "diagnostics_type")
    bm.need(type(p["schema"]) is int and p["schema"] == 1, "parameter_schema")
    create = p["create"]
    bm.need(set(create) == {"run_id", "create_token", "template_id", "boot_id", "candidate_sha256", "domain", "endpoint"}, "create_fields")
    second = p["second_plan"]
    bm.need(set(second) == {"create_token", "request_sha256"}, "second_plan_fields")
    bm.digest(second["request_sha256"])
    bm.need(second["create_token"] != create["create_token"], "second_plan_token")
    req = p["request"]
    bm.need(set(req) == {"request_id", "owner_id", "input_ref", "instruction"}, "request_fields")
    bm.need(all(type(value) is str and value for value in req.values()), "request_values")
    bm.digest(req["request_id"])
    for key in ("input_sha256", "instruction_sha256"):
        bm.digest(p[key])
    bm.need(bm.sha(req["instruction"].encode("utf-8")) == p["instruction_sha256"], "instruction_digest")
    window = p["window"]
    bm.need(set(window) == set(bm.FIELDS["window"].split()), "window_fields")
    bm.need(window["accepted"] is False and window["revoked"] is False, "disabled_window_required")
    template = p["template_pins"]
    bm.need(set(template) == {"template_id", "node_ip", "artifact_id", "artifact_sha256"}, "template_fields")
    bm.need(template["template_id"] == create["template_id"], "template_binding")
    bm.digest(template["artifact_sha256"])
    listener = p["listener"]
    bm.need(set(listener) == {"bind_host", "bind_port"}, "listener_fields")
    pins = p["supervisor_identity"]
    bm.need(set(pins) == {"executable", "executable_sha256", "script", "script_sha256", "unit_file",
                         "unit_sha256", "cmdline_sha256", "unit"}, "pins_fields")
    # Compare the supplied real pins BEFORE replacing their paths with symbolic
    # references. A mismatch cannot silently disappear during conversion.
    for name, pin in (("executable", "executable_sha256"), ("script", "script_sha256"), ("unit_file", "unit_sha256")):
        source = p["identity_files"][name]
        bm.need(pins[name] == source["path"] and pins[pin] == source["sha256"], "pins_source_mismatch")
        pins[name], pins[pin] = {"$identity_file": name}, {"$identity_sha256": name}
    f = lambda name: {"$file": name}
    h = lambda name: {"$sha256": name}
    d = lambda name: {"$directory": name}
    plan = dict(create, request_sha256=h("request"), restricted=True)
    expires = window["ends_epoch"]
    binding = {k: value for k, value in plan.items() if k != "endpoint"}
    binding.update(intent_sha256="0" * 64, approval_sha256=h("approval"))
    documents = {
        "request": dict(templateID=plan["template_id"], metadata=dict(agentseek_run_id=plan["run_id"],
                        agentseek_create_token=plan["create_token"]), network=dict(allowPublicTraffic=False)),
        "window": window,
        "approval": dict(schema=1, plan=plan, approved=False, max_creates=1, w0_accepted=False, expires_epoch=expires),
        "precreate": dict(schema=3, plan=plan, candidate_sha256=plan["candidate_sha256"], approval_file=f("approval"),
                          supervisor_directory=d("supervisor"), supervisor_identity=pins, api_key_file=f("api_key"), ca_file=f("ca"),
                          template_pins=template, exclusive_window=dict(path=f("window"), digest=h("window"),
                          creator_id=window["creator_id"], writers=[w["id"] for w in window["writers"]])),
        "installation": dict(schema=2, precreate_file=f("precreate"), precreate_sha256=h("precreate"), approval_sha256=h("approval"),
                             request_file=f("request"), vault_directory=d("vault"), fence_directory=d("fence"),
                             receipt_key_file=f("receipt_key"), receipt_key_sha256=h("receipt_key"), api_key_sha256=h("api_key"),
                             ca_sha256=h("ca"), batch_sequence=[plan, dict(plan, **second)], quota_directory=d("quota")),
        "launcher": dict(schema=1, create_file=f("installation"), create_sha256=h("installation"), candidate_sha256=plan["candidate_sha256"],
                         supervisor_script={"$identity_file": "script"}, supervisor_sha256={"$identity_sha256": "script"}, tracking_directory=d("tracking")),
        "lifecycle": dict(schema=1, slot="A", launcher=f("launcher"), launcher_sha256=h("launcher"), directory=d("lifecycle")),
        "session": dict(schema=1, csv_approved=False, termination_approved=False, expires_epoch=expires, expected_create=binding,
                        input_sha256=p["input_sha256"], instruction_sha256=p["instruction_sha256"], vault_directory=d("vault"),
                        vault_key_file=f("receipt_key"), control=dict(endpoint=plan["endpoint"], domain=plan["domain"], proxy_port=80,
                        api_key={"$ascii": "api_key"}, ca_file=f("ca")), supervisor_directory=d("supervisor"), supervisor_identity=pins),
        "permits": dict(schema=1, approved=False, permits=[dict(request=req, input_sha256=p["input_sha256"],
                        principal_sha256=h("broker_token"), expires_epoch=expires)]),
        "service": dict(schema=2, approved=False, bind_host=listener["bind_host"], approved_bind_host=listener["bind_host"],
                        bind_port=listener["bind_port"], certificate_file=f("certificate"), private_key_file=f("private_key"),
                        permits_file=f("permits"), permits_sha256=h("permits"), store_directory=d("store"), workspace_directory=d("workspace"),
                        plans=[dict(owner_id=req["owner_id"], request_id=req["request_id"], lifecycle_file=f("lifecycle"),
                                    lifecycle_sha256=h("lifecycle"), business_file=f("session"), business_sha256=h("session"))]),
    }
    if "diagnostics_enabled" in p:
        documents["session"]["diagnostics_enabled"] = p["diagnostics_enabled"]
    return dict(schema=1, output_directory=p["output_directory"], sources=p["sources"], identity_files=p["identity_files"],
                directories=p["directories"], documents=documents)


def prepare(path, digest, output, *, generate=False):
    raw = bm.read_private(bm.absolute(str(path)))
    bm.need(bm.sha(raw) == bm.digest(digest), "parameters_digest")
    spec = build_input(bm.decode(raw))
    # Validate controlled sources and the complete graph BEFORE writing input.
    bm.compile_materials(spec)
    output = bm.absolute(str(output))
    root = bm.absolute(spec["output_directory"])
    bm.need(not output.is_relative_to(root), "input_inside_output")
    bm.private_directory(output.parent)
    encoded = bm.encode(spec)
    bm.need(bm.read_private(bm.absolute(str(path))) == raw, "parameters_changed")
    bm.write_new(output, encoded)
    bm.need(bm.read_private(output) == encoded, "input_readback")
    result = dict(status="DISABLED_INPUT_PREPARED", input_sha256=bm.sha(encoded), execution_authorized=False)
    if generate:
        result["materials"] = bm.run(output, result["input_sha256"])
        result["status"] = "DISABLED_MATERIALS_READBACK_VERIFIED"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output-input", required=True)
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare(args.parameters, args.sha256, args.output_input, generate=args.generate)
    except Exception as exc:
        code = str(exc) if type(exc) is bm.MaterialError else "io_or_contract"
        print(json.dumps(dict(status="BLOCKED", error=code, execution_authorized=False)))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
