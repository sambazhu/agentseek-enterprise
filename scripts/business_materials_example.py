"""Synthetic-only input fixture. Never a deployment or approval generator.

Creates fake controlled sources and disabled template documents in a NEW private
directory; no platform endpoints are contacted and no real identifiers are used.
"""
import argparse
import hashlib
import json
from pathlib import Path
import os


def example(root):
    root = Path(root).resolve()
    root.mkdir(mode=0o700, exist_ok=False)
    source = root / "sources"
    source.mkdir(mode=0o700)
    def store(path, raw):
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(raw)
    sha = lambda raw: hashlib.sha256(raw).hexdigest()
    canonical = lambda obj: json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    sources = {}
    for name, raw in {"api_key": b"SYNTHETIC-NOT-A-KEY", "ca": b"SYNTHETIC-NOT-A-CERTIFICATE",
                      "receipt_key": b"x" * 32, "certificate": b"SYNTHETIC-NOT-A-CERTIFICATE",
                      "private_key": b"SYNTHETIC-NOT-A-PRIVATE-KEY", "broker_token": b"FAKE" * 16}.items():
        path = source / name
        store(path, raw)
        sources[name] = {"path": str(path), "sha256": sha(raw)}
    identity_files = {}
    for name in ("executable", "script", "unit_file"):
        path = source / ("identity-" + name)
        raw = b"SYNTHETIC-NEVER-EXECUTE-" + name.encode()
        store(path, raw)
        identity_files[name] = dict(path=str(path), sha256=sha(raw))
    directories = {}
    for name in ("supervisor", "vault", "fence", "quota", "tracking", "lifecycle", "store", "workspace"):
        path = root / name
        path.mkdir(mode=0o700)
        directories[name] = str(path)
    f = lambda name: {"$file": name}
    h = lambda name: {"$sha256": name}
    d = lambda name: {"$directory": name}
    plan = dict(run_id="synthetic-business", create_token="synthetic-token-a", template_id="synthetic-template",
                boot_id="00000000-0000-4000-8000-000000000001", candidate_sha256="a" * 64,
                request_sha256=h("request"), domain="example.invalid", restricted=True, endpoint="https://example.invalid")
    expires = 2000000100
    pins = dict(executable={"$identity_file": "executable"}, executable_sha256={"$identity_sha256": "executable"},
                script={"$identity_file": "script"}, script_sha256={"$identity_sha256": "script"},
                unit_file={"$identity_file": "unit_file"}, unit_sha256={"$identity_sha256": "unit_file"},
                cmdline_sha256="b" * 64, unit="synthetic-supervisor.service")
    # Pins are deliberately synthetic: exact live pins are separately supplied and
    # authenticated by the installer. This fixture makes no live-identity claim.
    req = dict(request_id="c" * 64, owner_id="synthetic-owner", input_ref="synthetic-input", instruction="Summarize synthetic CSV")
    binding = {k: v for k, v in plan.items() if k != "endpoint"}
    binding.update(intent_sha256="0" * 64, approval_sha256=h("approval"))
    documents = {
        "request": dict(templateID=plan["template_id"], metadata=dict(agentseek_run_id=plan["run_id"],
                        agentseek_create_token=plan["create_token"]), network=dict(allowPublicTraffic=False)),
        "window": dict(schema=1, window_id="synthetic-window", run_id=plan["run_id"], boot_id=plan["boot_id"],
                       candidate_sha256=plan["candidate_sha256"], creator_id="synthetic-creator",
                       inventory_sha256=sha(canonical(["synthetic-writer"]).encode()), accepted=False, revoked=False,
                       starts_epoch=2000000000, ends_epoch=expires,
                       writers=[dict(id="synthetic-writer", abstain=True, confirmed_epoch=1999999999, responsible="fixture")]),
        "approval": dict(schema=1, plan=plan, approved=False, max_creates=1, w0_accepted=False, expires_epoch=expires),
        "precreate": dict(schema=3, plan=plan, candidate_sha256=plan["candidate_sha256"], approval_file=f("approval"),
                          supervisor_directory=d("supervisor"), supervisor_identity=pins, api_key_file=f("api_key"), ca_file=f("ca"),
                          template_pins=dict(template_id=plan["template_id"], node_ip="127.0.0.1",
                                             artifact_id="synthetic-artifact", artifact_sha256="d" * 64),
                          exclusive_window=dict(path=f("window"), digest=h("window"), creator_id="synthetic-creator", writers=["synthetic-writer"])),
        "installation": dict(schema=2, precreate_file=f("precreate"), precreate_sha256=h("precreate"), approval_sha256=h("approval"),
                             request_file=f("request"), vault_directory=d("vault"), fence_directory=d("fence"),
                             receipt_key_file=f("receipt_key"), receipt_key_sha256=h("receipt_key"), api_key_sha256=h("api_key"),
                             ca_sha256=h("ca"), batch_sequence=[plan, dict(plan, create_token="synthetic-token-b", request_sha256="e" * 64)],
                             quota_directory=d("quota")),
        "launcher": dict(schema=1, create_file=f("installation"), create_sha256=h("installation"), candidate_sha256=plan["candidate_sha256"],
                         supervisor_script={"$identity_file": "script"}, supervisor_sha256={"$identity_sha256": "script"}, tracking_directory=d("tracking")),
        "lifecycle": dict(schema=1, slot="A", launcher=f("launcher"), launcher_sha256=h("launcher"), directory=d("lifecycle")),
        "session": dict(schema=1, csv_approved=False, termination_approved=False, expires_epoch=expires, expected_create=binding,
                        input_sha256="f" * 64, instruction_sha256=sha(req["instruction"].encode()), vault_directory=d("vault"),
                        vault_key_file=f("receipt_key"), control=dict(endpoint=plan["endpoint"], domain=plan["domain"], proxy_port=80,
                        api_key={"$ascii": "api_key"}, ca_file=f("ca")), supervisor_directory=d("supervisor"), supervisor_identity=pins),
        "permits": dict(schema=1, approved=False, permits=[dict(request=req, input_sha256="f" * 64,
                        principal_sha256=h("broker_token"), expires_epoch=expires)]),
        "service": dict(schema=2, approved=False, bind_host="127.0.0.1", approved_bind_host="127.0.0.1", bind_port=13100,
                        certificate_file=f("certificate"), private_key_file=f("private_key"), permits_file=f("permits"),
                        permits_sha256=h("permits"), store_directory=d("store"), workspace_directory=d("workspace"),
                        plans=[dict(owner_id=req["owner_id"], request_id=req["request_id"], lifecycle_file=f("lifecycle"),
                                    lifecycle_sha256=h("lifecycle"), business_file=f("session"), business_sha256=h("session"))]),
    }
    spec = dict(schema=1, output_directory=str(root / "output"), sources=sources, identity_files=identity_files,
                directories=directories, documents=documents)
    raw = (json.dumps(spec, sort_keys=True, indent=2) + "\n").encode()
    store(root / "input.json", raw)
    return root / "input.json", sha(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    path, digest = example(args.directory)
    print(json.dumps({"synthetic_only": True, "input": str(path), "sha256": digest}))
