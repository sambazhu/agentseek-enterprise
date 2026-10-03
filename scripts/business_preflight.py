"""Read-only local preparation checks, not a live gate or execution authority."""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

# Isolated CLI resolves only its colocated, reviewed tool (never cwd/PYTHONPATH).
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import business_materials as bm


def store_check(path, owner, request_id):
    path = bm.absolute(path)
    # Never instantiate BusinessStore: its constructor creates/migrates files.
    # Refuse WAL/journal sidecars rather than silently ignore uncheckpointed state.
    for suffix in ("-wal", "-shm", "-journal"):
        bm.need(not path.with_name(path.name + suffix).exists(), "store_sidecar")
    raw = bm.read_private(path)
    bm.need(raw.startswith(b"SQLite format 3\x00") and raw[18:20] == b"\x01\x01", "store_format")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(raw)
        db.execute("PRAGMA query_only=ON")
        bm.need(db.execute("PRAGMA quick_check").fetchall() == [("ok",)], "store_integrity")
        rows = db.execute("SELECT id,owner,request,state FROM attempts LIMIT 10001").fetchall()
        bm.need(len(rows) <= 10000, "store_limit")
        attempt = hashlib.sha256(json.dumps([owner, request_id]).encode()).hexdigest()
        for ident, row_owner, request_raw, state in rows:
            bm.need(state in {"succeeded", "failed"}, "unresolved_attempt")
            request = bm.decode(request_raw)
            bm.need(type(request) is dict and {"owner_id", "request_id"} <= set(request), "store_request")
            bm.need(row_owner == request["owner_id"], "store_owner")
            bm.need(ident != attempt and request["request_id"] != request_id, "request_consumed")
    finally:
        db.close()
    bm.need(bm.read_private(path) == raw, "store_changed")
    for suffix in ("-wal", "-shm", "-journal"):
        bm.need(not path.with_name(path.name + suffix).exists(), "store_sidecar")
    return len(rows)


def run(path, digest):
    raw = bm.read_private(bm.absolute(str(path)))
    bm.need(bm.sha(raw) == bm.digest(digest), "preflight_digest")
    spec = bm.decode(raw)
    common = {"schema", "role", "owner_id", "request_id", "store_file"}
    bm.need(type(spec) is dict and spec.get("role") in {"node", "gateway"}, "role")
    fields = common | ({"materials_input", "materials_sha256", "manifest_file"} if spec["role"] == "node" else set())
    bm.need(set(spec) == fields and type(spec["schema"]) is int and spec["schema"] == 1, "fields")
    bm.digest(spec["request_id"])
    bm.need(type(spec["owner_id"]) is str and 0 < len(spec["owner_id"]) <= 1024, "owner")
    checked = ["local_store", "request_unused"]
    if spec["role"] == "node":
        bm.run(spec["materials_input"], spec["materials_sha256"], verify_only=True)
        source = bm.read_private(bm.absolute(spec["materials_input"]))
        bm.need(bm.sha(source) == spec["materials_sha256"], "input_digest")
        materials = bm.decode(source)
        _, _, expected = bm.compile_materials(materials)
        service = bm.decode(expected["service"])
        plan = service["plans"][0]
        bm.need(plan["owner_id"] == spec["owner_id"] and plan["request_id"] == spec["request_id"], "request_binding")
        bm.need(str(Path(service["store_directory"]) / "business.sqlite") == spec["store_file"], "store_binding")
        for name in ("tracking", "lifecycle", "quota", "fence"):
            directory = bm.absolute(materials["directories"][name])
            bm.private_directory(directory)
            bm.need(not any(directory.iterdir()), "nonempty_" + name)
        manifest_path = bm.absolute(spec["manifest_file"])
        bm.need(manifest_path == bm.absolute(materials["directories"]["supervisor"]) / "manifest.json", "manifest_path")
        manifest = bm.decode(bm.read_private(manifest_path))
        create = bm.decode(expected["approval"])["plan"]
        bm.need(type(manifest) is dict and manifest.get("run_id") == create["run_id"]
                and manifest.get("template_id") == create["template_id"]
                and manifest.get("sandboxes") == {}, "manifest_binding_or_nonempty")
        checked += ["materials_readback", "empty_tracking_lifecycle_quota_fence", "private_empty_manifest"]
    rows = store_check(spec["store_file"], spec["owner_id"], spec["request_id"])
    bm.need(bm.read_private(bm.absolute(str(path))) == raw, "preflight_changed")
    return dict(status="LOCAL_PREPARATION_PASS", role=spec["role"], checks=checked,
                terminal_rows=rows, preflight_sha256=digest,
                binding_sha256=bm.sha(bm.encode([spec["owner_id"], spec["request_id"]])), execution_authorized=False,
                live_gate_required=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        result = run(args.input, args.sha256)
    except Exception as exc:
        # No paths, request IDs, SQL values, credentials or exception traceback.
        result = dict(status="BLOCKED", error=str(exc) if type(exc) is bm.MaterialError else "io_or_contract",
                      execution_authorized=False)
        print(json.dumps(result))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
