#!/usr/bin/env python3
"""Prepare deterministic bounded IR subsets from pinned, hash-verified inputs."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import urllib.request

from quality_metrics import select_documents, stable

ROOT = Path(__file__).resolve().parents[1]


def verified_file(directory, record, download=False):
    target = directory / record["local_name"]
    if target.name != record["local_name"]:
        raise ValueError("source filenames must be flat")
    if not target.exists() and download:
        temporary = target.with_suffix(target.suffix + ".partial")
        try:
            with urllib.request.urlopen(record["origin"], timeout=45) as response, temporary.open("wb") as sink:
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > record["bytes"]:
                        raise ValueError("download exceeds pinned size")
                    sink.write(block)
            data = temporary.read_bytes()
            if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("download hash/size mismatch")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    data = target.read_bytes()
    if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise ValueError("cached source hash/size mismatch: " + target.name)
    return target


def prepare(config, source, output, download=False):
    import pyarrow.parquet as pq
    source.mkdir(parents=True, exist_ok=True)
    manifest = config["manifest"]
    for record in manifest["files"]:
        verified_file(source, record, download)
    manifest_raw = json.dumps(manifest, indent=2).encode("utf-8")
    (source / "manifest.json").write_bytes(manifest_raw)
    qrel_path = source / config["qrels"]
    if qrel_path.suffix == ".tsv":
        with qrel_path.open(encoding="utf-8") as stream:
            qrels = list(csv.DictReader(stream, delimiter="\t"))
    else:
        qrels = pq.read_table(qrel_path).to_pylist()
    relevance = {}
    for row in qrels:
        query, doc, score = str(row["query-id"]), str(row["corpus-id"]), int(row["score"])
        if score > 0:
            if score > 4:
                raise ValueError("unsupported relevance grade")
            previous = relevance.setdefault(query, {}).get(doc)
            if previous is not None and previous != score:
                raise ValueError("conflicting judgments")
            relevance[query][doc] = score
    selected = sorted(relevance, key=stable)[:32]
    if len(selected) != 32:
        raise ValueError("32 judged queries are required")
    queries = {}
    for batch in pq.ParquetFile(source / config["queries"]).iter_batches(batch_size=1024):
        for row in batch.to_pylist():
            query = str(row["_id"])
            if query in selected:
                if query in queries:
                    raise ValueError("duplicate selected query")
                queries[query] = row["text"]
    if set(queries) != set(selected):
        raise ValueError("missing selected query")
    required = {doc for query in selected for doc in relevance[query]}
    corpus = pq.ParquetFile(source / config["corpus"])
    ids = [str(row["_id"]) for batch in corpus.iter_batches(batch_size=1024, columns=["_id"])
           for row in batch.to_pylist()]
    if len(ids) != len(set(ids)) or not required <= set(ids) or len(required) > 500:
        raise ValueError("invalid corpus identity/positive-document set")
    preferred = required | set(sorted(set(ids) - required, key=stable)[:500 - len(required)])
    documents = {}
    for batch in corpus.iter_batches(batch_size=128):
        for row in batch.to_pylist():
            doc = str(row["_id"])
            if doc in preferred:
                documents[doc] = "\n".join(text for text in [row.get("title") or "", row["text"]] if text)
    selected_relevance = {query: relevance[query] for query in selected}
    kept, size = select_documents(documents, selected_relevance)
    fixture = {
        "schema_version": 1, "status": "fixture_only", "full_benchmark": False,
        "dataset": config["dataset"], "upstream_license": manifest["license_declared"],
        "selection": "first 32 positive-judgment query IDs by SHA256(pg-qdrant-quality-v1 NUL ID); all positive documents retained; hash-ordered decoys up to 500 points/1MiB without truncating source text",
        "source_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "source_files": [{key: record[key] for key in ["repo", "revision", "path", "sha256", "origin"]}
                         for record in manifest["files"]],
        "original_corpus_points": len(ids), "source_bytes": size,
        "documents": [{"id": doc, "body": documents[doc]} for doc in kept],
        "queries": [{"id": query, "text": queries[query], "relevance": relevance[query]} for query in selected],
        "release_supported": False, "evaluation_executed": False,
    }
    raw = json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    output.mkdir(parents=True, exist_ok=True)
    (output / (config["dataset"] + ".json")).write_bytes(raw)
    return {"dataset": config["dataset"], "fixture_sha256": hashlib.sha256(raw).hexdigest(),
            "queries": len(selected), "points": len(kept), "source_bytes": size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=ROOT / "ci/quality-sources.json")
    parser.add_argument("--cache", type=Path, default=ROOT / "artifacts/quality-source")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/quality-fixtures")
    parser.add_argument("--download", action="store_true", help="download missing pinned public files")
    args = parser.parse_args()
    config = json.loads(args.sources.read_text(encoding="utf-8"))
    if config["schema_version"] != 1:
        raise ValueError("unsupported source manifest")
    for dataset in config["datasets"]:
        print(json.dumps(prepare(dataset, args.cache / dataset["dataset"], args.output, args.download)))


if __name__ == "__main__":
    main()
