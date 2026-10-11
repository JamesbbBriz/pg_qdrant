"""Measure real native compiler/cache paths inside the local act runner."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import re

import ci_build
from ci_cache import digest
from ci_resources import MANAGED, docker

ROOT = Path(__file__).resolve().parents[1]


def native_metrics(log):
    text = re.sub(r"\x1b\[[0-9;]*m", "", log)
    layer = re.search(r"^(#\d+) .* RUN --mount=.*target=/src/target", text, flags=re.M)
    if not layer:
        return {"native_layer_cached": False, "native_seconds": None,
                "compile_install_seconds": None, "compiled_crates": []}
    prefix = re.escape(layer[1])
    cached = bool(re.search(r"^" + prefix + r" CACHED$", text, flags=re.M))
    elapsed = re.search(r"^" + prefix + r" DONE ([\d.]+)s$", text, flags=re.M)
    installed = re.search(r"^" + prefix + r" ([\d.]+)\s+Finished installing pg_qdrant$", text, flags=re.M)
    return {"native_layer_cached": cached,
            "native_seconds": 0.0 if cached else float(elapsed[1]) if elapsed else None,
            "compile_install_seconds": 0.0 if cached else float(installed[1]) if installed else None,
            "compiled_crates": re.findall(r"^" + prefix + r" [\d.]+\s+Compiling ([^\n]+)", text, flags=re.M)}


def compilation_recipe(dockerfile):
    # Artifact export and later auditing are timed separately from compiler work.
    text = dockerfile.replace("\r\n", "\n")
    match = re.search(r"RUN --mount=type=cache,id=\$\{PGQ_TARGET_CACHE\}.*?--cargo=--locked", text, re.S)
    if not match:
        raise ValueError("native compilation recipe missing")
    return hashlib.sha256(match[0].encode()).hexdigest()


def main():
    run_id = os.environ["PGQ_RUN_ID"]
    selected = os.environ.get("PGQ_COMPILER", "llvm-1.96")
    candidates = json.loads((ROOT / "ci/compiler-candidates.json").read_text())["candidates"]
    candidate = next(value for value in candidates if value["name"] == selected)
    directory = ROOT / "artifacts/compiler-benchmark"
    directory.mkdir(parents=True)
    context = directory / "input"
    shutil.copytree(ROOT, context, ignore=shutil.ignore_patterns("artifacts", ".git", "__pycache__"))
    tools = os.environ.get("PGQ_BENCHMARK_TOOLS", "pg-qdrant-build-tools:1.96")
    report = {"status": "running", "candidate": candidate, "runs": [], "sql": [],
              "snapshot": json.loads((ROOT / "ci-input.json").read_text()),
              "qualification": "pending_full_regression", "release_supported": False}
    report_path = directory / "report.json"
    report["tools_image_id"] = json.loads(docker("image", "inspect", tools))[0]["Id"]
    report["native_inputs"] = {item["path"]: item["sha256"] for item in report["snapshot"]["files"]
        if item["path"].startswith("crates/") or item["path"] in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml")}
    report["native_recipe_sha256"] = hashlib.sha256((ROOT / "Dockerfile.p0").read_bytes().split(b"COPY . .", 1)[0]).hexdigest()
    report["compilation_recipe_sha256"] = compilation_recipe((ROOT / "Dockerfile.p0").read_text())
    report["builder_limits"] = {"cpus": 2, "memory_bytes": 8 * 1024**3, "cargo_jobs": 2, "incremental": False}
    image = "pgq-compiler-" + selected + "-" + run_id
    compiler_image = image + "-tools"
    if candidate["toolchain"] != "1.96.0":
        started = time.monotonic()
        log = directory / "compiler-install.log"
        def install(arguments):
            with log.open("wb") as output:
                return subprocess.run(arguments, stdout=output, stderr=subprocess.STDOUT).returncode
        code = 1
        try:
            code = ci_build.run(["docker", "build", "-f", "ci/Dockerfile.compiler", "--build-arg", "INPUT_IMAGE=" + tools,
                "--build-arg", "PGQ_TOOLCHAIN=" + candidate["toolchain"], "--build-arg",
                "PGQ_RUSTFLAGS=" + candidate["rustflags"], "--build-arg",
                "PGQ_CRANELIFT=" + str(int(candidate["cranelift"])), "--label", "io.pg_qdrant.ci.image-run=" + run_id,
                "-t", compiler_image, str(context)], install)
        except BaseException as error:
            report.update(status="failed", reason=str(error))
            raise
        finally:
            report["compiler_install"] = {"exit_code": code, "seconds": round(time.monotonic() - started, 3),
                "log": log.name, "log_sha256": digest(log) if log.exists() else None}
            report_path.write_text(json.dumps(report, indent=2) + "\n")
        if code:
            report.update(status="failed", reason="compiler installation or compatibility probe failed")
            report_path.write_text(json.dumps(report, indent=2))
            return code
        tools = compiler_image
    report["compiler_image_id"] = json.loads(docker("image", "inspect", tools))[0]["Id"]
    protocol = context / "crates/pg_qdrant-protocol/src/lib.rs"
    original = protocol.read_bytes()
    namespace = "pgq-bench-" + selected + "-" + run_id
    report["builds_include"] = "native engine/protocol/helper tests plus PostgreSQL extension installation"
    try:
        if candidate["toolchain"] == "1.96.0":
            container = docker("create", "--network=none", "--memory=1g", "--memory-swap=1g", "--cpus=2",
                "--label", MANAGED + "=1", "--label", "io.pg_qdrant.ci=" + run_id,
                "--env", "RUSTUP_TOOLCHAIN=" + candidate["toolchain"],
                "--env", "RUSTFLAGS=" + candidate["rustflags"], tools, "bash", "-euc",
                'rustc -Vv; cargo -V; cargo pgrx --version; '
                'rustc ${RUSTFLAGS} /tmp/pgq-compiler-probe.rs -o /tmp/pgq-compiler-probe; /tmp/pgq-compiler-probe')
            docker("cp", str(context / "ci/compiler-probe.rs"), container + ":/tmp/pgq-compiler-probe.rs")
            log = directory / "compiler-probe.log"
            with log.open("wb") as output:
                result = subprocess.run(["docker", "start", "--attach", container], stdout=output, stderr=subprocess.STDOUT)
            report["compiler_probe"] = {"exit_code": result.returncode, "container": container,
                "log": log.name, "log_sha256": digest(log)}
            if result.returncode:
                report.update(status="failed", reason="compiler compatibility probe failed")
                return 1
        for step in ("cold_1", "cold_2", "warm_1", "warm_2", "docs_only", "rust_change"):
            if step == "docs_only":
                with (context / "README.md").open("a") as stream:
                    stream.write("\nCompiler benchmark documentation input.\n")
            if step == "rust_change":
                protocol.write_bytes(original + b"\npub const PGQ_COMPILER_BENCHMARK: u8 = 1;\n")
            cache_id = namespace + ("-1" if step == "cold_1" else "-2")
            command = ["docker", "build", "-f", str(context / "Dockerfile.p0"),
                "--build-arg", "PG_QDRANT_BUILD_TOOLS=" + tools,
                "--build-arg", "PGQ_TARGET_CACHE=" + cache_id,
                "--build-arg", "PGQ_RUSTFLAGS=" + candidate["rustflags"],
                "--build-arg", "P0_HELPER=1", "--build-arg",
                "PG_QDRANT_SOURCE_SHA=" + report["snapshot"]["commit"],
                "--label", "io.pg_qdrant.ci.image-run=" + run_id, "-t", image, str(context)]
            if step != "rust_change":
                command.insert(2, "--output=type=cacheonly")
            started = time.monotonic()
            log = directory / (step + ".log")
            def execute(arguments):
                # Keep complete BuildKit/Cargo output for cache/freshness inspection.
                with log.open("wb") as output:
                    return subprocess.run(arguments, stdout=output, stderr=subprocess.STDOUT).returncode
            code = ci_build.run(command, execute)
            row = {"kind": step, "exit_code": code, "seconds": round(time.monotonic() - started, 3),
                   "log": log.name, "log_sha256": digest(log), "target_cache": cache_id,
                   "rust_input_sha256": digest(protocol), "readme_sha256": digest(context / "README.md")}
            row.update(native_metrics(log.read_text(errors="replace")))
            workspace = {"pg-qdrant-protocol", "pg-qdrant-edge-probe", "pg-qdrant-helper", "pg_qdrant"}
            row["dependency_crates_recompiled"] = [name for name in row["compiled_crates"]
                if name.split()[0] not in workspace]
            report["runs"].append(row)
            report_path.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(row), flush=True)
            if code:
                report.update(status="failed", reason="native build or audit failed")
                return code
            if step in ("warm_1", "warm_2", "docs_only") and not row["native_layer_cached"]:
                report.update(status="failed", reason="unchanged native inputs missed their verified build layer")
                return 1
            if step.startswith("cold_") and (row["native_layer_cached"] or not row["dependency_crates_recompiled"]):
                report.update(status="failed", reason="empty-target build did not compile locked dependencies")
                return 1
            if step == "rust_change" and row["dependency_crates_recompiled"]:
                report.update(status="failed", reason="source change lost the reusable dependency artifacts")
                return 1
            if step == "rust_change":
                row["image_id"] = json.loads(docker("image", "inspect", image))[0]["Id"]
        # Measure compiler candidates without duplicating the complete recovery
        # workload. The selected default still requires the full product gates.
        for name, script in (("p0", "run-p0.sh"),):
            container = docker("create", "--network=none", "--memory=5g", "--memory-swap=5g", "--cpus=2",
                "--label", MANAGED + "=1", "--label", "io.pg_qdrant.ci=" + run_id,
                image, "bash", "crates/pg_qdrant/tests/" + script)
            log = directory / (name + ".log")
            with log.open("wb") as output:
                result = subprocess.run(["docker", "start", "--attach", container], stdout=output, stderr=subprocess.STDOUT)
            report["sql"].append({"kind": name, "container": container, "exit_code": result.returncode,
                                  "log": log.name, "log_sha256": digest(log)})
            if result.returncode:
                report.update(status="failed", reason="SQL qualification failed")
                return 1
        report["status"] = "measured"
        return 0
    except BaseException as error:
        report.update(status="failed", reason=str(error))
        raise
    finally:
        protocol.write_bytes(original)
        report_path.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
