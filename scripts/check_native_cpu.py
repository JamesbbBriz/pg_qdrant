#!/usr/bin/env python3
"""Audit the fixed Edge C objects used by the conservative P0 CPU policy."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def inspect_object(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Expected a bounded regular native object")
    result = subprocess.run(
        ["objdump", "-d", "-M", "intel", "--no-show-raw-insn", str(path)],
        capture_output=True, text=True, timeout=15, check=True,
    )
    if len(result.stdout.encode()) > 4 * 1024 * 1024:
        raise ValueError("Native disassembly exceeds the audit budget")
    instructions = []
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*[0-9a-f]+:\s+([a-z][a-z0-9]*)\s*(.*)$", line)
        if match:
            instructions.append((match.group(1), match.group(2)))
    if not instructions:
        raise ValueError("No instructions decoded from the native object")
    hle_rtm = {"xacquire", "xrelease", "xbegin", "xend", "xabort", "xtest"}
    unsupported = [mnemonic for mnemonic, operands in instructions
                   if mnemonic in hle_rtm or re.search(r"\b(?:zmm\d+|k[0-7])\b", operands)]
    if unsupported:
        raise ValueError("Unexpected TSX or AVX-512 code requires a new CPU decision")
    vector_instructions = sorted({mnemonic for mnemonic, _ in instructions
                                  if mnemonic.startswith("v")})
    if not vector_instructions:
        raise ValueError("Expected fixed-release VEX vector code was not observed")
    # Hash normalized decoded instructions, not objdump's machine-local filename.
    decoded = "\n".join(f"{op} {args}" for op, args in instructions) + "\n"
    return {
        "object_name": path.name,
        "object_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "decoded_instructions_sha256": hashlib.sha256(decoded.encode()).hexdigest(),
        "instructions_decoded": len(instructions),
        "vector_mnemonics": vector_instructions,
        "hle_rtm_instructions": 0,
        "avx512_register_operands": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path, default=Path("target/debug/build"))
    args = parser.parse_args()
    directories = sorted(path for path in args.build_root.glob("qdrant-edge-*/out")
                         if list(path.glob("*-sse.o")))
    if not 1 <= len(directories) <= 8:
        raise ValueError("Expected one to eight selected Edge native build directories")
    records = []
    for directory in directories:
        sse = list(directory.glob("*-sse.o"))
        avx2 = list(directory.glob("*-avx2.o"))
        if len(sse) != 1 or len(avx2) != 1:
            raise ValueError("Each Edge native build requires one SSE and one AVX2 object")
        records.append({"sse": inspect_object(sse[0]), "avx2": inspect_object(avx2[0])})
    compiler = subprocess.check_output(["cc", "-dumpfullversion"], text=True, timeout=5).strip()
    disassembler = subprocess.check_output(["objdump", "--version"], text=True, timeout=5).splitlines()[0]
    print(json.dumps({
        "schema_version": 1, "kind": "p0_edge_native_cpu_object_audit", "status": "passed",
        "edge_version": "0.8.0", "compiler_version": compiler,
        "disassembler_version": disassembler, "objects": records,
        "hle_cpu_requirement": False,
        "hle_disposition": "No HLE/RTM instructions in either selected native source object; repeat after every native or upstream build change.",
        "generic_x86_64_compatibility": False,
        "complete_instruction_isa_proof": False,
        "scope": "Checks the exact two C objects and the policy's TSX exclusion. Runtime CPU admission and clean integration are separate checks; this does not prove every dependency's ISA or broader platform support.",
    }))


if __name__ == "__main__":
    main()
