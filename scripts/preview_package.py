#!/usr/bin/env python3
"""Build and inspect a local PG17 preview archive; never authorize a release."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import tarfile

MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_MEMBERS = 4096
INSTALL_PATHS = {
    "usr/lib/postgresql/17/lib/pg_qdrant.so": 0o644,
    "usr/lib/postgresql/17/bin/pg_qdrant_p0_helper": 0o755,
    "usr/share/postgresql/17/extension/pg_qdrant.control": 0o644,
    "usr/share/postgresql/17/extension/pg_qdrant--0.0.1.sql": 0o644,
}


class BoundedTarInfo(tarfile.TarInfo):
    @classmethod
    def fromtarfile(cls, archive):
        # Some Python security backports route this through _frombuf(), which
        # bypasses a public frombuf() override. Inspect the fixed header here.
        member = cls.frombuf(archive.fileobj.read(tarfile.BLOCKSIZE), archive.encoding, archive.errors)
        member.offset = archive.fileobj.tell() - tarfile.BLOCKSIZE
        return member._proc_member(archive)

    @classmethod
    def frombuf(cls, buffer, encoding, errors):
        member = super().frombuf(buffer, encoding, errors)
        # Reject extension headers before tarfile reads their declared bodies.
        # Otherwise a PAX/GNU header can allocate before the member budget runs.
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or not 0 <= member.size <= MAX_FILE:
            raise ValueError("nonregular, extended or oversized archive header")
        return member


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative_name(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or PurePosixPath(name).is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError("invalid package member name")
    return name


def checked_file(root, name):
    relative_name(name)
    root = Path(root).resolve(strict=True)
    path = root / name
    if any((root / Path(*Path(name).parts[:n])).is_symlink()
           for n in range(1, len(Path(name).parts) + 1)):
        raise ValueError("package inputs must not contain symlinks")
    if not path.is_file() or not 0 <= path.stat().st_size <= MAX_FILE:
        raise ValueError("missing or oversized package input: " + name)
    return path


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def write_archive(output, inputs, facts):
    """Normalize archive metadata, not the contents of upstream or native files."""
    output = Path(output)
    pending = output.with_name(output.name + ".pending")
    entries = []
    total = 0
    for name, path, mode in sorted(inputs):
        relative_name(name)
        path = Path(path)
        size = path.stat().st_size
        if path.is_symlink() or not path.is_file() or not 0 <= size <= MAX_FILE:
            raise ValueError("invalid archive input")
        total += size
        entries.append({"path": name, "size": size, "mode": mode, "sha256": digest(path)})
    names = [entry["path"] for entry in entries]
    if (not entries or len(entries) >= MAX_MEMBERS or len(set(names)) != len(names)
            or "manifest.json" in names or total > MAX_TOTAL):
        raise ValueError("duplicate or excessive archive inputs")
    manifest = dict(facts, schema_version=1, kind="local_pg17_preview", release_supported=False,
                    files=entries)
    with pending.open("xb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=1, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
                for entry, (_, path, _) in zip(entries, sorted(inputs)):
                    header = tarfile.TarInfo(entry["path"])
                    header.size, header.mode = entry["size"], entry["mode"]
                    with Path(path).open("rb") as stream:
                        archive.addfile(header, stream)
                body = canonical(manifest)
                header = tarfile.TarInfo("manifest.json")
                header.size, header.mode = len(body), 0o644
                archive.addfile(header, io.BytesIO(body))
        raw.flush()
        os.fsync(raw.fileno())
    # Re-read every member before publication; a changing input cannot silently
    # produce an archive whose content differs from its recorded checksum.
    verify_archive(pending)
    os.link(pending, output, follow_symlinks=False)
    pending.unlink()
    return manifest


def verify_archive(path):
    with tarfile.open(path, "r:gz", tarinfo=BoundedTarInfo) as archive:
        members = []
        total = 0
        for member in archive:
            relative_name(member.name)
            if not member.isfile() or not 0 <= member.size <= MAX_FILE:
                raise ValueError("nonregular or oversized archive member")
            total += member.size
            members.append(member)
            if total > MAX_TOTAL or len(members) > MAX_MEMBERS:
                raise ValueError("archive exceeds inspection budget")
        names = [member.name for member in members]
        if len(names) != len(set(names)) or names.count("manifest.json") != 1:
            raise ValueError("duplicate or missing archive manifest")
        info = next(member for member in members if member.name == "manifest.json")
        if info.size > 1024 * 1024:
            raise ValueError("manifest exceeds byte budget")
        manifest = json.load(archive.extractfile(info))
        if (manifest.get("schema_version") != 1 or manifest.get("kind") != "local_pg17_preview"
                or manifest.get("release_supported") is not False):
            raise ValueError("unsupported archive manifest")
        entries = manifest["files"]
        expected = {entry["path"]: entry for entry in entries}
        if len(expected) != len(entries) or set(expected) != set(names) - {"manifest.json"}:
            raise ValueError("manifest membership mismatch")
        for member in members:
            if member.name == "manifest.json":
                continue
            entry = expected[member.name]
            if (entry["size"] != member.size or entry["mode"] not in (0o644, 0o755)
                    or member.mode != entry["mode"]
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                    or hashlib.file_digest(archive.extractfile(member), "sha256").hexdigest()
                    != entry["sha256"]):
                raise ValueError("archive member checksum or metadata mismatch")
        if not set(INSTALL_PATHS).issubset(expected):
            raise ValueError("incomplete installed product")
        for name, mode in INSTALL_PATHS.items():
            if expected[name]["mode"] != mode:
                raise ValueError("incorrect install mode")
        return manifest


def stage_archive(archive_path, destination):
    manifest = verify_archive(archive_path)
    destination = Path(destination).absolute()
    if destination.parent.resolve(strict=True) != destination.parent:
        raise ValueError("staging parent must not contain symlinks")
    destination.mkdir(mode=0o700)  # Never reuse or overwrite an existing tree.
    with tarfile.open(archive_path, "r:gz", tarinfo=BoundedTarInfo) as archive:
        for name, mode in INSTALL_PATHS.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            member = archive.getmember(name)
            expected = next(item for item in manifest["files"] if item["path"] == name)
            if not member.isfile() or member.size != expected["size"]:
                raise ValueError("archive changed before staging")
            with archive.extractfile(member) as source, target.open("xb") as output:
                for chunk in iter(lambda: source.read(65536), b""):
                    output.write(chunk)
            os.chmod(target, mode)
            if digest(target) != next(item["sha256"] for item in manifest["files"] if item["path"] == name):
                raise ValueError("staged product changed during extraction")
    return manifest


def build_preview(root, output, pg_config, snapshot_path):
    root = Path(root).resolve(strict=True)
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("preview build requires Linux x86_64")
    def pg(option):
        return subprocess.check_output([pg_config, option], text=True, timeout=10).strip()
    if not pg("--version").startswith("PostgreSQL 17."):
        raise ValueError("PostgreSQL 17 build required")
    if (pg("--bindir") != "/usr/lib/postgresql/17/bin"
            or pg("--pkglibdir") != "/usr/lib/postgresql/17/lib"
            or pg("--sharedir") != "/usr/share/postgresql/17"):
        raise ValueError("preview requires the documented PGDG layout")
    snapshot_path = Path(snapshot_path)
    if snapshot_path.is_symlink() or not snapshot_path.is_file() or snapshot_path.stat().st_size > 1024 * 1024:
        raise ValueError("invalid source snapshot manifest")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if (snapshot["dirty_diff_sha256"] != hashlib.sha256(b"").hexdigest()
            or snapshot["untracked_inputs"] or not re.fullmatch(r"[0-9a-f]{40}", snapshot["commit"])
            or os.environ.get("PG_QDRANT_SOURCE_SHA") != snapshot["commit"]):
        raise ValueError("clean exact-commit installed build required")
    rows = snapshot["files"]
    if hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != snapshot["source_sha256"]:
        raise ValueError("source manifest identity mismatch")
    inputs = []
    for row in rows:
        path = checked_file(root, row["path"])
        if digest(path) != row["sha256"]:
            raise ValueError("source snapshot changed: " + row["path"])
        inputs.append(("source/" + row["path"], path, 0o644))
    native = json.loads(checked_file(root, "artifacts/native-build-report.json").read_text(encoding="utf-8"))
    if native["profile"] != "helper" or native["status"] != "collected":
        raise ValueError("normal managed-helper build report required")
    for name, mode in INSTALL_PATHS.items():
        path = checked_file(Path("/"), name)
        if name.endswith(".so") or name.endswith("_helper"):
            role = "extension" if name.endswith(".so") else "helper"
            if digest(path) != native["elf"][role]["sha256"]:
                raise ValueError("installed native binary differs from build report")
            with path.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    raise ValueError("installed native artifact is not ELF")
        inputs.append((name, path, mode))
    for name in ("native-build-report.json", "native-cpu-objects.json", "cpu-admission.json"):
        inputs.append(("evidence/" + name, checked_file(root, "artifacts/" + name), 0o644))
    inputs.append(("evidence/source-input.json", snapshot_path, 0o644))
    return write_archive(output, inputs, {
        "source_commit": snapshot["commit"], "source_manifest_sha256": snapshot["source_sha256"],
        "platform": {"os": "Linux", "architecture": "x86_64", "postgres_major": 17,
                     "layout": "PGDG", "build_profile": "debug", "fault_injection": False},
        "scope": "local installation preview; all P1-P5 release gates remain required",
        "license_review_complete": False, "reproducible_binary_build_verified": False,
        "notes": "Includes original source, upstream declarations and existing notices; complete transitive distribution review remains open.",
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--root", type=Path, default=Path("/src"))
    build.add_argument("--pg-config", default="/usr/lib/postgresql/17/bin/pg_config")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--snapshot", type=Path, required=True)
    for name in ("verify", "stage"):
        child = commands.add_parser(name)
        child.add_argument("--archive", type=Path, required=True)
        if name == "stage":
            child.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        result = build_preview(args.root, args.output, args.pg_config, args.snapshot)
    elif args.command == "stage":
        result = stage_archive(args.archive, args.destination)
    else:
        result = verify_archive(args.archive)
    print(json.dumps({key: value for key, value in result.items() if key != "files"}, indent=2))


if __name__ == "__main__":
    main()
