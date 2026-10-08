#!/usr/bin/env python3
"""Collect read-only native build evidence into a CI artifact, not a license clearance."""

import sys

# Keep imported local modules read-only too; the requested report is the sole output.
sys.dont_write_bytecode = True

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from verify_native import VerificationError, verify_installed


MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_COMMAND_BYTES = 4 * 1024 * 1024
DEP5_FORMATS = {
    scheme + "://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
    for scheme in ("http", "https")
}


def sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, check=True, timeout=30,
                            env={**os.environ, "LC_ALL": "C"})
    if len(result.stdout) + len(result.stderr) > MAX_COMMAND_BYTES:
        raise VerificationError("command output exceeds the report budget")
    return result.stdout.decode("utf-8", errors="strict").strip()


def installed_packages(output):
    rows = []
    seen = set()
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            raise VerificationError("expected package, version, architecture and dpkg status")
        name, version, architecture, status = fields
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?", name):
            raise VerificationError("invalid binary package name")
        if name in seen or not version or not architecture or status != "installed":
            raise VerificationError("duplicate, incomplete or not-installed dpkg entry: " + name)
        seen.add(name)
        rows.append({"package": name, "version": version, "architecture": architecture})
    if not 0 < len(rows) <= 4096:
        raise VerificationError("unexpected installed package count")
    return sorted(rows, key=lambda row: row["package"])


def dep5_licenses(text):
    """Keep declared synopsis labels and their stanza scope; never infer SPDX IDs."""
    paragraphs = re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").strip())
    parsed = []
    for paragraph in paragraphs:
        fields = {}
        previous = None
        for line in paragraph.splitlines():
            if line.startswith((" ", "\t")):
                if previous is None:
                    return {"status": "unparsed_malformed_dep5", "labels": None, "declarations": None}
                fields[previous].append(line[1:])
                continue
            match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9-]*):[ \t]*(.*)", line)
            if not match or match[1].lower() in fields:
                # Most Debian copyright files legitimately use the older prose format.
                status = "unparsed_malformed_dep5" if text.startswith("Format:") else "unparsed_non_dep5"
                return {"status": status, "labels": None, "declarations": None}
            previous = match[1].lower()
            fields[previous] = [match[2]]
        parsed.append(fields)
    if not parsed or parsed[0].get("format", [None])[0] not in DEP5_FORMATS:
        return {"status": "unparsed_non_dep5", "labels": None, "declarations": None}
    declarations = []
    for number, fields in enumerate(parsed):
        if "license" not in fields:
            continue
        label = fields["license"][0].strip()
        if not label:
            return {"status": "unparsed_empty_license_synopsis", "labels": None, "declarations": None}
        scope = "header" if number == 0 else "files" if "files" in fields else "definition"
        declarations.append({"stanza": number, "scope": scope,
                             "files": "\n".join(fields["files"]) if "files" in fields else None,
                             "license_synopsis": label})
    return {"status": "parsed_dep5" if declarations else "dep5_without_license_labels",
            "labels": sorted({row["license_synopsis"] for row in declarations}) or None,
            "declarations": declarations}


def copyright_metadata(package, doc_root=Path("/usr/share/doc")):
    base_name = package.split(":", 1)[0]
    requested = doc_root / base_name / "copyright"
    result = {"requested_file": str(requested), "resolved_file": None,
              "sha256": None, "size_bytes": None, "labels": None, "declarations": None}
    try:
        resolved = requested.resolve(strict=True)
        result["resolved_file"] = str(resolved)
        if not resolved.is_relative_to(doc_root.resolve()):
            return {**result, "status": "unreadable_outside_documentation_root"}
        if not resolved.is_file():
            return {**result, "status": "unreadable_not_regular_file"}
        size = resolved.stat().st_size
        result["size_bytes"] = size
        if size > MAX_METADATA_BYTES:
            return {**result, "status": "unparsed_size_budget_exceeded"}
        content = resolved.read_bytes()
        result["sha256"] = hashlib.sha256(content).hexdigest()
        try:
            return {**result, **dep5_licenses(content.decode("utf-8", errors="strict"))}
        except UnicodeDecodeError:
            return {**result, "status": "unparsed_non_utf8"}
    except FileNotFoundError:
        return {**result, "status": "missing_copyright_file"}
    except (OSError, RuntimeError):
        return {**result, "status": "unreadable_copyright_file"}


def parse_elf_private_headers(output):
    formats = re.findall(r"^.*:\s+file format (\S+)\s*$", output, flags=re.MULTILINE)
    if len(formats) != 1 or not formats[0].startswith("elf"):
        raise VerificationError("objdump did not report exactly one ELF file")
    fields = {"NEEDED": [], "SONAME": [], "RPATH": [], "RUNPATH": []}
    for line in output.splitlines():
        match = re.fullmatch(r"\s+(NEEDED|SONAME|RPATH|RUNPATH)\s+(\S.*)", line)
        if match:
            fields[match[1]].append(match[2])
    if any(len(fields[key]) > 1 for key in ("SONAME", "RPATH", "RUNPATH")):
        raise VerificationError("duplicate singleton ELF dynamic field")
    return {"format": formats[0], "needed": fields["NEEDED"],
            "soname": next(iter(fields["SONAME"]), None),
            "rpath": next(iter(fields["RPATH"]), None),
            "runpath": next(iter(fields["RUNPATH"]), None),
            "resolved_transitive_link_closure": None,
            "scope": "ELF direct dynamic entries only; object was not executed"}


def elf_metadata(path):
    path = Path(path).resolve(strict=True)
    with path.open("rb") as binary:
        if binary.read(4) != b"\x7fELF":
            raise VerificationError("expected an ELF artifact: " + str(path))
    return {"file": str(path), "sha256": sha256(path),
            **parse_elf_private_headers(command(["objdump", "-p", "--", str(path)]))}


def tool_metadata(executable, version_argument="--version"):
    resolved = shutil.which(executable)
    if resolved is None:
        raise VerificationError("required build tool is missing: " + executable)
    path = Path(resolved).resolve(strict=True)
    return {"executable": str(path), "sha256": sha256(path),
            "version_output": command([str(path), version_argument])}


def parse_pgrx_trace(text):
    versions = []
    paths = []
    pg17 = False
    for line in text.splitlines():
        if line.startswith("Generating bindings for pg"):
            pg17 = line == "Generating bindings for pg17"
        elif pg17 and line.startswith("Bindgen found "):
            versions.append(line.removeprefix("Bindgen found "))
        elif pg17 and line.startswith("found libclang at "):
            path = line.removeprefix("found libclang at ")
            if not Path(path).is_absolute() or any(ord(character) < 32 for character in path):
                raise VerificationError("invalid reported libclang path")
            paths.append(path)
    return {"loaded_version_outputs": sorted(set(versions)),
            "reported_loaded_library_paths": sorted(set(paths))}


def pgrx_trace(path):
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > MAX_COMMAND_BYTES:
        raise VerificationError("pgrx build trace exceeds the report budget")
    content = path.read_bytes()
    return {"file": str(path), "sha256": hashlib.sha256(content).hexdigest(),
            **parse_pgrx_trace(content.decode("utf-8", errors="strict"))}


def collect(args):
    packages = installed_packages(command([
        "dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Status}\n"
    ]))
    observed = "".join(row["package"] + "\t" + row["version"] + "\n" for row in packages)
    verified = verify_installed(args.expected_packages, observed)
    for row in packages:
        row["copyright"] = copyright_metadata(row["package"])
    license_statuses = Counter(row["copyright"]["status"] for row in packages)
    compiler = tool_metadata(args.cc)
    compiler["target"] = command([compiler["executable"], "-dumpmachine"])
    compiler["full_version"] = command([compiler["executable"], "-dumpfullversion"])
    pg = tool_metadata(args.pg_config)
    pg["configure_output"] = command([pg["executable"], "--configure"])
    bindings = [{"file": str(Path(path).resolve(strict=True)), "sha256": sha256(path)}
                for path in args.bindings_artifact]
    traces = [pgrx_trace(path) for path in args.pgrx_build_stderr]
    loaded_paths = sorted({path for trace in traces for path in trace["reported_loaded_library_paths"]})
    loaded_path = loaded_paths[0] if len(loaded_paths) == 1 else None
    return {
        "schema_version": 1,
        "status": "collected",
        "profile": args.profile,
        "collector_sha256": sha256(__file__),
        "package_verifier_sha256": sha256(Path(__file__).with_name("verify_native.py")),
        "native_package_verification": verified,
        "tools": {"cc": compiler, "ld": tool_metadata(args.ld), "pg_config": pg,
                  "objdump": tool_metadata("objdump")},
        "bindings": {
            "selected_libclang": loaded_path,
            "selection_status": "observed_in_supplied_pg17_build_trace" if loaded_path else
                                "multiple_paths_in_supplied_traces" if loaded_paths else "not_observed",
            "reason": "only pgrx's loaded-library diagnostic is path evidence; configuration is not selection",
            "configured_libclang_path": os.environ.get("LIBCLANG_PATH"),
            "installed_libclang_packages": [row["package"] for row in packages
                                             if row["package"].startswith("libclang")],
            "supplied_artifacts": bindings,
            "supplied_pgrx_build_traces": traces,
            "artifact_scope": "hashes of explicitly supplied files; no automatic build-profile attribution",
        },
        "elf": {"extension": elf_metadata(args.extension),
                "helper": elf_metadata(args.helper) if args.helper else None},
        "installed_packages": packages,
        "licenses": {
            "scope": "installed Debian package declarations, including build tools and inherited packages",
            "upstream_declared_license_inventory": True,
            "all_package_licenses_parsed": all(row["copyright"]["status"] == "parsed_dep5" for row in packages),
            "copyright_status_counts": dict(sorted(license_statuses.items())),
            "labels_are_uninterpreted_declarations": True,
            "spdx_normalization_performed": False,
            "distribution_review_complete": False,
            "p5_notices_complete": False,
        },
        "limits": {"selected_libclang_build_trace_observed": loaded_path is not None,
                   "resolved_link_closure_proven": False,
                   "copyright_file_byte_budget": MAX_METADATA_BYTES,
                   "machine_paths_are_ci_artifact_only": True},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-packages", required=True)
    parser.add_argument("--extension", required=True)
    parser.add_argument("--helper")
    parser.add_argument("--pg-config", required=True)
    parser.add_argument("--profile", choices=("direct", "direct-fault", "helper", "helper-fault"), required=True)
    parser.add_argument("--cc", default="cc")
    parser.add_argument("--ld", default="ld")
    parser.add_argument("--bindings-artifact", action="append", default=[])
    parser.add_argument("--pgrx-build-stderr", action="append", default=[])
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.profile.startswith("helper") != bool(args.helper):
        parser.error("helper profiles require --helper; direct profiles must omit it")
    try:
        report = collect(args)
        Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except (VerificationError, OSError, UnicodeError, subprocess.SubprocessError) as error:
        parser.exit(1, "native build report failed: " + str(error) + "\n")
    print(json.dumps({"status": report["status"], "packages": len(report["installed_packages"]),
                      "license_status_counts": report["licenses"]["copyright_status_counts"]}, sort_keys=True))


if __name__ == "__main__":
    main()
