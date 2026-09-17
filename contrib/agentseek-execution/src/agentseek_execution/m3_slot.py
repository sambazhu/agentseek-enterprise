"""Explicit, pinned post-create slot driver. No create, kill or automatic B.

Receipt provisioning and approval remain separate trusted installation steps.
One durable slot intent prevents resumption after failure, crash or UNKNOWN.
This entry is not yet the create-to-closeout installation orchestrator.
"""

import argparse
import hashlib
import math
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .m3_case_process import dispatch_isolated
from .m3_create_process import _path, _pinned
from .m3_evidence_process import collect_gate_isolated
from .m3_probe_dispatch import Binding, DispatchDirectory, _validate_result
from .m3_probe_process import _decode
from .m3_receipt_probe import ReceiptProbeSource
from .models import Code, canonical, require

MATRIX = (("E1", "correct"), ("E2", "correct"), ("E2", "wrong"),
          ("E3", "correct"), ("E3", "missing"))


def _save(fd, name, value):
    output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        data = canonical(value).encode()
        while data:
            count = os.write(output, data)
            require(count > 0, Code.UNKNOWN)
            data = data[count:]
        os.fsync(output)
        os.fsync(fd)
    finally:
        os.close(output)


def _accepted(observation, endpoint, state):
    _validate_result(observation)
    if not observation["complete"]:
        return False
    signal = observation["protocol"]
    if state == "correct":
        return (observation["status"] == 200 and signal["kind"] ==
                {"E1": "command_success", "E2": "file_payload", "E3": "stat_payload"}[endpoint])
    return (observation["status"] == 403 and signal["kind"] == "http_denial_signal"
            and signal["activity"] is False)


def run(path: Path, digest: str):
    """Installed root caller supplies an independently pinned slot configuration.

    Explicit positive reserve has no default: its operational approval and
    measurement remain prerequisites, not facts inferred by this driver.
    """
    require(sys.platform == "linux" and os.getuid() == os.geteuid() == 0, Code.DENIED)
    config = _decode(_pinned(path, digest))
    fields = {"schema", "slot", "reserve_seconds", "directory", "rows"}
    require(set(config) == (fields | {"previous_slot"} if config.get("slot") == "B" else fields), Code.DENIED)
    require(type(config["schema"]) is int and config["schema"] == 1, Code.DENIED)
    require(config["slot"] in {"A", "B"}, Code.DENIED)
    reserve = config["reserve_seconds"]
    require(type(reserve) in {int, float} and math.isfinite(reserve) and 0 < reserve < 120, Code.DENIED)
    matrix = MATRIX + ((("E1", "cross_guest"),) if config["slot"] == "B" else ())
    require(type(config["rows"]) is list and len(config["rows"]) == len(matrix), Code.DENIED)
    prepared = []
    identity = None
    seen = set()
    for row, selectors in zip(config["rows"], matrix):
        require(type(row) is dict and set(row) == {"installation", "sha256", "approval_sha256"}, Code.DENIED)
        installed = _decode(_pinned(_path(row["installation"]), row["sha256"]))
        require(type(installed["schema"]) is int and installed["schema"] == 4, Code.DENIED)
        binding = Binding(**installed["binding"])
        binding.validate()
        source = ReceiptProbeSource.from_wire(installed["receipt_source"])
        probe = _decode(_pinned(_path(installed["probe_config"]), binding.config_sha256))
        require((probe["endpoint"], probe["state"]) == selectors and probe["traffic_alias"] == "e2b", Code.DENIED)
        require((source.donor is not None) == (selectors[1] == "cross_guest"), Code.DENIED)
        current = (asdict(source.create), binding.sandbox_id, binding.artifact_sha256,
                   str(source.vault_directory), str(source.vault_key_file), str(source.api_key_file))
        require(identity is None or current == identity, Code.DENIED)
        identity = current
        require(binding.case_id not in seen, Code.DENIED)
        seen.add(binding.case_id)
        prepared.append((row, binding, selectors))
    if config["slot"] == "B":
        previous = config["previous_slot"]
        require(type(previous) is dict and set(previous) == {"path", "sha256"}, Code.DENIED)
        record = _decode(_pinned(_path(previous["path"]), previous["sha256"]))
        require(set(record) == {"schema", "slot", "rows_observed", "next_create_authorized",
                                "config_sha256", "create"}, Code.DENIED)
        require(record["schema"] == 1 and record["slot"] == "A" and record["rows_observed"] == 5
                and record["next_create_authorized"] is False, Code.DENIED)
        require(record["create"] == source.donor["create"], Code.DENIED)
    directory = _path(config["directory"])
    fd = DispatchDirectory(directory)._open()
    prefix = hashlib.sha256(canonical([identity[0], config["slot"]]).encode()).hexdigest()
    try:
        # Burns the whole slot before any live call; repeated invocation cannot
        # skip a failed positive, a lost output, or an earlier incomplete row.
        _save(fd, prefix + ".slot-intent", {"schema": 1, "config_sha256": digest})
        first, binding, _ = prepared[0]
        evidence = collect_gate_isolated(_path(first["installation"]), installation_digest=first["sha256"],
                                         approval_digest=first["approval_sha256"], binding=binding)
        deadline = evidence.verify(binding, time.monotonic())
        require(deadline - time.monotonic() >= len(prepared) * 16 + reserve, Code.DENIED)
        for index, (row, binding, selectors) in enumerate(prepared):
            require(_decode(_pinned(path, digest)) == config, Code.DENIED)
            # One fixed cutoff, including setup/persistence elapsed time.
            require(deadline - time.monotonic() >= (len(prepared) - index) * 16 + reserve, Code.DENIED)
            observation = dispatch_isolated(_path(row["installation"]), directory,
                                             installation_digest=row["sha256"],
                                             approval_digest=row["approval_sha256"], deadline=deadline - reserve)
            _save(fd, prefix + f".row-{index}", observation)
            require(_accepted(observation, *selectors), Code.DENIED)
        result = {"schema": 1, "slot": config["slot"], "rows_observed": len(prepared),
                  "next_create_authorized": False, "config_sha256": digest, "create": identity[0]}
        _save(fd, prefix + ".slot-result", result)
        return result
    finally:
        os.close(fd)


def main():
    try:
        require(sys.flags.isolated, Code.DENIED)
        parser = argparse.ArgumentParser()
        parser.add_argument("--config", required=True)
        parser.add_argument("--sha256", required=True)
        # Keep the standard invalid-input rejection silent.
        if not sys.argv[1:]:
            sys.exit(2)
        args = parser.parse_args()
        print(canonical(run(_path(args.config), args.sha256)))
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
