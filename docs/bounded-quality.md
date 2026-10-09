# Bounded bilingual BM25 measurement

This development baseline measures actual `qdrant.search` through PostgreSQL
and embedded Edge 0.8.0. It is a deterministic reduced-corpus experiment, not
the full upstream benchmark or a completed P2-QUALITY acceptance. No model is
downloaded or required for queries.

The pinned inputs and their byte sizes, SHA-256 checksums, upstream revisions
and declared licenses are in [the source manifest](../ci/quality-sources.json).
English data comes from [BEIR SciFact](https://huggingface.co/datasets/BeIR/scifact)
and its [test judgments](https://huggingface.co/datasets/BeIR/scifact-qrels),
declared CC-BY-SA-4.0. Chinese data comes from
[MTEB T2Retrieval](https://huggingface.co/datasets/mteb/T2Retrieval), derived from
[THUIR T2Ranking](https://github.com/THUIR/T2Ranking), whose publisher declares
Apache-2.0. Preserve the upstream attribution/citation and license obligations
when using or redistributing downloaded data. The repository contains metadata
and aggregate measurements, without corpus text or query text; the project's
AGPL-3.0-only license does not replace upstream data licenses.

Selection sorts positive-judgment query IDs by
`SHA256("pg-qdrant-quality-v1\0" + id)` and takes the first 32. Every positively
judged document for those queries is retained. Hash-ordered decoys fill up to
500 points, stopping before the 1-MiB source-body budget. Complete title and
body text are joined with a newline; bodies are never truncated. Selection
does not use retrieval scores. SciFact has 500 selected points from 5183;
T2Retrieval has 470 from 118605. Positive denominators are preserved, while the
much smaller decoy domain makes these results unsuitable for comparison with
full-corpus leaderboard scores.

Recall@10 divides retrieved positives by **all** positive judgments for that
query. NDCG@10 uses gain `2^grade - 1`, logarithmic rank discount and the ideal
graded ranking. MRR@10 uses the first positive rank. Unjudged documents count
as non-relevant. The report includes every query ID/ranking, failures, sample
size and zero-result count; duplicated source keys are refused. Independent
goldens cover missing results, full denominators, graded relevance, rank
discounts, cutoff and malformed judgments.

The measured plan is the existing native multilingual BM25 analyzer with
lowercasing, no stemming/stopwords, average length 16 and live-generation IDF.
This tests one configured plan; it does not establish general Chinese analysis
or semantic quality. Native point IDs are joined back to the current source
through the SQL result path. This workload runs as the registered source owner
without tenant filters; existing security regression tests remain separate.

Prepare inputs on a host with Python and the pinned optional Parquet reader:

```sh
python3 -m pip install -r ci/quality-requirements.txt
python3 scripts/prepare_quality.py --download
```

Subsequent preparation verifies the cached bytes without network access when
`--download` is omitted. A mismatched cache is rejected, never silently replaced.
The fixture hashes are fixed and checked again by the SQL measurement runner.
Canonical UTF-8/LF metadata makes fixture identity independent of host newline
settings. Downloaded data, generated fixtures and full reports stay under the
ignored `artifacts/` directory.

Select a previously built matching PostgreSQL 17 product image containing the
extension, managed helper and install SQL; then use local act:

```sh
python3 scripts/local_quality.py --image YOUR_INSTALLED_PRODUCT_IMAGE
```

The launcher requires the runner image documented in [local CI](build.md),
records immutable product/runner image IDs, snapshots source and fixture
hashes, and invokes [the local workflow](../ci/quality.yml). It creates a fresh
non-root PostgreSQL cluster with fsync and synchronous commit enabled, ordinary
business tables, `CREATE EXTENSION`, public index registration, and a durable
readiness wait before queries. The measurement harness does not rebuild the
selected product binaries; installed helper/library/SQL hashes and actual
`build_info` are recorded. Source-snapshot identity is the harness identity,
not an assertion that an arbitrary selected image was compiled from that tree.

The product container has two CPUs, 2 GiB memory with no extra swap, 128 PIDs
and no network. It executes sequential queries once, without warmup or cache
resets. p50/p95 include one `psql` process, SQL/native execution, source JOIN
and JSON serialization per query. They are not engine-only latencies or
production service percentiles. The initial registration-to-durable interval
is recorded separately. Failed/incomplete measurements fail the local act job
and retain the failed cluster; successful runs export results and remove only
their owned containers.

Dense, learned sparse, RRF/DBSF, MaxSim, quantization and external-engine
comparisons have not been measured here. Identifier errors, authorized-filter
fill, diversity, coverage, encoding cost, native RSS/disk and update/optimize/
recovery timings remain open. There is no quality threshold or claimed speedup.
The complete acceptance contract and all comparator requirements remain in
[acceptance.md](acceptance.md#quality-and-efficiency-evidence).

The recorded local run `quality-cd951b2e40028cb6` measured all 64 queries with
zero errors and no zero-result queries. All fixture/input/log/result hashes
were verified, and every per-query metric was independently recomputed. Its
aggregate results and installed binary hashes are in
[the measurement receipt](evidence/bounded-quality.json).

| Dataset subset | Queries | Points | Recall@10 | NDCG@10 | MRR@10 | End-to-end p50 / p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| SciFact | 32 | 500 | 0.906250 | 0.826365 | 0.805208 | 136.14 / 168.28 |
| T2Retrieval | 32 | 470 | 0.898695 | 0.902885 | 0.953125 | 144.87 / 156.16 |

These are observations for the declared small corpus and single sequential
run. They provide a reproducible starting point for ablations and regressions;
they do not supply a full-corpus quality threshold or close P2-QUALITY.
