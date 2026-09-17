"""Read-only installed-wheel smoke check. Run with the new venv's python -I.

Imports hash-matched modules and invokes fixed CLI entrypoints with invalid input.
Never supplies a valid installation, credential, endpoint or create request.
"""

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path


def stop(message):
    raise SystemExit(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    args = parser.parse_args()
    if not sys.flags.isolated:
        stop("requires python -I")
    manifest = json.loads(args.inventory.read_bytes())
    prefix = "contrib/agentseek-execution/src/agentseek_execution/"
    hashes = {item["path"].removeprefix(prefix): item["sha256"] for item in manifest["files"]
              if item["path"].startswith(prefix)}
    installation = Path(sys.prefix).resolve()
    for name in manifest["runtime_modules"]:
        if name != "agentseek_execution" and not name.startswith("agentseek_execution."):
            stop("unexpected module namespace")
        module = importlib.import_module(name)
        source = Path(module.__file__).resolve()
        if not source.is_relative_to(installation) or hashlib.sha256(source.read_bytes()).hexdigest() != hashes[source.name]:
            stop("installed source mismatch")
    # Fixed entries only: never take an executable/module name from the manifest.
    entries = ("m3_create_process", "m3_case_process", "m3_precreate_process", "m3_receipt_probe", "m3_launcher",
               "m3_slot", "m3_lifecycle", "m3_materialize")
    for name in entries:
        result = subprocess.run(  # noqa: S603 -- fixed interpreter and module allowlist
            [sys.executable, "-I", "-m", f"agentseek_execution.{name}"], input=b"{}",
            capture_output=True, env={}, timeout=5)
        if result.returncode != 2 or result.stdout or result.stderr:
            stop(f"unexpected rejection contract: {name}")
    print(json.dumps({"isolated": True, "installed_modules_verified": len(manifest["runtime_modules"]),
                      "invalid_input_rejected": len(entries),
                      "distributions": sorted(f"{d.metadata['Name']}=={d.version}"
                                              for d in importlib.metadata.distributions())}, indent=2))


if __name__ == "__main__":
    main()
