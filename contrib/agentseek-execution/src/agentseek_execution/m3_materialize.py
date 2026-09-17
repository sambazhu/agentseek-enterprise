"""Pinned preapproval -> exact receipt-bound row files. No network or execution.

This is explicit delegated approval materialization, not approval invention.
Root installation pins the preapproval out of band. Only sandbox/intent IDs and
derived hashes come from the authenticated receipt; no arbitrary substitution.
Partial output burns the directory and is never resumed or overwritten.
"""

import argparse
import hashlib
import math
import os
import sys
import time
from dataclasses import asdict, replace

from .m3_create_process import _path, _pinned
from .m3_create_receipt import CreateBinding, CreateReceiptVault
from .m3_create_worker import CreatePlan
from .m3_probe_dispatch import Binding, DispatchDirectory
from .m3_probe_process import _decode, config_bytes
from .m3_receipt_probe import ReceiptProbeSource, donor_receipt
from .m3_slot import MATRIX, _save
from .m3_supervisor_snapshot import system_clock
from .m3_template_evidence import TemplatePins
from .models import Code, canonical, require


def _sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def materialize(path, digest):
    require(sys.platform == "linux" and os.getuid() == os.geteuid() == 0, Code.DENIED)
    raw = _pinned(path, digest)
    permit = _decode(raw)
    fields = {"schema", "approved", "materialize_exact_rows", "expires_epoch", "slot",
              "reserve_seconds", "expected_create", "create_result_file", "receipt_source",
              "installation", "output_directory", "dispatch_directory", "case_ids", "approval_refs"}
    require(set(permit) == (fields | {"previous_slot", "donor"} if permit.get("slot") == "B" else fields), Code.DENIED)
    require(type(permit["schema"]) is int and permit["schema"] == 1
            and permit["approved"] is True and permit["materialize_exact_rows"] is True, Code.DENIED)
    require(permit["slot"] in {"A", "B"}, Code.DENIED)
    expiry, reserve = permit["expires_epoch"], permit["reserve_seconds"]
    require(type(expiry) in {int, float} and math.isfinite(expiry) and expiry - time.time() >= 16, Code.DENIED)
    require(type(reserve) in {int, float} and math.isfinite(reserve) and 0 < reserve < 120, Code.DENIED)
    expected = CreateBinding(**permit["expected_create"])
    expected.validate()
    require(expected.intent_sha256 == "0" * 64 and expected.restricted is True, Code.DENIED)
    require(system_clock().boot_id == expected.boot_id, Code.DENIED)
    saved_raw = config_bytes(_path(permit["create_result_file"]))
    saved = _decode(saved_raw)
    require(set(saved) == {"schema", "config_sha256", "result"} and saved["schema"] == 1, Code.DENIED)
    result = saved["result"]
    require(set(result) == {"schema", "binding", "registered_and_observed"}
            and result["schema"] == 1 and result["registered_and_observed"] is True, Code.DENIED)
    created = CreateBinding(**result["binding"])
    created.validate()
    require(created.intent_sha256 != "0" * 64 and replace(created, intent_sha256="0" * 64) == expected, Code.DENIED)
    source_fields = permit["receipt_source"]
    require(type(source_fields) is dict and set(source_fields) == {
        "vault_directory", "vault_key_file", "api_key_file", "domain", "proxy_port"}, Code.DENIED)
    source = ReceiptProbeSource.from_wire(dict(source_fields, create=asdict(created)))
    key = config_bytes(source.vault_key_file)
    vault = CreateReceiptVault(source.vault_directory, key)
    vault.read_intent(created)
    receipt = vault.read(created)
    require(receipt.domain == source.domain == expected.domain and receipt.template_id == expected.template_id, Code.DENIED)
    base = permit["installation"]
    require(type(base) is dict and set(base) == {
        "supervisor_directory", "supervisor_identity", "control", "plan", "template_pins",
        "exclusive_window", "create_request_file"}, Code.DENIED)
    plan = CreatePlan(**base["plan"])
    require(plan.binding(expected.approval_sha256) == expected, Code.DENIED)
    pins = TemplatePins(**base["template_pins"])
    pins.validate()
    require(pins.template_id == expected.template_id, Code.DENIED)
    require(base["control"]["endpoint"] == plan.endpoint
            and base["control"]["domain"] == source.domain
            and base["control"]["proxy_port"] == source.proxy_port
            and base["control"]["api_key"] == config_bytes(source.api_key_file).decode("ascii"), Code.DENIED)
    matrix = MATRIX + ((("E1", "cross_guest"),) if permit["slot"] == "B" else ())
    for name in ("case_ids", "approval_refs"):
        require(type(permit[name]) is list and len(permit[name]) == len(matrix)
                and all(type(v) is str for v in permit[name])
                and len(set(permit[name])) == len(matrix), Code.DENIED)
    cross_source = None
    if permit["slot"] == "B":
        previous = permit["previous_slot"]
        require(type(previous) is dict and set(previous) == {"path", "sha256"}, Code.DENIED)
        old = _decode(_pinned(_path(previous["path"]), previous["sha256"]))
        require(old["slot"] == "A" and old["rows_observed"] == 5
                and old["next_create_authorized"] is False
                and old["create"] == permit["donor"]["create"], Code.DENIED)
        cross_source = replace(source, donor=permit["donor"])
        donor_receipt(cross_source, vault)
    output = _path(permit["output_directory"])
    dispatch = _path(permit["dispatch_directory"])
    require(output != dispatch and output not in {source.vault_directory, source.vault_key_file.parent}, Code.DENIED)
    check = DispatchDirectory(dispatch)._open()
    os.close(check)
    fd = DispatchDirectory(output)._open()
    try:
        require(not os.listdir(fd), Code.DENIED)
        _save(fd, "materialize-intent.json", {"schema": 1, "preapproval_sha256": digest})
        rows = []
        for index, (endpoint, state) in enumerate(matrix):
            current = cross_source if state == "cross_guest" else source
            probe = dict(schema=4 if state == "cross_guest" else 3, receipt_ref=source.reference(),
                         endpoint=endpoint, layer="traffic", state=state, traffic_alias="e2b")
            if state == "cross_guest":
                probe["donor_ref"] = _sha(permit["donor"])
            binding = Binding(created.run_id, created.create_token, receipt.sandbox_id, created.template_id,
                              pins.artifact_sha256, permit["case_ids"][index], _sha(probe),
                              permit["approval_refs"][index], created.candidate_sha256, created.boot_id)
            binding.validate()
            approval = dict(schema=1, approved=True, binding=asdict(binding), expires_epoch=expiry)
            installed = dict(base, schema=4, binding=asdict(binding), receipt_source=current.wire(),
                             probe_config=str(output / f"probe-{index}.json"),
                             approval_file=str(output / f"approval-{index}.json"))
            for label, value in (("probe", probe), ("approval", approval), ("installation", installed)):
                _save(fd, f"{label}-{index}.json", value)
            rows.append(dict(installation=str(output / f"installation-{index}.json"),
                             sha256=_sha(installed), approval_sha256=_sha(approval)))
        slot = dict(schema=1, slot=permit["slot"], reserve_seconds=reserve, directory=str(dispatch), rows=rows)
        if permit["slot"] == "B":
            slot["previous_slot"] = permit["previous_slot"]
        require(_pinned(path, digest) == raw and config_bytes(_path(permit["create_result_file"])) == saved_raw
                and config_bytes(source.vault_key_file) == key and vault.read(created) == receipt
                and expiry - time.time() >= 16, Code.DENIED)
        _save(fd, "slot.json", slot)
        manifest = dict(schema=1, preapproval_sha256=digest, slot_file=str(output / "slot.json"),
                        slot_sha256=_sha(slot), rows=len(rows), next_create_authorized=False)
        _save(fd, "materialize-result.json", manifest)
        return manifest
    finally:
        os.close(fd)


def main():
    try:
        require(sys.flags.isolated, Code.DENIED)
        if not sys.argv[1:]:
            sys.exit(2)
        parser = argparse.ArgumentParser()
        parser.add_argument("--preapproval", required=True)
        parser.add_argument("--sha256", required=True)
        args = parser.parse_args()
        print(canonical(materialize(_path(args.preapproval), args.sha256)))
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
