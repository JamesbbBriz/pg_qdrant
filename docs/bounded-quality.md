# Bounded bilingual retrieval measurement

This development baseline measures actual `qdrant.search` through PostgreSQL
and embedded Edge 0.8.0. It is a deterministic reduced-corpus experiment, not
the full upstream benchmark or a completed P2-QUALITY acceptance. BM25 queries
need no model. The optional dense experiment below uses explicitly prepared
local model inputs.

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

The recorded baseline below measures BM25 only. Learned sparse, MaxSim,
quantization and external-engine comparisons have not been measured here.
Identifier errors, authorized-filter
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

## Optional dense and fusion experiment

The additional local run `quality-3b44118f8c1f6381` measured the same frozen
queries with BM25, normalized dense dot product, RRF and DBSF. All 256 SQL
measurements completed; query identities, individual metrics and aggregates
were independently verified. The [dense measurement receipt](evidence/bounded-dense-quality.json)
records the frozen harness snapshot, model/vector hashes and installed binary
identity separately. The harness source is `e291e9c` plus the recorded changes;
the reused native product was compiled at `46389c1`. This remains partial
P2-QUALITY evidence with no completed formal acceptance gate.

The optional [model manifest](../ci/dense-quality-model.json) pins
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` at revision
`e8f8c211226b894fcb81acc59f3b34ba3efd5f42`. The publisher declares Apache-2.0.
Downloaded model files and the optional Linux Python runtime remain outside
the source repository. Each input has a byte bound and SHA-256 checksum;
existing mismatched files and interrupted downloads are preserved and rejected.
The input directory has a one-GiB admission budget including older files and
partials; projected downloads that exceed it are refused without deleting them.
The launcher uses the shared build/image budgets and run lock described in
[build-cache.md](build-cache.md). After a successful run, it verifies the complete
copied fixture/model input set before removing those temporary snapshot copies.
Original assets, measurements and manifests remain. Failed, changed or unrecorded
inputs stop retirement and preserve the failed source snapshot.
The model is an evaluation input, without changing the extension's offline BM25
default or adding model inference to its runtime dependencies.

```sh
python3 scripts/prepare_dense_quality.py --download --root artifacts/quality-model
python3 scripts/local_quality.py --image YOUR_INSTALLED_PRODUCT_IMAGE \
  --model-inputs artifacts/quality-model
```

The CPU-only ONNX encoder uses the pinned tokenizer, right truncation to 128
tokens, attention-mask mean pooling including special tokens, explicit unit
normalization and 384-dimensional float32 output. It checks scalar pooling
goldens and native batch-padding invariance before encoding. These checks do
not establish parity with a different model framework. Encoding receipts record
untruncated and used token counts, truncation, elapsed time, process peak RSS,
runtime versions and exact fixture/vector hashes. They keep partial failures.

Model output is bound to current public `qdrant.encoding_inputs` requests and
written through ordinary source-table updates with incarnation, source
fingerprint and model identity guards. Each output batch commits before waiting
for its fixed-membership durable ticket. All selected documents must become
ready before queries. The encoder exits before the PostgreSQL measurement starts
in the same bounded container.

Four plans each execute every fixed query once. Dense uses the installed exact
native search path; fusion uses native RRF/DBSF with a candidate limit of 100.
Metrics and latency distributions are reported separately for each plan.
Completion requires the exact dataset, fixture checksum and query-ID set for
each plan, including queries with zero hits. The launcher distinguishes compiled
product identity from the measurement-source snapshot and exports both encoding
and SQL evidence. Full-corpus evaluation, learned-sparse/MaxSim ablations,
comparators and the remaining acceptance requirements stay open.

| Dataset subset | Plan | Recall@10 | NDCG@10 | MRR@10 | End-to-end p50 / p95 ms |
|---|---|---:|---:|---:|---:|
| SciFact | BM25 | 0.906250 | 0.826365 | 0.805208 | 146.31 / 159.75 |
| SciFact | Dense | 0.859375 | 0.779364 | 0.776042 | 164.43 / 183.15 |
| SciFact | RRF | 0.968750 | 0.852431 | 0.816406 | 198.74 / 220.41 |
| SciFact | DBSF | 0.937500 | 0.846968 | 0.820312 | 193.64 / 214.24 |
| T2Retrieval | BM25 | 0.898695 | 0.902885 | 0.953125 | 171.60 / 203.22 |
| T2Retrieval | Dense | 0.845346 | 0.775464 | 0.807292 | 191.71 / 220.94 |
| T2Retrieval | RRF | 0.915882 | 0.904237 | 0.932292 | 227.42 / 250.78 |
| T2Retrieval | DBSF | 0.919007 | 0.933089 | 0.968750 | 226.91 / 262.19 |

Each row covers 32 queries, with no zero-result queries. Dense alone scored
below BM25 on both subsets. Fusion improved some metrics in this run; this
small corpus and one execution per query do not establish a general quality
improvement. The model's 128-token contract truncated 495 of 532 SciFact inputs
and 399 of 502 T2Retrieval inputs, including documents and queries. Encoding
cost and peak process RSS are in the receipt; the encoder exits before SQL
measurement and these values are not the native engine's memory use.

The prior run and completed retry used identical vector bytes. Three SciFact
RRF queries changed the order of equal-score hits; RRF NDCG/MRR therefore
differed between runs. The table reports the completed retry. Fixed input
selection does not guarantee identical ordering of native fusion ties across
fresh indexes, and these observations do not establish ranking repeatability.

All 500 and 470 document representations became ready through ordinary guarded
source updates and 50/47 committed durable-ticket batches. Both owned containers
and runner volumes were retired, together with 34 verified temporary input
copies (527,444,348 bytes) and the materialized source view. Original assets
remain available for reuse. An earlier run completed retrieval but failed on
multi-tag image retirement; its original failure evidence was preserved before
the retention fix and successful retry. The completed retry includes resource
retirement in its success condition.
