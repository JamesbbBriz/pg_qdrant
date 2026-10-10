"""Retain bounded completed image families, with explicit pins and exact ownership."""
from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import time

from ci_resources import docker, local_run_lock

IMAGE_RUN = "io.pg_qdrant.ci.image-run"
STATE = Path(tempfile.gettempdir()) / "pg-qdrant-ci-images.json"


def read_state(path):
    return json.loads(path.read_text()) if path.exists() else {"runs": [], "pins": []}


def save_state(path, state):
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def pin_image(identifier, *, path=STATE):
    """A caller explicitly reusing a compiled image keeps that exact ID."""
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", identifier):
        raise ValueError("pin requires an exact image ID")
    state = read_state(path)
    if identifier not in state["pins"]:
        state["pins"].append(identifier)
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


def retire_images(state, save):
    receipt = {"removed": [], "retained": [], "errors": []}
    keep = retained_runs(state["runs"])
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
    return retire_images(state, save)


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
                state["pins"].append(identifier)
            elif args.action == "unpin" and identifier in state["pins"]:
                state["pins"].remove(identifier)
            save_state(STATE, state)
        print(json.dumps(state, indent=2))
