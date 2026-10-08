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
PROFILES = {
    "direct-normal": ["pg_qdrant/pg17"],
    "direct-private": ["pg_qdrant/pg17", "pg_qdrant/p0-fault-injection"],
    "helper-normal": ["pg_qdrant/pg17", "pg_qdrant/p0-managed-helper"],
    "helper-private": ["pg_qdrant/pg17", "pg_qdrant/p0-managed-helper",
                       "pg_qdrant/p0-fault-injection", "pg-qdrant-helper/p0-fault-injection"],
}


def manifest_inputs() -> list[dict[str, str]]:
    paths = [ROOT / "Cargo.toml", ROOT / "rust-toolchain.toml",
             *sorted((ROOT / "crates").glob("*/Cargo.toml"))]
    return [{"path": path.relative_to(ROOT).as_posix(),
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(paths)]


def command(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True)


def collect(profile: str = "direct-normal") -> dict:
    lock_bytes = (ROOT / "Cargo.lock").read_bytes()
    locked = tomllib.loads(lock_bytes.decode("utf-8"))
    checksums = {
        (p["name"], p["version"]): p.get("checksum") for p in locked["package"]
    }
    metadata = json.loads(command(
        "cargo", "metadata", "--locked", "--format-version", "1",
        "--no-default-features", "--features", ",".join(PROFILES[profile]),
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
        "requested_features": [*PROFILES[profile], "pgrx/cshim"],
        "cargo_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "manifest_inputs": manifest_inputs(),
        "packages": packages,
        "limitations": [
            "License expressions are package metadata, not a complete license audit.",
            "Native system libraries and bundled dictionaries need separate review.",
            "Dependency resolution does not prove successful compilation or runtime support.",
        ],
    }


def serialize(report: dict) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False) + "\n"


def compare_profiles(reports: dict[str, dict]) -> dict:
    base = reports["direct-normal"]
    baseline = {p["package"]: p for p in base["packages"]}

    def registry(packages: list[dict]) -> set[tuple]:
        return {(p["package"], p["source"], p["checksum"])
                for p in packages if p["source"] != "workspace"}

    profiles = []
    for name, report in reports.items():
        if (report["cargo_lock_sha256"] != base["cargo_lock_sha256"]
                or report["manifest_inputs"] != base["manifest_inputs"]):
            raise ValueError("Dependency inputs changed during profile resolution.")
        packages = {p["package"]: p for p in report["packages"]}
        changes = []
        for package in sorted(baseline.keys() & packages.keys()):
            before, after = baseline[package], packages[package]
            delta = {key + "_" + direction: sorted(set(left[key]) - set(right[key]))
                     for key in ("features", "dependencies")
                     for direction, left, right in [("added", after, before), ("removed", before, after)]}
            if any(delta.values()):
                changes.append({"package": package, **delta})
        profiles.append({
            "profile": name,
            "metadata_feature_arguments": PROFILES[name],
            "requested_features": report["requested_features"],
            "full_inventory_sha256": hashlib.sha256(serialize(report).encode()).hexdigest(),
            "package_count": len(packages),
            "registry_coordinates_checksums_match_direct_normal": registry(report["packages"]) == registry(base["packages"]),
            "workspace_packages": [p for p in report["packages"] if p["source"] == "workspace"],
            "changes_from_direct_normal": {
                "packages_added": [packages[p] for p in sorted(packages.keys() - baseline.keys())],
                "packages_removed": sorted(baseline.keys() - packages.keys()),
                "package_feature_dependency_changes": changes,
            },
        })
    return {
        "schema_version": 1,
        "evidence_kind": "cargo_locked_profile_resolution_comparison",
        "target": base["target"],
        "baseline_profile": "direct-normal",
        "cargo_lock_sha256": base["cargo_lock_sha256"],
        "manifest_inputs": base["manifest_inputs"],
        "profiles": profiles,
        "limitations": [
            "Resolution only: this comparison does not verify compilation, runtime, or release support.",
            "Cargo metadata resolves workspace packages; inclusion is not proof that a package is linked into a selected binary.",
            "Helper-private explicitly selects both extension and helper-binary private features used by the image build.",
            "Inventory hashes cover the same serialized full report produced by --profile; comparisons are against direct-normal.",
        ],
    }


def write_or_check(path: pathlib.Path, report: dict, check: bool) -> None:
    if check:
        if json.loads(path.read_text()) != report:
            raise SystemExit(f"Dependency inventory differs from the locked graph: {path.name}; regenerate and review it.")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialize(report), encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="direct-normal")
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--comparison-output", type=pathlib.Path,
                        help="Also resolve all four profiles and write/check their concise comparison")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    report = collect(args.profile)
    filename = "dependency-graph.json" if args.profile == "direct-normal" else f"dependency-graph-{args.profile}.json"
    write_or_check(args.output or ROOT / "docs" / filename, report, args.check)
    if args.comparison_output:
        reports = {name: report if name == args.profile else collect(name) for name in PROFILES}
        write_or_check(args.comparison_output, compare_profiles(reports), args.check)
    missing = [p["package"] for p in report["packages"]
               if not p["license_expression"] and not p["license_file_sha256"]]
    print(json.dumps({"packages": len(report["packages"]), "license_metadata_missing": missing,
                      "lock_sha256": report["cargo_lock_sha256"]}))


if __name__ == "__main__":
    main()
