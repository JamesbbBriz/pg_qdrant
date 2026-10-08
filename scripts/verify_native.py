#!/usr/bin/env python3
"""Fail closed on PGDG archive changes or installed native-package drift."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess


class VerificationError(ValueError):
    pass


def package_manifest(path):
    value = json.loads(Path(path).read_text())
    if value.get("schema_version") != 1 or len(value.get("packages", [])) != 7:
        raise VerificationError("expected schema 1 and exactly seven PGDG packages")
    result = {}
    for row in value["packages"]:
        name = row["package"]
        if name in result or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name):
            raise VerificationError("duplicate or invalid package name")
        if row["architecture"] not in ("amd64", "all"):
            raise VerificationError("unexpected native package architecture")
        if not re.fullmatch(r"[a-f0-9]{64}", row["deb_sha256"]):
            raise VerificationError("invalid archive SHA256")
        if not 0 < row["deb_size"] <= 128 * 1024 * 1024:
            raise VerificationError("invalid archive byte budget")
        result[name] = row
    return result


def archive_identity(path):
    completed = subprocess.run(
        ["dpkg-deb", "--field", str(path), "Package", "Version", "Architecture"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    fields = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition(": ")
        if not separator or key in fields:
            raise VerificationError("invalid dpkg-deb identity output")
        fields[key] = value
    if set(fields) != {"Package", "Version", "Architecture"}:
        raise VerificationError("incomplete dpkg-deb identity")
    return fields["Package"], fields["Version"], fields["Architecture"]


def verify_archives(manifest, cache):
    expected = package_manifest(manifest)
    seen = set()
    paths = sorted(Path(cache).glob("*.deb"))
    if len(paths) > 512:
        raise VerificationError("unexpected archive-cache size")
    for path in paths:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise VerificationError("archive must be a regular file")
        name, version, architecture = archive_identity(path)
        if name not in expected:
            if ".pgdg" in version:
                raise VerificationError("unexpected PGDG package: " + name)
            # Other downloads are authenticated by APT's fixed Ubuntu snapshot.
            continue
        row = expected[name]
        if name in seen:
            raise VerificationError("duplicate archive for " + name)
        if (version, architecture) != (row["version"], row["architecture"]):
            raise VerificationError("package identity changed: " + name)
        if metadata.st_size != row["deb_size"]:
            raise VerificationError("archive size changed: " + name)
        with path.open("rb") as archive:
            digest = hashlib.file_digest(archive, "sha256").hexdigest()
        if digest != row["deb_sha256"]:
            raise VerificationError("archive checksum changed: " + name)
        seen.add(name)
    if seen != set(expected):
        raise VerificationError("missing verified archives: " + ", ".join(sorted(set(expected) - seen)))
    return {"status": "passed", "verified_pgdg_archives": len(seen),
            "manifest_sha256": hashlib.sha256(Path(manifest).read_bytes()).hexdigest()}


def inventory(text):
    rows = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not all(fields) or fields[0] in rows:
            raise VerificationError("invalid or duplicate installed-package entry")
        rows[fields[0]] = fields[1]
    if not rows:
        raise VerificationError("empty installed-package inventory")
    return rows


def verify_installed(expected_path, observed_text):
    expected = inventory(Path(expected_path).read_text())
    actual = inventory(observed_text)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(name for name in set(expected) & set(actual) if expected[name] != actual[name])
    if missing or extra or changed:
        raise VerificationError(json.dumps({"missing": missing, "extra": extra, "changed": changed}))
    return {"status": "passed", "installed_package_entries": len(actual),
            "expected_inventory_sha256": hashlib.sha256(Path(expected_path).read_bytes()).hexdigest(),
            "observed_inventory_sha256": hashlib.sha256(observed_text.encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    archives = modes.add_parser("archives")
    archives.add_argument("--manifest", required=True)
    archives.add_argument("--cache", required=True)
    installed = modes.add_parser("installed")
    installed.add_argument("--expected", required=True)
    installed.add_argument("--observed", required=True)
    args = parser.parse_args()
    try:
        if args.mode == "archives":
            report = verify_archives(args.manifest, args.cache)
        else:
            report = verify_installed(args.expected, Path(args.observed).read_text())
    except (VerificationError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, "native package verification failed: " + str(error) + "\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
