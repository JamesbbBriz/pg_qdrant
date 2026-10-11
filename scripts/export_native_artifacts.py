"""Copy runtime binaries and complete native audit inputs out of Cargo's cache."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

MAX_BYTES = 2 * 1024**3


def export(target, destination, *, helper=False, maximum=MAX_BYTES):
    for path in [target, *target.parents, destination, *destination.parents]:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("artifact links are unsupported")
    if destination.exists():
        raise ValueError("artifact destination must not exist")
    build = target / "debug/build"
    required = [target / "debug/pg-qdrant-edge-probe"]
    if helper:
        required.append(target / "debug/pg_qdrant_p0_helper")
    if not build.is_dir() or any(not item.is_file() for item in required):
        raise ValueError("required runtime or audit artifact missing")
    # Preserve every Edge C-object directory and every pgrx bindings/trace
    # directory used by the unchanged native CPU and build-report checks.
    audit = []
    for pattern in ("qdrant-edge-*", "pgrx-pg-sys-*"):
        directories = sorted(build.glob(pattern))
        if not directories:
            raise ValueError("required native audit directories missing")
        for directory in directories:
            audit.extend([directory, *sorted(directory.rglob("*"))])
    paths = [*required, *audit]
    files = []
    for path in paths:
        for ancestor in [path, *path.parents]:
            if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
                raise ValueError("artifact links are unsupported")
            if ancestor == target:
                break
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("artifact must be a regular file")
        files.append(path)
    size = sum(path.stat().st_size for path in files)
    if size > maximum:
        raise RuntimeError("runtime and audit artifacts exceed export byte budget")
    records = []
    destination.mkdir(parents=True)
    for source in files:
        relative = source.relative_to(target)
        copied = destination / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copied)
        with source.open("rb") as stream:
            expected = hashlib.file_digest(stream, "sha256").hexdigest()
        with copied.open("rb") as stream:
            observed = hashlib.file_digest(stream, "sha256").hexdigest()
        if expected != observed:
            raise RuntimeError("native artifact changed during export")
        records.append({"path": relative.as_posix(), "bytes": copied.stat().st_size, "sha256": observed})
    report = {"kind": "native_runtime_and_audit_artifacts", "bytes": size,
              "limit_bytes": maximum, "files": records,
              "cargo_dependency_archives_exported": False, "test_executables_exported": False}
    (destination / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--helper", action="store_true")
    args = parser.parse_args()
    result = export(args.target, args.destination, helper=args.helper)
    print(json.dumps({"exported_bytes": result["bytes"], "files": len(result["files"])}))
