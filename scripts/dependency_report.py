#!/usr/bin/env python3
"""Export a path-free dependency/features/license inventory from Cargo's locked graph.

Resolution is not compilation evidence. Compilation and runtime evidence are recorded
separately in docs/dependency-baseline.json and docs/acceptance.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def manifest_inputs() -> list[dict[str, str]]:
    paths = [ROOT / "Cargo.toml", ROOT / "rust-toolchain.toml",
             *sorted((ROOT / "crates").glob("*/Cargo.toml"))]
    return [{"path": str(path.relative_to(ROOT)),
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(paths)]


def command(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True)


def collect() -> dict:
    lock_bytes = (ROOT / "Cargo.lock").read_bytes()
    locked = tomllib.loads(lock_bytes.decode("utf-8"))
    checksums = {
        (p["name"], p["version"]): p.get("checksum") for p in locked["package"]
    }
    metadata = json.loads(command(
        "cargo", "metadata", "--locked", "--format-version", "1",
        "--no-default-features", "--features", "pg_qdrant/pg17",
        "--filter-platform", "x86_64-unknown-linux-gnu",
    ))
    names = {p["id"]: f'{p["name"]}@{p["version"]}' for p in metadata["packages"]}
    resolved = {p["id"]: p for p in metadata["resolve"]["nodes"]}
    packages = []
    for package in sorted(metadata["packages"], key=lambda p: (p["name"], p["version"])):
        node = resolved.get(package["id"])
        if node is None:
            continue
        license_file = package.get("license_file")
        license_hash = None
        if license_file:
            path = pathlib.Path(package["manifest_path"]).parent / license_file
            if path.is_file():
                license_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        packages.append({
            "package": names[package["id"]],
            "source": package["source"] or "workspace",
            "checksum": checksums.get((package["name"], package["version"])),
            "license_expression": package.get("license"),
            "license_file_sha256": license_hash,
            "features": sorted(node["features"]),
            "dependencies": sorted({names[d] for d in node["dependencies"]}),
        })
    return {
        "schema_version": 1,
        "evidence_kind": "cargo_locked_resolution",
        "target": "x86_64-unknown-linux-gnu",
        "requested_features": ["pg_qdrant/pg17", "pgrx/cshim"],
        "cargo_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "manifest_inputs": manifest_inputs(),
        "packages": packages,
        "limitations": [
            "License expressions are package metadata, not a complete license audit.",
            "Native system libraries and bundled dictionaries need separate review.",
            "Dependency resolution does not prove successful compilation or runtime support.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, default=ROOT / "docs/dependency-graph.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    report = collect()
    if args.check:
        if json.loads(args.output.read_text()) != report:
            raise SystemExit("Dependency inventory differs from the locked graph; regenerate and review it.")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    missing = [p["package"] for p in report["packages"]
               if not p["license_expression"] and not p["license_file_sha256"]]
    print(json.dumps({"packages": len(report["packages"]), "license_metadata_missing": missing,
                      "lock_sha256": report["cargo_lock_sha256"]}))


if __name__ == "__main__":
    main()
