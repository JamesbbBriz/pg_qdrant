"""Byte-bounded, verified local caches; callers hold the local CI run lock."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import time

GiB = 1024**3
OWNER = "pg-qdrant-local-cache-v1"


def cache_root():
    return Path(os.environ.get("PGQ_CACHE_ROOT", str(Path(os.environ.get(
        "LOCALAPPDATA", str(Path.home() / ".cache"))) / "pg-qdrant" / "cache-v1")))


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_child(root, relative):
    """Never follow a link/junction or accept an absolute/traversing cache path."""
    part = PurePosixPath(relative)
    if (not relative or part.is_absolute() or ".." in part.parts or "\\" in relative
            or ":" in relative or str(part) != relative):
        raise ValueError("unsafe cache path")
    candidate = root
    for component in part.parts:
        candidate /= component
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise ValueError("cache links are unsupported")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("cache path escaped its root")
    return candidate


def inventory(root):
    files = {}
    for directory, folders, names in os.walk(root, followlinks=False):
        for name in folders + names:
            path = checked_child(root, (Path(directory) / name).relative_to(root).as_posix())
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path.stat().st_size
    return files


class Store:
    """Payload bytes and entry counts are hard admission limits, including staging.

    An interrupted write remains visible and blocks reuse until explicitly reviewed.
    Unknown files, changed receipts and pins never authorize destructive cleanup.
    """
    def __init__(self, bucket, limit, entries, root=None):
        if bucket not in ("snapshots", "oci") or limit <= 0 or entries <= 0:
            raise ValueError("invalid cache policy")
        parent = Path(root) if root else cache_root()
        for component in [parent, *parent.parents]:
            if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
                raise ValueError("cache ancestors cannot be links")
        self.root = parent / bucket
        self.limit, self.entries = limit, entries
        if self.root.is_symlink() or (hasattr(self.root, "is_junction") and self.root.is_junction()):
            raise ValueError("cache root cannot be a link")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.json"
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("owner") != OWNER:
                raise ValueError("foreign cache owner")
        else:
            if any(self.root.iterdir()):
                raise ValueError("refusing to adopt nonempty cache directory")
            self.state = {"owner": OWNER, "entries": {}}
            self.save()
        self.audit()

    def save(self):
        temporary = self.root / "state.pending"
        temporary.write_text(json.dumps(self.state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def audit(self):
        expected = {"state.json"}
        for key, item in self.state["entries"].items():
            if not re.fullmatch(r"[a-f0-9]{64}", key):
                raise ValueError("invalid cache key")
            expected.update(key + "/" + name for name in item["files"])
        actual = inventory(self.root)
        if set(actual) != expected:
            raise RuntimeError("unrecorded or missing cache data; retain for review")
        for key, item in self.state["entries"].items():
            if any(actual[key + "/" + name] != record["bytes"] for name, record in item["files"].items()):
                raise RuntimeError("cache data size changed; retain for review")
        return sum(size for name, size in actual.items() if name != "state.json")

    def verify(self, key):
        item = self.state["entries"].get(key)
        if item is None:
            return None
        for name, record in item["files"].items():
            if digest(checked_child(self.root, key + "/" + name)) != record["sha256"]:
                raise RuntimeError("cache checksum changed; retain for review")
        item["used"] = time.time()
        self.save()
        return self.root / key

    def reserve(self, size, protect=(), new_entry=True):
        if size < 0 or size > self.limit:
            raise RuntimeError("cache entry exceeds byte budget")
        used = self.audit()
        removable = sorted(self.state["entries"], key=lambda k: self.state["entries"][k]["used"])
        removed = []
        for key in removable:
            if used + size <= self.limit and len(self.state["entries"]) + int(new_entry) <= self.entries:
                break
            item = self.state["entries"][key]
            if item.get("pinned") or key in protect:
                continue
            self.verify(key)
            target = checked_child(self.root, key)
            count = sum(record["bytes"] for record in item["files"].values())
            shutil.rmtree(target)
            del self.state["entries"][key]
            self.save()
            used -= count
            removed.append(key)
        if used + size > self.limit or len(self.state["entries"]) + int(new_entry) > self.entries:
            raise RuntimeError("protected cache data fills budget; no new build admitted")
        return removed

    @contextmanager
    def write(self, key, maximum, protect=()):
        if not re.fullmatch(r"[a-f0-9]{64}", key) or key in self.state["entries"]:
            raise ValueError("invalid or existing cache entry")
        self.reserve(maximum, protect=protect)
        pending = self.root / (key + ".pending")
        pending.mkdir()
        try:
            yield pending
            files = inventory(pending)
            if sum(files.values()) > maximum:
                raise RuntimeError("cache write exceeded reserved byte budget")
            record = {name: {"bytes": size, "sha256": digest(pending / name)} for name, size in files.items()}
            pending.rename(self.root / key)
            self.state["entries"][key] = {"used": time.time(), "pinned": False, "files": record}
            self.save()
            self.audit()
        except BaseException:
            # Only this call's checked staging directory, never an old unknown entry.
            if pending.exists():
                shutil.rmtree(checked_child(self.root, pending.name))
            raise

    def pin(self, key, value=True):
        self.verify(key)
        self.state["entries"][key]["pinned"] = value
        self.save()


def archive_snapshot(source, report):
    store = Store("snapshots", 2 * GiB, 32)
    key = report["source_sha256"]
    cached = store.verify(key)
    hit = cached is not None
    if cached is None:
        size = sum((source / item["path"]).stat().st_size for item in report["files"])
        maximum = size + len(report["files"]) * 2048 + 65536
        with store.write(key, maximum) as destination:
            with tarfile.open(destination / "source.tar.gz", "w:gz") as archive:
                for item in report["files"]:
                    path = checked_child(source, item["path"])
                    if digest(path) != item["sha256"]:
                        raise RuntimeError("snapshot changed before archiving")
                    archive.add(path, arcname=item["path"], recursive=False)
        cached = store.verify(key)
    return {"key": key, "archive_sha256": digest(cached / "source.tar.gz"),
            "bytes": (cached / "source.tar.gz").stat().st_size, "cache_hit": hit}


def finish_snapshot(source, report, passed):
    """Retire only a verified source view; preserve manifests and failed sources."""
    archived = archive_snapshot(source, report)
    store = Store("snapshots", 2 * GiB, 32)
    dirty = (report.get("dirty_diff_sha256", hashlib.sha256(b"").hexdigest()) != hashlib.sha256(b"").hexdigest()
             or bool(report.get("untracked_inputs")))
    if not passed or dirty:
        store.pin(report["source_sha256"])
    if passed:
        actual = inventory(source)
        expected = {item["path"] for item in report["files"]} | {"ci-input.json"}
        if set(actual) == expected:
            for item in report["files"]:
                path = checked_child(source, item["path"])
                if digest(path) != item["sha256"]:
                    raise RuntimeError("completed source snapshot changed")
            for item in report["files"]:
                checked_child(source, item["path"]).unlink()
            archived["materialized_sources_retired"] = True
    return archived


def evidence_admission(directory, maximum=8 * GiB):
    directory.mkdir(parents=True, exist_ok=True)
    size = sum(inventory(directory).values())
    if size >= maximum:
        raise RuntimeError("local evidence budget exhausted; archive/review evidence before another run")
    return {"bytes": size, "limit_bytes": maximum}


def restore_snapshot(manifest, destination):
    for component in [destination, *destination.parents]:
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise ValueError("restore destination ancestors cannot be links")
    report = json.loads(manifest.read_text(encoding="utf-8"))
    files = report["files"]
    if hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != report["source_sha256"]:
        raise ValueError("snapshot manifest hash mismatch")
    store = Store("snapshots", 2 * GiB, 32)
    entry = store.verify(report["source_sha256"])
    if entry is None:
        raise FileNotFoundError("snapshot archive no longer retained")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("restore destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    expected = {item["path"]: item["sha256"] for item in files}
    seen = set()
    with tarfile.open(entry / "source.tar.gz") as archive:
        for member in archive:
            if not member.isfile() or member.name not in expected or member.name in seen:
                raise ValueError("unexpected snapshot archive member")
            target = checked_child(destination, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as body, target.open("xb") as output:
                shutil.copyfileobj(body, output)
            if digest(target) != expected[member.name]:
                raise ValueError("restored source checksum mismatch")
            target.chmod(member.mode & 0o777)
            seen.add(member.name)
    if seen != set(expected):
        raise ValueError("snapshot archive incomplete")
    (destination / "ci-input.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import argparse
    from ci_resources import local_run_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "trim", "pin", "unpin", "restore"))
    parser.add_argument("--bucket", choices=("snapshots", "oci"), default="snapshots")
    parser.add_argument("--key")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    with local_run_lock():
        if args.action == "restore":
            if not args.manifest or not args.destination:
                parser.error("restore requires --manifest and --destination")
            restore_snapshot(args.manifest, args.destination)
        else:
            store = Store(args.bucket, (2 if args.bucket == "snapshots" else 16) * GiB,
                          32 if args.bucket == "snapshots" else 16)
            if args.action in ("pin", "unpin"):
                if not args.key or args.key not in store.state["entries"]:
                    parser.error("an existing exact --key is required")
                store.pin(args.key, args.action == "pin")
            removed = store.reserve(0, new_entry=False) if args.action == "trim" else []
            print(json.dumps({"bytes": store.audit(), "limit_bytes": store.limit,
                "removed": removed, "entries": store.state["entries"]}, indent=2))
