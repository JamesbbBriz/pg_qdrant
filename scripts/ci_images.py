"""Retain bounded completed image families, with explicit pins and exact ownership."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

from ci_resources import docker, local_run_lock
from ci_cache import cache_root

IMAGE_RUN = "io.pg_qdrant.ci.image-run"
STATE = cache_root() / "images.json"
MAX_IMAGE_BYTES = 80 * 1024**3


def read_state(path):
    return json.loads(path.read_text()) if path.exists() else {"runs": [], "pins": []}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def pin_image(identifier, *, path=STATE, maximum=MAX_IMAGE_BYTES):
    """A caller explicitly reusing a compiled image keeps that exact ID."""
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", identifier):
        raise ValueError("pin requires an exact image ID")
    state = read_state(path)
    if identifier not in state["pins"]:
        state["pins"].append(identifier)
        if image_bytes(state) > maximum:
            raise RuntimeError("image pin exceeds byte budget; existing pins retained")
        save_state(path, state)


def retained_runs(runs):
    # Keep two successful build families for rollback and the latest failed build.
    keep = set()
    for status, count in (("passed", 2), ("failed", 1)):
        selected = sorted((run for run in runs if run["status"] == status),
                          key=lambda run: run["completed"], reverse=True)
        keep.update(run["run_id"] for run in selected[:count])
    return keep


def release_alias(alias, identifier):
    """Remove only the temporary tag created by a launcher, after its containers."""
    if not re.fullmatch(r"pgq-(preview|quality)-base-[a-f0-9]{16}", alias):
        raise ValueError("invalid temporary image alias")
    data = json.loads(docker("image", "inspect", alias))[0]
    if data["Id"] != identifier:
        raise RuntimeError("temporary alias changed; retain for review")
    docker("image", "rm", "--no-prune", alias, timeout=90)


def retire_images(state, save, *, retire_runs=()):
    receipt = {"removed": [], "retained": [], "errors": []}
    keep = retained_runs(state["runs"]) - set(retire_runs)
    for run in state["runs"]:
        if run["run_id"] in keep:
            continue
        for image in run["images"]:
            identifier = image["id"]
            if image.get("removed"):
                continue
            if identifier in state["pins"]:
                receipt["retained"].append({"id": identifier, "reason": "pinned"})
                continue
            try:
                # A timed-out Docker delete can finish in the daemon after its client exits.
                # A fresh inventory makes retry idempotent without guessing on inspect errors.
                present = docker("image", "ls", "-aq", "--no-trunc").split()
                if identifier not in present:
                    image["removed"] = True
                    receipt["removed"].append(identifier)
                    save()
                    continue
                data = json.loads(docker("image", "inspect", identifier))[0]
                if (data["Id"] != identifier or data["Config"].get("Labels", {}).get(IMAGE_RUN) != run["run_id"]
                        or sorted(data.get("RepoTags") or []) != image["tags"]):
                    raise RuntimeError("image ownership or tags changed; retain for review")
                references = docker("ps", "-aq", "--filter", "ancestor=" + identifier).split()
                if references:
                    receipt["retained"].append({"id": identifier, "reason": "container reference"})
                    continue
                # No force: Docker independently refuses concurrent references/multiple tags.
                docker("image", "rm", "--no-prune", identifier, timeout=90)
                image["removed"] = True
                receipt["removed"].append(identifier)
                save()
            except Exception as error:
                receipt["errors"].append({"id": identifier, "error": str(error)})
    return receipt


def image_bytes(state, active_run=None):
    """Conservative sum of unique full image sizes, including explicit pins.

    Shared Docker layers are counted more than once; this is not reclaimed space.
    Unregistered historical resources are outside automatic deletion authority.
    """
    identifiers = set(state["pins"])
    identifiers.update(image["id"] for run in state["runs"] for image in run["images"] if not image.get("removed"))
    if active_run:
        identifiers.update(docker("image", "ls", "-aq", "--no-trunc",
            "--filter", "label=" + IMAGE_RUN + "=" + active_run).split())
    present = set(docker("image", "ls", "-aq", "--no-trunc").split())
    return sum(json.loads(docker("image", "inspect", identifier))[0]["Size"]
               for identifier in identifiers if identifier in present)


def enforce_image_budget(*, path=STATE, reserve=0, maximum=MAX_IMAGE_BYTES, active_run=None):
    state = read_state(path)
    receipt = {"removed": [], "retained": [], "errors": []}
    save = lambda: save_state(path, state)
    for run in sorted(state["runs"], key=lambda item: item["completed"]):
        if image_bytes(state, active_run) + reserve <= maximum:
            break
        # The latest completed family remains available for diagnosis/rollback.
        if run is max(state["runs"], key=lambda item: item["completed"]):
            continue
        result = retire_images(state, save, retire_runs=(run["run_id"],))
        for key in receipt:
            receipt[key].extend(result[key])
    receipt.update(bytes=image_bytes(state, active_run), limit_bytes=maximum, reserved_bytes=reserve)
    if receipt["errors"] or receipt["bytes"] + reserve > maximum:
        raise RuntimeError("image byte budget exhausted; protected images retained: " + json.dumps(receipt))
    return receipt


def complete_images(run_id, status, *, path=STATE):
    """Call under the launcher lock, only after successful diagnostic retirement."""
    if not re.fullmatch(r"[a-f0-9]{16}", run_id) or status not in ("passed", "failed"):
        raise ValueError("invalid completed run")
    state = read_state(path)
    identifiers = sorted(set(docker("image", "ls", "-aq", "--no-trunc",
        "--filter", "label=" + IMAGE_RUN + "=" + run_id).split()))
    images = []
    for identifier in identifiers:
        data = json.loads(docker("image", "inspect", identifier))[0]
        if data["Config"].get("Labels", {}).get(IMAGE_RUN) != run_id:
            raise RuntimeError("image run label changed")
        images.append({"id": data["Id"], "tags": sorted(data.get("RepoTags") or [])})
    if images and not any(run["run_id"] == run_id for run in state["runs"]):
        state["runs"].append({"run_id": run_id, "status": status,
                              "completed": time.time(), "images": images})
    save = lambda: save_state(path, state)
    save()
    receipt = retire_images(state, save)
    receipt["capacity"] = enforce_image_budget(path=path)
    return receipt


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "pin", "unpin"))
    parser.add_argument("--image", help="exact image ID or an existing local tag")
    args = parser.parse_args()
    with local_run_lock():
        state = read_state(STATE)
        if args.action != "status":
            if not args.image:
                parser.error("--image is required")
            identifier = json.loads(docker("image", "inspect", args.image))[0]["Id"]
            if args.action == "pin" and identifier not in state["pins"]:
                pin_image(identifier)
                state = read_state(STATE)
            elif args.action == "unpin" and identifier in state["pins"]:
                state["pins"].remove(identifier)
            save_state(STATE, state)
        print(json.dumps(state, indent=2))
