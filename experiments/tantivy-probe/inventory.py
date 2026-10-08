#!/usr/bin/env python3
"""Record the isolated Linux dependency selection without changing the core graph."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib


def inventory(root):
    metadata = json.loads(subprocess.check_output([
        "cargo", "metadata", "--locked", "--manifest-path", str(root / "Cargo.toml"),
        "--filter-platform", "x86_64-unknown-linux-gnu", "--format-version", "1",
    ], text=True))
    lock = tomllib.loads((root / "Cargo.lock").read_text())
    checksums = {(p["name"], p["version"]): p.get("checksum") for p in lock["package"]}
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    packages = []
    for package in metadata["packages"]:
        node = nodes.get(package["id"])
        if node is None:
            continue
        packages.append({
            "name": package["name"], "version": package["version"],
            "source": package["source"],
            "checksum": checksums.get((package["name"], package["version"])),
            "license": package["license"],
            "license_file": Path(package["license_file"]).name if package["license_file"] else None,
            "features": node["features"],
            "dependencies": sorted(edge["pkg"].split("#")[-1] for edge in node["deps"]),
        })
    packages.sort(key=lambda item: (item["name"], item["version"]))
    return {
        "schema_version": 1, "kind": "isolated_tantivy_selected_dependency_inventory",
        "platform": "x86_64-unknown-linux-gnu", "core_dependency_adopted": False,
        "package_count": len(packages),
        "manifest_sha256": hashlib.sha256((root / "Cargo.toml").read_bytes()).hexdigest(),
        "lock_sha256": hashlib.sha256((root / "Cargo.lock").read_bytes()).hexdigest(),
        "packages": packages,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    record = inventory(root)
    output = root / "dependency-inventory.json"
    rendered = json.dumps(record, indent=2) + "\n"
    if args.check:
        if output.read_text() != rendered:
            raise SystemExit("Isolated dependency inventory differs from locked metadata")
    else:
        output.write_text(rendered)
    print(json.dumps({"status": "passed", "selected_packages": record["package_count"],
                      "core_dependency_adopted": False}))


if __name__ == "__main__":
    main()
