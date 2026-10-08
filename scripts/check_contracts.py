#!/usr/bin/env python3
"""Fail closed when scope, dependency pins, or evidence claims drift."""
from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPECTED = {f"{prefix}{number:02}" for prefix, count in
            [("F", 20), ("V", 8), ("Q", 14), ("L", 12)]
            for number in range(1, count + 1)}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_json(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def main() -> None:
    rows = re.findall(r"^\| ([FVQL]\d{2}) \|", (ROOT / "docs/capabilities.md").read_text(), re.M)
    require(set(rows) == EXPECTED and len(rows) == 54,
            "The 54 capability IDs must occur exactly once in capability tables.")
    ledger = read_json("docs/work-items.json")
    require(ledger["schema_version"] == 1, "Unsupported work-item schema.")
    work = ledger["work_items"]
    by_id = {item["id"]: item for item in work}
    require(len(by_id) == len(work), "Duplicate work-item IDs.")
    covered: set[str] = set()
    primary: collections.Counter = collections.Counter()
    for item in work:
        ids = set(item["capability_ids"])
        require(ids <= EXPECTED, f"Unknown capability in {item['id']}.")
        covered |= ids
        if item["kind"] == "capability":
            primary.update(ids)
        for key in ["owner", "deliverable", "phase", "status", "estimate", "acceptance"]:
            require(bool(item.get(key)), f"Missing {key} in {item['id']}.")
        require(set(item["dependencies"]) <= by_id.keys(), f"Unknown dependency in {item['id']}.")
        require(item["id"] not in item["dependencies"], f"Self dependency in {item['id']}.")
        if item["status"] in {"complete", "completed", "release-supported"}:
            require(bool(item["evidence"]), f"Completed item {item['id']} has no evidence.")
    require(covered == EXPECTED, "Formal capability scope has been lost from the work ledger.")
    require(all(primary[i] == 1 for i in EXPECTED), "Each capability requires exactly one primary work item.")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(work_id: str) -> None:
        require(work_id not in visiting, f"Cyclic work dependency at {work_id}.")
        if work_id in visited:
            return
        visiting.add(work_id)
        for dependency in by_id[work_id]["dependencies"]:
            visit(dependency)
        visiting.remove(work_id)
        visited.add(work_id)

    for work_id in by_id:
        visit(work_id)

    baseline = read_json("docs/dependency-baseline.json")
    lock_path = ROOT / "Cargo.lock"
    require(baseline["cargo_lock_present"] == lock_path.is_file(), "Cargo.lock presence is misreported.")
    require(lock_path.is_file(), "A real resolved Cargo.lock is required for this build baseline.")
    lock = tomllib.loads(lock_path.read_text())
    packages = {(p["name"], p["version"]): p for p in lock["package"]}
    for dep in baseline["required_direct_dependencies"]:
        req = dep["cargo_requirement"]
        require(req.startswith("="), f"{dep['name']} needs an exact direct version.")
        key = (dep["name"], req[1:])
        require(key in packages, f"{key} missing from lockfile.")
        if dep.get("package_sha256"):
            require(packages[key].get("checksum") == dep["package_sha256"],
                    f"Registry checksum differs for {dep['name']}.")

    root_manifest = tomllib.loads((ROOT / "Cargo.toml").read_text())
    workspace_deps = root_manifest["workspace"]["dependencies"]
    declared: dict[str, set[str]] = collections.defaultdict(set)
    declared_defaults: dict[str, set[bool]] = collections.defaultdict(set)
    for manifest in [ROOT / "Cargo.toml", *sorted((ROOT / "crates").glob("*/Cargo.toml"))]:
        contents = tomllib.loads(manifest.read_text())
        for section in [contents.get("dependencies", {}),
                        contents.get("workspace", {}).get("dependencies", {})]:
            for name, dep in section.items():
                if isinstance(dep, dict) and dep.get("workspace"):
                    dep = workspace_deps[name]
                version = dep if isinstance(dep, str) else dep.get("version")
                if version:
                    declared[name].add(version)
                    declared_defaults[name].add(dep.get("default-features", True)
                                                if isinstance(dep, dict) else True)
        require("qdrant-client" not in contents.get("dependencies", {}),
                "A network client cannot replace the embedded dependency.")
    for dep in baseline["required_direct_dependencies"]:
        require(declared[dep["name"]] == {dep["cargo_requirement"]},
                f"Manifest/baseline drift for {dep['name']}.")
        require(declared_defaults[dep["name"]] == {dep["default_features"]},
                f"Default-feature drift for {dep['name']}.")

    pgrx = next(d["cargo_requirement"][1:] for d in baseline["required_direct_dependencies"] if d["name"] == "pgrx")
    dockerfile = (ROOT / "Dockerfile.p0").read_text()
    tool_pins = re.findall(r"cargo install cargo-pgrx --version ([0-9.]+) --locked", dockerfile)
    require(tool_pins == [pgrx], "cargo-pgrx and pgrx must be upgraded together.")
    toolchain = tomllib.loads((ROOT / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    require(re.fullmatch(r"\d+\.\d+\.\d+", toolchain) is not None, "Rust toolchain must be exact.")
    require(baseline["rust_toolchain"]["exact_version"] == toolchain,
            "Toolchain candidate/verified baseline must reflect the build configuration.")
    if baseline["compiled_dependency_graph_verified"]:
        passed = {entry.get("kind") for entry in baseline.get("build_evidence", [])
                  if entry.get("status") == "passed" and entry.get("rust") == toolchain}
        require(bool(passed & {"engine_build", "engine_runtime"}) and "postgres_build" in passed,
                "A compiled combined graph requires successful engine and PostgreSQL linked builds on the exact toolchain.")
    graph_path = ROOT / "docs/dependency-graph.json"
    if graph_path.exists():
        graph = read_json("docs/dependency-graph.json")
        require(graph["cargo_lock_sha256"] == hashlib.sha256(lock_path.read_bytes()).hexdigest(),
                "Dependency inventory is stale after a lockfile change.")
        for manifest in graph["manifest_inputs"]:
            path = ROOT / manifest["path"]
            require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == manifest["sha256"],
                    f"Dependency inventory is stale after changing {manifest['path']}.")
        resolved_features = {package["package"]: set(package["features"])
                             for package in graph["packages"]}
        for dependency in baseline["required_direct_dependencies"]:
            coordinate = dependency["name"] + "@" + dependency["cargo_requirement"][1:]
            require(resolved_features.get(coordinate) == set(dependency["features"]),
                    f"Resolved feature/baseline drift for {dependency['name']}.")
    profiles_path = ROOT / "docs/dependency-profiles.json"
    if profiles_path.exists():
        comparison = read_json("docs/dependency-profiles.json")
        require(comparison["schema_version"] == 1, "Unsupported profile inventory schema.")
        require(comparison["cargo_lock_sha256"] == hashlib.sha256(lock_path.read_bytes()).hexdigest(),
                "Profile comparison is stale after a lockfile change.")
        for manifest in comparison["manifest_inputs"]:
            path = ROOT / manifest["path"]
            require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == manifest["sha256"],
                    f"Profile comparison is stale after changing {manifest['path']}.")
        profiles = {profile["profile"]: profile for profile in comparison["profiles"]}
        require(len(profiles) == len(comparison["profiles"]) == 4 and set(profiles) == {
            "direct-normal", "direct-private", "helper-normal", "helper-private"},
            "All four distinct P0 feature profiles are required.")
        require(profiles["direct-normal"]["full_inventory_sha256"] ==
                hashlib.sha256(graph_path.read_bytes()).hexdigest(),
                "Normal inventory and profile comparison disagree.")
        require(baseline["build_configuration"].get("profile_resolution_evidence") ==
                "dependency-profiles.json", "Baseline must identify the profile comparison.")
    print(json.dumps({"status": "passed", "capabilities": 54, "work_items": len(work),
                      "scope_completed": False, "compiled_graph_claim": baseline["compiled_dependency_graph_verified"]}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        print(f"Contract validation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
