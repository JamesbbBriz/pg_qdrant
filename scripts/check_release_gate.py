#!/usr/bin/env python3
"""Fail-closed, offline candidate-release gate. Not proof of genuine CI execution.

A passing local attestation is a necessary review input, never sufficient proof
of genuine execution. Reviewers must inspect the snapshot-bound act receipts,
complete logs, native results and product acceptance evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys

def acceptance_gates(path: pathlib.Path) -> tuple[str, ...]:
    """The acceptance table is the authority, including future added gates."""
    found: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = line.strip().split("|")
        if len(cells) < 3 or cells[0]:
            continue
        name = cells[1].strip().strip("`")
        if not re.match(r"P\d+-", name):
            continue
        if re.fullmatch(r"P[0-5]-[A-Z][A-Z0-9_-]*", name) is None:
            raise ValueError(f"invalid acceptance gate: {name}")
        found.append(name)
    gates = tuple(found)
    if not gates or len(set(gates)) != len(gates) or set(
            name.split("-")[0] for name in gates) != {f"P{i}" for i in range(6)}:
        raise ValueError("missing phases or duplicate acceptance gates")
    return gates


REQUIRED_GATES = acceptance_gates(
    pathlib.Path(__file__).resolve().parents[1] / "docs/acceptance.md")
REQUIRED_ARTIFACTS = (
    "pg_qdrant.so", "pg_qdrant.control", "pg_qdrant--0.0.1.sql", "pg_qdrant_p0_helper"
)
REQUIRED_FEATURES = tuple(
    f"{prefix}{i:02}" for prefix, n in (("F", 20), ("V", 8), ("Q", 14), ("L", 12))
    for i in range(1, n + 1)
)


def digest(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verified_file(root: pathlib.Path, item: object) -> bool:
    if not isinstance(item, dict):
        return False
    rel, sha = item.get("path"), item.get("sha256")
    if not isinstance(rel, str) or not isinstance(sha, str):
        return False
    if len(sha) != 64 or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
        return False
    path = pathlib.Path(rel)
    if path.is_absolute() or ".." in path.parts:
        return False
    absolute = root / path
    if any((root / pathlib.Path(*path.parts[:i])).is_symlink()
           for i in range(1, len(path.parts) + 1)):
        return False
    if not absolute.is_file():
        return False
    return digest(absolute) == sha


def local_ci_evidence(root: pathlib.Path, ci: object, git_sha: str) -> bool:
    """Verify complete local act receipts; static-only and dirty runs cannot release."""
    if not isinstance(ci, dict) or ci.get("runner") != "act-local" or \
            ci.get("conclusion") != "success" or ci.get("checkout_sha") != git_sha:
        return False
    if not all(verified_file(root, ci.get(key)) for key in ["run_report", "verify_report"]):
        return False
    try:
        run = json.loads((root / ci["run_report"]["path"]).read_text())
        verification = json.loads((root / ci["verify_report"]["path"]).read_text())
        snapshot = verification["snapshot"]
        registry = json.loads((root / "ci/steps.json").read_text())
        required = [step["id"] for steps in registry["profiles"].values() for step in steps]
        actual = verification["steps"]
        files = snapshot["files"]
        manifest = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        clean_diff = hashlib.sha256(b"").hexdigest()
        log_root = (root / ci["verify_report"]["path"]).parent
        return (run["status"] == verification["status"] == "passed"
                and run["profile"] == verification["profile"] == "full"
                and run["act_exit_code"] == 0 and run["act_version"].startswith("act version ")
                and run["run_id"] == verification["run_id"]
                and run["snapshot"] == snapshot and snapshot["commit"] == git_sha
                and snapshot["dirty_diff_sha256"] == clean_diff
                and snapshot["untracked_inputs"] == []
                and snapshot["source_sha256"] == hashlib.sha256(manifest).hexdigest()
                and bool(files) and all(verified_file(root, item) for item in files)
                and [item["id"] for item in actual] == required
                and bool(required)
                and all(item["status"] == "passed" and item.get("exit_code") == 0
                        and verified_file(log_root, {"path": item.get("log"),
                            "sha256": item.get("log_sha256")}) for item in actual))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def assess(root: pathlib.Path, attestation: pathlib.Path,
           artifact_root: pathlib.Path, git_sha: str | None = None) -> dict:
    root = root.resolve()
    errors: list[str] = []
    try:
        required_gates = acceptance_gates(root / "docs/acceptance.md")
        if required_gates != REQUIRED_GATES:
            errors.append("acceptance authority differs from the checked gate implementation")
    except (OSError, ValueError):
        errors.append("authoritative acceptance table unavailable or invalid")
        required_gates = REQUIRED_GATES
    if git_sha is None:
        process = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                 text=True, capture_output=True, check=False)
        git_sha = process.stdout.strip() if process.returncode == 0 else ""
    if re.fullmatch(r"[0-9a-f]{40}", git_sha or "") is None:
        errors.append("source checkout Git SHA is unavailable")

    if not all((root / path).is_file() for path in
               ["ci/verify.yml", "ci/steps.json", "scripts/verify.py", "scripts/local_ci.py"]):
        errors.append("local act verification workflow is missing or incomplete")

    try:
        ledger = json.loads((root / "docs/work-items.json").read_text())
        if set(ledger["capability_ids"]) != set(REQUIRED_FEATURES) or len(
                ledger["work_items"]) != 70:
            errors.append("54-capability/70-work-item contract drift")
    except (OSError, ValueError, KeyError, TypeError):
        errors.append("work item/capability ledger unavailable")

    try:
        license_text = (root / "LICENSE").read_text()
        manifest = (root / "Cargo.toml").read_text()
        if "GNU AFFERO GENERAL PUBLIC LICENSE" not in license_text.upper() or \
                'license = "AGPL-3.0-only"' not in manifest:
            errors.append("source license/manifest is not the recorded AGPL-3.0-only baseline")
    except OSError:
        errors.append("project license or manifest missing")

    try:
        att = json.loads(attestation.read_text())
        if not isinstance(att, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        errors.append("real revision-bound release attestation is missing or invalid")
        att = {}

    if att.get("schema_version") != 1 or att.get("checkout_sha") != git_sha:
        errors.append("attestation schema/checkout SHA mismatch")
    ci = att.get("ci", {})
    if not local_ci_evidence(root, ci, git_sha):
        errors.append("verified revision-bound full CI attestation missing")

    gates = att.get("gates", {})
    if not isinstance(gates, dict):
        gates = {}
    for name in required_gates:
        value = gates.get(name)
        if not isinstance(value, dict) or value.get("status") != "passed" or \
                value.get("checkout_sha") != git_sha or not isinstance(
                    value.get("evidence"), list) or not value["evidence"] or not all(
                    verified_file(root, entry) for entry in value["evidence"]):
            errors.append(f"{name}: no exact-source passing evidence")

    coverage = att.get("capabilities", {})
    if not isinstance(coverage, dict) or set(coverage) != set(REQUIRED_FEATURES):
        errors.append("full 54-capability disposition is absent")
    else:
        for name in REQUIRED_FEATURES:
            item = coverage[name]
            if not isinstance(item, dict) or item.get("disposition") not in (
                    "supported", "explicitly_excluded") or not item.get("rationale"):
                errors.append(f"{name}: unsupported or unreviewed coverage disposition")

    artifact_manifest = att.get("artifacts", {})
    if not isinstance(artifact_manifest, dict):
        artifact_manifest = {}
    for name in REQUIRED_ARTIFACTS:
        value = artifact_manifest.get(name)
        if not verified_file(artifact_root, {"path": name, "sha256": value}):
            errors.append(f"artifact {name}: missing or invalid checksum")

    return {"ready": not errors, "checkout_sha": git_sha,
            "required_gates": len(required_gates), "blockers": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parents[1])
    parser.add_argument("--attestation", type=pathlib.Path)
    parser.add_argument("--artifact-root", type=pathlib.Path)
    args = parser.parse_args()
    root = args.root
    report = assess(root,
                    args.attestation or root / "dist/release-attestation.json",
                    args.artifact_root or root / "dist/candidate")
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    sys.exit(main())
