"""Reuse native builds in a dedicated, bounded BuildKit cache."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tomllib

from ci_cache import GiB, Store, cache_root, checked_child
from ci_resources import docker

ROOT = Path(__file__).resolve().parents[1]
BUILDER = "pg-qdrant-bounded-v1"
NODE = BUILDER + "0"
CONTAINER = "buildx_buildkit_" + NODE
BUILDKIT = "moby/buildkit:v0.34.0@sha256:b059f8d7226d0b326bc871af489a5dddf65e36af4d20d14251b072974d9ee87c"
MAXIMUM = 24 * GiB
RESERVE = 40 * GiB


def ensure_builder():
    config = ROOT / "ci/buildkitd.toml"
    present = subprocess.run(["docker", "buildx", "inspect", BUILDER], capture_output=True, timeout=30)
    if present.returncode:
        subprocess.run(["docker", "buildx", "create", "--name", BUILDER, "--node", NODE,
            "--driver", "docker-container", "--buildkitd-config", str(config),
            "--driver-opt", "image=" + BUILDKIT, "--driver-opt", "memory=8g",
            "--driver-opt", "memory-swap=8g", "--driver-opt", "cpu-period=100000",
            "--driver-opt", "cpu-quota=200000", "--driver-opt", "restart-policy=no"], check=True, timeout=45)
    subprocess.run(["docker", "buildx", "inspect", BUILDER, "--bootstrap"], check=True, timeout=180)
    data = json.loads(docker("inspect", CONTAINER))[0]
    policy = data["HostConfig"]
    if (data["Config"]["Image"] != BUILDKIT or policy["Memory"] != 8 * GiB
            or policy["MemorySwap"] != 8 * GiB or policy["CpuPeriod"] != 100000
            or policy["CpuQuota"] != 200000 or policy["RestartPolicy"]["Name"] != "no"):
        raise RuntimeError("dedicated builder identity or resource policy changed")
    observed = docker("exec", CONTAINER, "cat", "/etc/buildkit/buildkitd.toml")
    # buildx may normalize line endings but must retain this exact policy.
    if tomllib.loads(observed) != tomllib.loads(config.read_text()):
        raise RuntimeError("dedicated builder GC configuration changed")
    return data["Id"]


def cache_usage():
    # buildx's JSON formatter rounds Size into display strings. buildctl returns
    # the daemon's integer byte counts, including mutable target/registry mounts.
    records = json.loads(docker("exec", CONTAINER, "buildctl", "du", "--format", "{{json .}}", timeout=60))
    if any(type(record.get("size")) is not int or record["size"] < 0 for record in records):
        raise ValueError("invalid builder byte measurement")
    return sum(record["size"] for record in records)


def maintain_builder():
    ensure_builder()
    subprocess.run(["docker", "buildx", "prune", "--builder", BUILDER, "--force",
        "--max-used-space", str(MAXIMUM), "--min-free-space", str(RESERVE),
        "--reserved-space", str(2 * GiB)], check=True, timeout=180)
    size = cache_usage()
    free = int(docker("exec", CONTAINER, "df", "-Pk", "/var/lib/buildkit").splitlines()[-1].split()[3]) * 1024
    if size > MAXIMUM or free < RESERVE:
        raise RuntimeError("native cache or Docker free-space budget exhausted")
    return {"builder": BUILDER, "cache_bytes": size, "limit_bytes": MAXIMUM, "free_bytes": free}


def oci_manifest(layout, identifier):
    """Resolve linux/amd64 to an inspected config, manifest or image-index ID."""
    index = json.loads((layout / "index.json").read_text())
    def visit(descriptor, anchored=False):
        value = descriptor["digest"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
            raise ValueError("invalid OCI descriptor digest")
        raw = (layout / "blobs/sha256" / value[7:]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != value[7:] or len(raw) != descriptor["size"]:
            raise ValueError("OCI descriptor content mismatch")
        item = json.loads(raw)
        anchored = anchored or value == identifier
        if "manifests" in item:
            for child in item["manifests"]:
                result = visit(child, anchored)
                if result:
                    return result
        elif "config" in item and (anchored or item["config"]["digest"] == identifier):
            for child in [item["config"], *item["layers"]]:
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", child["digest"]):
                    raise ValueError("invalid OCI blob digest")
                blob = checked_child(layout, "blobs/sha256/" + child["digest"][7:])
                if blob.stat().st_size != child["size"]:
                    raise ValueError("missing or truncated OCI blob")
            config = json.loads((layout / "blobs/sha256" / item["config"]["digest"][7:]).read_text())
            if config["architecture"] != "amd64" or config["os"] != "linux":
                return None
            return value
        return None
    for descriptor in index["manifests"]:
        result = visit(descriptor)
        if result:
            return result
    raise ValueError("saved image does not match inspected local image")


def save_oci(image, store, protect=()):
    data = json.loads(docker("image", "inspect", image))[0]
    identifier = data["Id"]
    key = identifier.removeprefix("sha256:")
    layout = store.verify(key)
    hit = layout is not None
    if layout is None:
        maximum = data["Size"] + 64 * 1024**2
        # Admission accounts for the entire in-progress export before opening it.
        store.reserve(maximum, protect=protect)
        with store.write(key, maximum, protect=protect) as destination:
            process = subprocess.Popen(["docker", "save", identifier], stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
            try:
                written = 0
                with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                    for member in archive:
                        name = member.name
                        if member.isdir() and name in ("blobs", "blobs/sha256"):
                            continue
                        if name == "manifest.json":
                            continue  # Docker's redundant compatibility manifest
                        if (not member.isfile() or not (name in ("index.json", "oci-layout")
                                or re.fullmatch(r"blobs/sha256/[a-f0-9]{64}", name))):
                            raise ValueError("unsupported or unsafe saved OCI member")
                        written += member.size
                        if written > maximum:
                            raise RuntimeError("OCI export exceeded its reserved byte budget")
                        target = checked_child(destination, name)
                        if target.exists():
                            raise ValueError("duplicate OCI member")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        sha = hashlib.sha256()
                        with archive.extractfile(member) as body, target.open("xb") as output:
                            count = 0
                            for chunk in iter(lambda: body.read(1024**2), b""):
                                output.write(chunk)
                                sha.update(chunk)
                                count += len(chunk)
                        if count != member.size or (name.startswith("blobs/") and sha.hexdigest() != target.name):
                            raise ValueError("saved OCI blob checksum mismatch")
                if process.wait(timeout=30):
                    raise RuntimeError("local OCI export failed")
                oci_manifest(destination, identifier)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
        layout = store.verify(key)
    manifest = oci_manifest(layout, identifier)
    return layout, manifest, {"image_id": identifier, "manifest": manifest, "cache_hit": hit}


def prepare(command):
    if command[:2] != ["docker", "build"]:
        raise ValueError("only docker build inputs are accepted")
    from ci_images import enforce_image_budget
    enforce_image_budget(reserve=12 * GiB, active_run=os.environ.get("PGQ_RUN_ID"))
    maintain_builder()
    arguments = command[2:]
    store = Store("oci", 16 * GiB, 16)
    contexts, receipts, protected = [], [], []
    for index, argument in enumerate(arguments[:-1]):
        if argument == "--build-arg":
            key, separator, value = arguments[index + 1].partition("=")
            if separator and (key.endswith("IMAGE") or key in ("PGQ_QUALITY_BASE", "PG_QDRANT_BUILD_TOOLS")):
                layout, manifest, receipt = save_oci(value, store, protect=protected)
                alias = "pgq_base_" + str(len(receipts))
                arguments[index + 1] = key + "=" + alias
                contexts += ["--build-context", alias + "=oci-layout://" + layout.as_posix() + "@" + manifest]
                receipts.append(receipt)
                protected.append(receipt["image_id"][7:])
    output = [] if "--output=type=cacheonly" in arguments else ["--load"]
    return ["docker", "buildx", "build", "--builder", BUILDER, *output,
            "--progress=plain", *contexts, *arguments], receipts


def run(command, execute):
    code = 1
    report = {"builder": BUILDER, "requested_command": command}
    prepared = False
    try:
        built, inputs = prepare(command)
        report.update(inputs=inputs, command=built)
        prepared = True
        code = execute(built)
        return code
    except BaseException as error:
        report["error"] = str(error)
        raise
    finally:
        report["exit_code"] = code
        try:
            if prepared:
                report["cache_after"] = maintain_builder()
        except BaseException as error:
            report.update(maintenance_error=str(error), exit_code=1)
            raise
        finally:
            path = ROOT / "artifacts/build-cache"
            path.mkdir(parents=True, exist_ok=True)
            (path / (hashlib.sha256(json.dumps(command).encode()).hexdigest()[:16] + ".json")).write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8")


def act_cache_options():
    root = cache_root().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return (' --mount "type=bind,source=' + root.as_posix() + ',target=/pgq-cache"',
            ["--env", "PGQ_CACHE_ROOT=/pgq-cache"])


if __name__ == "__main__":
    import sys
    from retry_snapshot_build import run as execute
    raise SystemExit(run(sys.argv[1:], execute))
