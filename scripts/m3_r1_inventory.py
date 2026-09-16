"""Build a static R1 source/test inventory; optionally stage a fresh test tree.

Does not import candidate code or contact CubeSandbox. Static imports are followed;
runtime-generated imports and non-Python assets require separate review.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

PACKAGE = Path("contrib/agentseek-execution/src/agentseek_execution")
TESTS = Path("contrib/agentseek-execution/tests")
EXAMPLE = Path("examples/enterprise_wecom_digital_employee")
ENTRIES = ("m3_create_process", "m3_case_process", "m3_precreate_process", "m3_receipt_probe")
EXTRA_TESTS = ("test_m3_batch_integration", "test_m3_history_installation",
               "test_m3_create_crash", "test_m3_fence_handoff", "test_m3_supervisor_process",
               "test_m3_installation_sequence")


def imports(tree, package):
    """Yield (module, optional symbol); missing explicit local modules are errors."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, None
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[:len(parts) - node.level + 1])
                base = ".".join(filter(None, (base, node.module)))
            else:
                base = node.module or ""
            for alias in node.names:
                yield base, alias.name


def dependencies(tree, package, modules, external):
    for base, symbol in imports(tree, package):
        if base in modules:
            yield base
        elif base.startswith(("agentseek_execution", "sandbox_poc", "test_")):
            message = f"missing local module: {base}"
            raise ValueError(message)
        else:
            external.add(base.split(".")[0])
        candidate = f"{base}.{symbol}"
        if symbol and candidate in modules:
            yield candidate


def closure(root, modules, seeds, external):
    selected = set()
    pending = list(seeds)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        if name not in modules:
            message = f"missing local module: {name}"
            raise ValueError(message)
        selected.add(name)
        path = modules[name]
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        if package in modules:
            pending.append(package)
        tree = ast.parse((root / path).read_text(encoding="utf-8"))
        pending.extend(dependencies(tree, package, modules, external))
    return selected


def inventory(root: Path) -> dict:
    modules = {}
    for prefix, directory in (("agentseek_execution", PACKAGE), ("", TESTS),
                              ("sandbox_poc", EXAMPLE / "sandbox_poc")):
        for path in (root / directory).glob("*.py"):
            name = prefix if path.stem == "__init__" else ".".join(filter(None, (prefix, path.stem)))
            modules[name] = path.relative_to(root)
    modules["test_node_supervisor"] = EXAMPLE / "tests/test_node_supervisor.py"
    external = set()

    runtime = closure(root, modules, [f"agentseek_execution.{name}" for name in ENTRIES], external)
    tests = {"test_" + name.rsplit(".", 1)[-1] for name in runtime}
    tests = (tests & modules.keys()) | set(EXTRA_TESTS) | {"test_node_supervisor"}
    if "conftest" in modules:
        tests.add("conftest")
    complete = closure(root, modules, runtime | tests, external)
    paths = sorted({modules[name] for name in complete})
    files = [{"path": path.as_posix(), "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}
             for path in paths]
    return {"schema": 1, "scope": "static R1 source/test closure, not a release approval",
            "entry_modules": list(ENTRIES), "runtime_modules": sorted(runtime),
            "test_paths": sorted(modules[name].as_posix() for name in complete if name.startswith("test_")),
            "import_roots": sorted(external - {"", "__future__"}), "files": files,
            "limitations": ["No dependency version lock or installable wheel is produced.",
                            "Dynamic imports, subprocess installation and non-Python assets need review."]}


def stage(root: Path, destination: Path, manifest: dict) -> None:
    # Never merge with, erase or overwrite an existing directory.
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    for item in manifest["files"]:
        source = root / item["path"]
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            message = "source changed after inventory"
            raise ValueError(message)
        target = destination / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    # Only a generated inventory is added, not working-tree configuration or secrets.
    (destination / "R1_INVENTORY.json").write_text(json.dumps(manifest, indent=2) + "\n")


def stage_package(root: Path, destination: Path, manifest: dict) -> None:
    """Stage only runtime modules into a separate wheel project, never production."""
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    expected = {item["path"]: item["sha256"] for item in manifest["files"]}
    for module in manifest["runtime_modules"]:
        relative = PACKAGE / (module.split(".")[-1] + ".py" if "." in module else "__init__.py")
        data = (root / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected[relative.as_posix()]:
            message = "source changed after inventory"
            raise ValueError(message)
        target = destination / "src/agentseek_execution" / relative.name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    (destination / "pyproject.toml").write_bytes((root / "scripts/m3_r1_package.toml").read_bytes())
    (destination / "R1_INVENTORY.json").write_text(json.dumps(manifest, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--stage", type=Path, help="New, non-existing directory; never overwritten")
    parser.add_argument("--package-stage", type=Path, help="New runtime-only wheel project directory")
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = inventory(root)
    if args.stage:
        stage(root, args.stage, manifest)
    if args.package_stage:
        stage_package(root, args.package_stage, manifest)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
