"""Stage a separate node-only CSV business candidate; does not enable a service."""

import argparse
import json
from pathlib import Path

from m3_r1_inventory import inventory, stage, stage_package

ENTRIES = ("business_service", "business_cube_session")


def business_inventory(root):
    return inventory(root, entries=ENTRIES, extra_tests=("test_business_execution", "test_business_http"),
                     scope="CSV business node closure; not R1 matrix approval or gateway package")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--package-stage", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = business_inventory(root)
    if args.stage:
        stage(root, args.stage, manifest)
    if args.package_stage:
        stage_package(root, args.package_stage, manifest, template=Path("scripts/business_package.toml"))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
