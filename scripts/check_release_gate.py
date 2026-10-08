#!/usr/bin/env python3
"""Fail-closed, offline candidate-release gate. Not proof of genuine CI execution.

A passing local attestation is a necessary review input, never sufficient proof
of a real GitHub Actions result. Reviewers must check the cited run externally.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys

PHASES = {
    "P0": "BUILD API ENGINE LEXICAL PG FAULT DECISION",
    "P1": "CATALOG BACKFILL TRANSACTION ORDER IDENTITY MODEL DURABILITY WAIT DDL",
    "P2": "TEXT HYBRID PERMISSION RESULT STATS DISCOVERY EXAMPLE QUALITY",
    "P3": "FAST FILTER RESOURCE MAINTENANCE GENERATION RECOVERY UPGRADE PLATFORM",
    "P4": "VISUAL EXPLORE SCORING READ LEXICAL DUAL COVERAGE",
    "P5": "PACKAGE DOCS LICENSE CI JOURNEY",
}
REQUIRED_GATES = tuple(
    f"{phase}-{name}" for phase, names in PHASES.items()
    for name in names.split()
)
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


def assess(root: pathlib.Path, attestation: pathlib.Path,
           artifact_root: pathlib.Path, git_sha: str | None = None) -> dict:
    root = root.resolve()
    errors: list[str] = []
    if git_sha is None:
        process = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                 text=True, capture_output=True, check=False)
        git_sha = process.stdout.strip() if process.returncode == 0 else ""
    if re.fullmatch(r"[0-9a-f]{40}", git_sha or "") is None:
        errors.append("source checkout Git SHA is unavailable")

    try:
        workflow = (root / ".github/workflows/p0.yml").read_text()
        if not re.search(r"(?m)^  push:\s*$", workflow) or not re.search(
                r"(?m)^  pull_request:\s*$", workflow):
            errors.append("automatic CI triggers remain disabled; restore push and pull_request")
    except OSError:
        errors.append("P0 workflow is missing")

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
    if not isinstance(ci, dict) or ci.get("conclusion") != "success" or \
            ci.get("checkout_sha") != git_sha or not re.fullmatch(
                r"https://github\.com/JamesbbBriz/pg_qdrant/actions/runs/[0-9]+",
                str(ci.get("url", ""))):
        errors.append("verified revision-bound full CI attestation missing")

    gates = att.get("gates", {})
    if not isinstance(gates, dict):
        gates = {}
    for name in REQUIRED_GATES:
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
            "required_gates": len(REQUIRED_GATES), "blockers": errors}


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
