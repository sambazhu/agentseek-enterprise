"""One-shot A create/materialize/slot caller; closeout remains explicit.

No retries, polling, termination, approval generation or automatic successor.
Existing lifecycle, receipt and slot gates remain authoritative.
"""

import argparse
import os
import sys
import time
from dataclasses import asdict

from .m3_create_process import _path, _pinned
from .m3_create_worker import CreatePlan
from .m3_lifecycle import execute
from .m3_materialize import materialize
from .m3_probe_dispatch import DispatchDirectory
from .m3_probe_process import _decode, config_bytes
from .m3_slot import _save, run as run_slot
from .models import Code, canonical, require


def _binding_check(life, permit):
    launcher = _decode(_pinned(_path(life["launcher"]), life["launcher_sha256"]))
    installed = _decode(_pinned(_path(launcher["create_file"]), launcher["create_sha256"]))
    require(installed["schema"] == 2, Code.DENIED)
    precreate = _decode(_pinned(_path(installed["precreate_file"]), installed["precreate_sha256"]))
    plan = CreatePlan(**precreate["plan"])
    require(permit["expected_create"] == asdict(plan.binding(installed["approval_sha256"])), Code.DENIED)
    require(permit["installation"]["plan"] == precreate["plan"], Code.DENIED)
    for key in ("supervisor_directory", "supervisor_identity", "exclusive_window", "template_pins"):
        require(permit["installation"][key] == precreate[key], Code.DENIED)
    require(_path(permit["installation"]["create_request_file"]) == _path(installed["request_file"]), Code.DENIED)
    source = permit["receipt_source"]
    require(_path(source["vault_directory"]) == _path(installed["vault_directory"])
            and _path(source["vault_key_file"]) == _path(installed["receipt_key_file"])
            and _path(source["api_key_file"]) == _path(precreate["api_key_file"]), Code.DENIED)


def run(path, digest):
    require(sys.platform == "linux" and os.getuid() == os.geteuid() == 0, Code.DENIED)
    raw = _pinned(path, digest)
    config = _decode(raw)
    require(set(config) == {"schema", "lifecycle", "lifecycle_sha256", "preapproval",
                            "preapproval_sha256", "directory"}, Code.DENIED)
    require(type(config["schema"]) is int and config["schema"] == 1, Code.DENIED)
    life_path, permit_path = _path(config["lifecycle"]), _path(config["preapproval"])
    life_raw = _pinned(life_path, config["lifecycle_sha256"])
    permit_raw = _pinned(permit_path, config["preapproval_sha256"])
    life, permit = _decode(life_raw), _decode(permit_raw)
    require(life["slot"] == permit["slot"] == "A", Code.DENIED)
    require(permit["approved"] is True and permit["materialize_exact_rows"] is True, Code.DENIED)
    _binding_check(life, permit)
    life_dir = _path(life["directory"])
    output = _path(permit["output_directory"])
    dispatch = _path(permit["dispatch_directory"])
    directory = _path(config["directory"])
    require(_path(permit["create_result_file"]) == life_dir / "create-result.json", Code.DENIED)
    # Reject aliasing and reused state before a guest can be created.
    directories = (directory, life_dir, output, dispatch)
    require(len(set(directories)) == 4, Code.DENIED)
    require(all(a not in b.parents for a in directories for b in directories if a != b), Code.DENIED)
    for item in directories:
        fd = DispatchDirectory(item)._open()
        try:
            require(not os.listdir(fd), Code.DENIED)
        finally:
            os.close(fd)
    fd = DispatchDirectory(directory)._open()
    started = time.monotonic()
    timings = {}

    def pinned():
        require(_pinned(path, digest) == raw
                and _pinned(life_path, config["lifecycle_sha256"]) == life_raw
                and _pinned(permit_path, config["preapproval_sha256"]) == permit_raw, Code.UNKNOWN)

    try:
        _save(fd, "continuous-intent.json", {"schema": 1, "config_sha256": digest})
        pinned()
        created = execute(life_path, config["lifecycle_sha256"], "create")
        require(created["saved"] is True and created["next_create_authorized"] is False, Code.UNKNOWN)
        timings["create_elapsed_s"] = time.monotonic() - started
        pinned()
        before = time.monotonic()
        generated = materialize(permit_path, config["preapproval_sha256"])
        require(_decode(config_bytes(output / "materialize-result.json")) == generated, Code.UNKNOWN)
        require(generated["preapproval_sha256"] == config["preapproval_sha256"]
                and generated["rows"] == 5 and generated["next_create_authorized"] is False
                and _path(generated["slot_file"]) == output / "slot.json", Code.UNKNOWN)
        slot_path = _path(generated["slot_file"])
        _pinned(slot_path, generated["slot_sha256"])
        timings["materialize_elapsed_s"] = time.monotonic() - before
        pinned()
        before = time.monotonic()
        result = run_slot(slot_path, generated["slot_sha256"])
        require(result["slot"] == "A" and result["rows_observed"] == 5
                and result["next_create_authorized"] is False, Code.UNKNOWN)
        timings["slot_elapsed_s"] = time.monotonic() - before
        summary = dict(schema=1, config_sha256=digest, rows_observed=5,
                       next_create_authorized=False, closeout_required=True, timings=timings)
        _save(fd, "continuous-result.json", summary)
        return summary
    finally:
        # On any exception the durable intent remains; no retry or cleanup.
        os.close(fd)


def main():
    try:
        require(sys.flags.isolated, Code.DENIED)
        if not sys.argv[1:]:
            sys.exit(2)
        parser = argparse.ArgumentParser()
        parser.add_argument("--config", required=True)
        parser.add_argument("--sha256", required=True)
        args = parser.parse_args()
        print(canonical(run(_path(args.config), args.sha256)))
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
