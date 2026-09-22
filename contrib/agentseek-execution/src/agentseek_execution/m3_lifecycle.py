"""Explicit create/save and closeout phases; never generates approvals or loops.

Slot configuration is provisioned after the sealed response, independently
approved, and sent to m3_slot. This module never treats closeout as B approval.
"""

import argparse
import os
import sys
from dataclasses import asdict

from .m3_create_process import _path, _pinned
from .m3_create_receipt import CreateBinding
from .m3_launcher import launch, launch_successor, check_closed
from .m3_probe_dispatch import DispatchDirectory
from .m3_probe_process import _decode, config_bytes
from .m3_slot import _save
from .models import Code, canonical, require
from .execution_diagnostic import diagnosed, phase


@diagnosed("lifecycle")
def execute(path, digest, action):
    require(sys.platform == "linux" and os.getuid() == os.geteuid() == 0, Code.DENIED)
    config = _decode(_pinned(path, digest))
    base = {"schema", "slot", "launcher", "launcher_sha256", "directory"}
    require(set(config) == (base | {"previous"} if config.get("slot") == "B" else base), Code.DENIED)
    require(type(config["schema"]) is int and config["schema"] == 1
            and config["slot"] in {"A", "B"} and action in {"create", "closeout"}, Code.DENIED)
    directory = _path(config["directory"])
    launcher = _path(config["launcher"])
    launcher_sha = config["launcher_sha256"]
    launch_config = _decode(_pinned(launcher, launcher_sha))
    installation = _decode(_pinned(_path(launch_config["create_file"]), launch_config["create_sha256"]))
    require(type(installation["schema"]) is int
            and installation["schema"] == (2 if config["slot"] == "A" else 3), Code.DENIED)
    fd = DispatchDirectory(directory)._open()
    try:
        if action == "closeout":
            saved = _decode(config_bytes(directory / "create-result.json"))
            require(saved["config_sha256"] == digest, Code.DENIED)
            binding = CreateBinding(**saved["result"]["binding"])
            result = check_closed(launcher, launcher_sha, binding)
            _save(fd, "closeout-result.json", {"schema": 1, "config_sha256": digest, "result": result})
            return result
        previous = None
        if config["slot"] == "B":
            previous = config["previous"]
            require(type(previous) is dict and set(previous) == {
                "launcher", "launcher_sha256", "create_result", "create_result_sha256",
                "slot_result", "slot_result_sha256"}, Code.DENIED)
            created = _decode(_pinned(_path(previous["create_result"]), previous["create_result_sha256"]))
            completed = _decode(_pinned(_path(previous["slot_result"]), previous["slot_result_sha256"]))
            binding = CreateBinding(**created["result"]["binding"])
            binding.validate()
            require(created["result"]["registered_and_observed"] is True
                    and completed["slot"] == "A" and completed["rows_observed"] == 5
                    and completed["next_create_authorized"] is False
                    and completed["create"] == asdict(binding), Code.DENIED)
        with phase("intent_save"):
            _save(fd, "create-intent.json", {"schema": 1, "config_sha256": digest})
        if previous is None:
            result = launch(launcher, launcher_sha)
        else:
            result = launch_successor(launcher, launcher_sha, _path(previous["launcher"]),
                                      previous["launcher_sha256"], binding)
        require(_decode(_pinned(path, digest)) == config, Code.UNKNOWN)
        with phase("result_save"):
            _save(fd, "create-result.json", {"schema": 1, "config_sha256": digest, "result": result})
        return {"schema": 1, "saved": True, "next_create_authorized": False}
    finally:
        os.close(fd)


def main():
    try:
        require(sys.flags.isolated, Code.DENIED)
        if not sys.argv[1:]:
            sys.exit(2)
        parser = argparse.ArgumentParser()
        parser.add_argument("--config", required=True)
        parser.add_argument("--sha256", required=True)
        parser.add_argument("--action", choices=("create", "closeout"), required=True)
        args = parser.parse_args()
        print(canonical(execute(_path(args.config), args.sha256, args.action)))
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
