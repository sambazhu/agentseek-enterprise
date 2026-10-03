"""Offline, pinned business-material compiler. No execution or service authority.

Inputs are operator-reviewed templates, NOT executable Python. Controlled source
bytes are authenticated before any output is created. Expected JSON bytes are
compiled in memory, then independently read back from disk. Re-verification uses
the same pinned input, never a checksum manufactured from the output under test.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

LIMIT = 2_000_000
ASSETS = {"api_key", "ca", "receipt_key", "certificate", "private_key", "broker_token"}
IDENTITY_FILES = {"executable", "script", "unit_file"}
FIELDS = {
    "request": "templateID metadata network",
    "window": "schema window_id run_id boot_id candidate_sha256 creator_id inventory_sha256 accepted revoked starts_epoch ends_epoch writers",
    "approval": "schema plan approved max_creates w0_accepted expires_epoch",
    "precreate": "schema plan candidate_sha256 approval_file supervisor_directory supervisor_identity api_key_file ca_file template_pins exclusive_window",
    "installation": "schema precreate_file precreate_sha256 approval_sha256 request_file vault_directory fence_directory receipt_key_file receipt_key_sha256 api_key_sha256 ca_sha256 batch_sequence quota_directory",
    "launcher": "schema create_file create_sha256 candidate_sha256 supervisor_script supervisor_sha256 tracking_directory",
    "lifecycle": "schema slot launcher launcher_sha256 directory",
    "session": "schema csv_approved termination_approved expires_epoch expected_create input_sha256 instruction_sha256 vault_directory vault_key_file control supervisor_directory supervisor_identity",
    "permits": "schema approved permits",
    "service": "schema approved bind_host bind_port certificate_file private_key_file permits_file permits_sha256 store_directory workspace_directory plans approved_bind_host",
}
DIRECTORIES = {"supervisor", "vault", "fence", "quota", "tracking", "lifecycle", "store", "workspace"}


class MaterialError(Exception):
    pass


def need(condition, code="contract"):
    if not condition:
        raise MaterialError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    need(type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None, "uncovered_digest")
    return value


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "duplicate_key")
            result[key] = value
        return result
    def invalid(_):
        raise MaterialError("nonfinite")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def absolute(value):
    need(type(value) is str, "path")
    path = Path(value)
    need(path.is_absolute() and str(path) == value and path.resolve() == path, "path")
    return path


def private_directory(path):
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
         and info.st_uid == os.geteuid(), "directory_permissions")


def read_private(path):
    return read_file(path, private=True)


def read_file(path, *, private):
    path = absolute(str(path))
    if private:
        private_directory(path.parent)
    else:
        parent = path.parent.lstat()
        need(stat.S_ISDIR(parent.st_mode) and not parent.st_mode & 0o022
             and parent.st_uid in {0, os.geteuid()}, "directory_permissions")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "file_permissions")
        need((stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid()) if private else
             (not info.st_mode & 0o022 and info.st_uid in {0, os.geteuid()}), "file_permissions")
        limit = LIMIT if private else 32 * 1024 * 1024
        need(info.st_size <= limit, "size")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(limit + 1)
        need(len(raw) <= limit, "size")
        after = os.fstat(fd)
        need((info.st_size, info.st_mtime_ns, info.st_ctime_ns) ==
             (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "changed_during_read")
        return raw
    finally:
        os.close(fd)


def write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def compile_materials(spec):
    need(type(spec) is dict and set(spec) == {"schema", "output_directory", "sources", "identity_files", "directories", "documents"})
    need(type(spec["schema"]) is int and spec["schema"] == 1)
    root = absolute(spec["output_directory"])
    private_directory(root.parent)
    sources, directories, templates = spec["sources"], spec["directories"], spec["documents"]
    need(set(sources) == ASSETS and set(directories) == DIRECTORIES and set(templates) == set(FIELDS))
    paths, expected, values = {}, {}, {}
    identities = spec["identity_files"]
    need(set(identities) == IDENTITY_FILES)
    for identity_name, source in identities.items():
        need(set(source) == {"path", "sha256"}, "uncovered_digest")
        pinned = digest(source["sha256"])
        source_path = absolute(source["path"])
        need(not source_path.is_relative_to(root), "source_is_output")
        # Launcher reads its script through config_bytes (0600 / parent0700),
        # whereas process identity permits protected system binaries/unit files.
        content = read_file(source_path, private=identity_name == "script")
        need(identity_name != "script" or len(content) <= 65536, "runtime_file_size")
        need(sha(content) == pinned, "identity_digest")
    for name, source in sources.items():
        need(type(source) is dict and set(source) == {"path", "sha256"}, "uncovered_digest")
        pinned = digest(source["sha256"])
        source_path = absolute(source["path"])
        need(not source_path.is_relative_to(root), "source_is_output")
        raw = read_private(source_path)
        need(sha(raw) == pinned, "source_digest")
        expected[name] = raw
        paths[name] = root / (name + ".asset")
    need(len(expected["receipt_key"]) == 32, "receipt_key")
    for name, low, high in (("api_key", 1, 4096), ("broker_token", 32, 256)):
        raw = expected[name]
        need(low <= len(raw) <= high and all(33 <= byte <= 126 for byte in raw), "credential_format")
    dirs = {name: absolute(path) for name, path in directories.items()}
    for path in dirs.values():
        private_directory(path)
        need(not path.is_relative_to(root) and not root.is_relative_to(path), "directory_overlap")
    need(len(set(dirs.values())) == len(dirs), "directory_overlap")
    for a in dirs.values():
        for b in dirs.values():
            need(a == b or not a.is_relative_to(b), "directory_overlap")
    paths.update({name: root / (name + ".json") for name in templates})
    visiting = set()

    def build(name):
        need(name in paths, "unknown_reference")
        if name in expected:
            return expected[name]
        need(name not in visiting, "reference_cycle")
        visiting.add(name)
        value = resolve(templates[name])
        fields = set(FIELDS[name].split())
        optional = {"diagnostics_enabled"} if name == "session" else set()
        need(type(value) is dict and set(value) in (fields, fields | optional), "field_set")
        if name == "session":
            need(type(value.get("diagnostics_enabled", False)) is bool, "diagnostics_type")
        values[name] = value
        expected[name] = encode(value)
        visiting.remove(name)
        return expected[name]

    def resolve(value, key=""):
        if type(value) is dict and any(k.startswith("$") for k in value):
            need(len(value) == 1, "reference")
            op, name = next(iter(value.items()))
            need(type(name) is str, "reference")
            if op == "$file":
                need(name in paths, "unknown_reference")
                return str(paths[name])
            if op == "$sha256":
                return sha(build(name))
            if op == "$ascii":
                need(name == "api_key", "inline_secret")
                return expected[name].decode("ascii")
            if op in {"$identity_file", "$identity_sha256"}:
                need(name in identities, "unknown_reference")
                return identities[name]["path" if op == "$identity_file" else "sha256"]
            if op == "$directory":
                need(name in dirs, "unknown_reference")
                return str(dirs[name])
            raise MaterialError("reference")
        # Path-bearing input must be symbolic; an untracked literal cannot pass.
        if key.endswith("_file") or key in {"launcher", "supervisor_script", "path"}:
            raise MaterialError("uncovered_reference")
        if key == "api_key":
            raise MaterialError("inline_secret")
        if type(value) is dict:
            return {k: resolve(v, k) for k, v in value.items()}
        if type(value) is list:
            return [resolve(item) for item in value]
        return value

    for name in templates:
        build(name)
    def check_digests(value):
        if type(value) is dict:
            for key, child in value.items():
                if key.endswith("sha256"):
                    digest(child)
                check_digests(child)
        elif type(value) is list:
            for child in value:
                check_digests(child)
    check_digests(values)
    need(all(len(raw) <= 65536 for raw in expected.values()), "runtime_file_size")
    validate_links(values, paths, expected, dirs, identities)
    return root, paths, expected


def validate_links(v, p, raw, dirs, identities):
    """Static graph checks only. No network, live gate, store or quota constructor."""
    a, w, pre, ins = (v[n] for n in ("approval", "window", "precreate", "installation"))
    launch, life, session, service = (v[n] for n in ("launcher", "lifecycle", "session", "service"))
    plan = a["plan"]
    need(set(plan) == {"run_id", "create_token", "template_id", "boot_id", "candidate_sha256",
                       "request_sha256", "domain", "restricted", "endpoint"})
    need(plan["restricted"] is True)
    for name, schema in {"request": None, "precreate": 3, "installation": 2, "service": 2}.items():
        if schema is not None:
            need(type(v[name]["schema"]) is int and v[name]["schema"] == schema)
    for name in set(v) - {"request", "precreate", "installation", "service"}:
        need(type(v[name]["schema"]) is int and v[name]["schema"] == 1)
    for doc, flag in ((a, "approved"), (a, "w0_accepted"), (w, "accepted"),
                      (session, "csv_approved"), (session, "termination_approved"),
                      (service, "approved"), (v["permits"], "approved")):
        need(type(doc[flag]) is bool, "approval_type")
    need(len({a["approved"], a["w0_accepted"], w["accepted"], session["csv_approved"],
              session["termination_approved"], service["approved"], v["permits"]["approved"]}) == 1, "approval_mismatch")
    need(type(a["max_creates"]) is int and a["max_creates"] == 1 and life["slot"] == "A")
    need(pre["plan"] == plan and plan["candidate_sha256"] == pre["candidate_sha256"] == launch["candidate_sha256"])
    need(plan["request_sha256"] == sha(raw["request"]), "request_digest")
    req = v["request"]
    need(req["templateID"] == plan["template_id"] and req["metadata"]["agentseek_run_id"] == plan["run_id"]
         and req["metadata"]["agentseek_create_token"] == plan["create_token"] and req["network"] == {"allowPublicTraffic": False})
    for key in ("run_id", "boot_id", "candidate_sha256"):
        need(w[key] == plan[key], "window_binding")
    need(w["revoked"] is False and w["starts_epoch"] < w["ends_epoch"] == a["expires_epoch"] == session["expires_epoch"])
    writers = w["writers"]
    need(type(writers) is list and bool(writers))
    ids = [x["id"] for x in writers]
    need(len(ids) == len(set(ids)) and w["creator_id"] not in ids)
    canonical_ids = json.dumps(sorted(ids), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    need(w["inventory_sha256"] == sha(canonical_ids), "inventory_digest")
    for writer in writers:
        need(set(writer) == {"id", "abstain", "confirmed_epoch", "responsible"})
        need(writer["abstain"] is True and type(writer["confirmed_epoch"]) in (int, float)
             and writer["confirmed_epoch"] <= w["starts_epoch"], "writer_confirmation")
    def file_ref(doc, key, target, pin=None):
        need(doc[key] == str(p[target]), "reference_mismatch")
        if pin:
            need(doc[pin] == sha(raw[target]), "reference_digest")
    file_ref(pre, "approval_file", "approval")
    file_ref(pre, "api_key_file", "api_key")
    file_ref(pre, "ca_file", "ca")
    window = pre["exclusive_window"]
    need(set(window) == {"path", "digest", "creator_id", "writers"})
    file_ref(window, "path", "window", "digest")
    need(window["creator_id"] == w["creator_id"] and sorted(window["writers"]) == sorted(ids))
    file_ref(ins, "precreate_file", "precreate", "precreate_sha256")
    file_ref(ins, "request_file", "request")
    file_ref(ins, "receipt_key_file", "receipt_key", "receipt_key_sha256")
    for key, target in (("approval_sha256", "approval"), ("api_key_sha256", "api_key"), ("ca_sha256", "ca")):
        need(ins[key] == sha(raw[target]), "reference_digest")
    sequence = ins["batch_sequence"]
    need(type(sequence) is list and len(sequence) == 2 and sequence[0] == plan, "batch")
    need(set(sequence[1]) == set(plan), "batch")
    need(sequence[1]["create_token"] != plan["create_token"], "batch")
    for key in set(plan) - {"create_token", "request_sha256"}:
        need(sequence[1][key] == plan[key], "batch")
    file_ref(launch, "create_file", "installation", "create_sha256")
    need(launch["supervisor_script"] == identities["script"]["path"]
         and launch["supervisor_sha256"] == identities["script"]["sha256"], "script_binding")
    file_ref(life, "launcher", "launcher", "launcher_sha256")
    binding = {k: val for k, val in plan.items() if k != "endpoint"}
    binding.update(approval_sha256=sha(raw["approval"]), intent_sha256="0" * 64)
    need(session["expected_create"] == binding, "binding")
    file_ref(session, "vault_key_file", "receipt_key")
    need(session["supervisor_identity"] == pre["supervisor_identity"], "pins")
    pins = pre["supervisor_identity"]
    need(set(pins) == {"executable", "executable_sha256", "script", "script_sha256", "unit_file",
                       "unit_sha256", "cmdline_sha256", "unit"}, "pins")
    for name, pin in (("executable", "executable_sha256"), ("script", "script_sha256"), ("unit_file", "unit_sha256")):
        need(pins[name] == identities[name]["path"] and pins[pin] == identities[name]["sha256"], "pins")
    digest(pins["cmdline_sha256"])
    need(type(pins["unit"]) is str and len(pins["unit"]) <= 255
         and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.service", pins["unit"]) is not None, "pins")
    control = session["control"]
    need(set(control) == {"endpoint", "api_key", "ca_file", "domain", "proxy_port"})
    need(type(control["proxy_port"]) is int and control["proxy_port"] == 80, "proxy_port")
    need(control["api_key"].encode("ascii") == raw["api_key"] and control["endpoint"] == plan["endpoint"]
         and control["domain"] == plan["domain"], "control")
    file_ref(control, "ca_file", "ca")
    for doc, key, target in ((pre, "supervisor_directory", "supervisor"), (session, "supervisor_directory", "supervisor"),
             (session, "vault_directory", "vault"), (ins, "vault_directory", "vault"), (ins, "fence_directory", "fence"),
             (ins, "quota_directory", "quota"), (launch, "tracking_directory", "tracking"), (life, "directory", "lifecycle"),
             (service, "store_directory", "store"), (service, "workspace_directory", "workspace")):
        need(doc[key] == str(dirs[target]), "directory_reference")
    file_ref(service, "certificate_file", "certificate")
    file_ref(service, "private_key_file", "private_key")
    file_ref(service, "permits_file", "permits", "permits_sha256")
    need(service["bind_host"] == service["approved_bind_host"])
    permits, plans = v["permits"]["permits"], service["plans"]
    need(type(permits) is list and len(permits) == 1 and type(plans) is list and len(plans) == 1)
    permit, entry = permits[0], plans[0]
    need(set(permit) == {"request", "input_sha256", "principal_sha256", "expires_epoch"})
    need(set(entry) == {"owner_id", "request_id", "lifecycle_file", "lifecycle_sha256", "business_file", "business_sha256"})
    need(set(permit["request"]) == {"owner_id", "request_id", "input_ref", "instruction"})
    for key in ("request_id", "owner_id"):
        need(entry[key] == permit["request"][key], "business_binding")
    need(permit["principal_sha256"] == sha(raw["broker_token"]) and permit["input_sha256"] == session["input_sha256"]
         and permit["expires_epoch"] == session["expires_epoch"], "permit_binding")
    need(session["instruction_sha256"] == sha(permit["request"]["instruction"].encode()), "instruction_digest")
    file_ref(entry, "lifecycle_file", "lifecycle", "lifecycle_sha256")
    file_ref(entry, "business_file", "session", "business_sha256")


def run(path, pinned_digest, *, verify_only=False):
    spec_raw = read_private(absolute(str(path)))
    need(sha(spec_raw) == digest(pinned_digest), "input_digest")
    root, paths, expected = compile_materials(decode(spec_raw))
    # Bytes and expected hashes above derive from pinned inputs, NOT destination.
    if not verify_only:
        root.mkdir(mode=0o700, exist_ok=False)
        for name, raw in expected.items():
            write_new(paths[name], raw)
    private_directory(root)
    need(set(root.iterdir()) == set(paths.values()), "output_inventory")
    for name, target in paths.items():
        actual = read_private(target)
        need(sha(actual) == sha(expected[name]) and actual == expected[name], "output_digest")
    if not verify_only:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    # Re-read input and controlled sources too; never report success after drift.
    need(read_private(absolute(str(path))) == spec_raw, "input_changed")
    _, _, again = compile_materials(decode(spec_raw))
    need(again == expected, "source_changed")
    return {"status": "MATERIALS_READBACK_VERIFIED", "files": len(paths),
            "service_sha256": sha(expected["service"]), "execution_authorized": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args.input, args.sha256, verify_only=args.verify_only)
    except Exception as exc:
        code = str(exc) if type(exc) is MaterialError else "io_or_contract"
        print(json.dumps({"status": "BLOCKED", "error": code, "execution_authorized": False}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
