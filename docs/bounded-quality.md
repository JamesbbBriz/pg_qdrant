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

## Optional trained token-vector measurement

The same measurement accepts `--tokens` with the pinned dense inputs. It adds
[Mixedbread's English 17M ColBERT model](https://huggingface.co/mixedbread-ai/mxbai-edge-colbert-v0-17m/tree/592c6417c1c6687572043408ed1ae5196bce16b1)
at revision `592c6417c1c6687572043408ed1ae5196bce16b1`, using the publisher's
float32 ONNX export and 48-dimensional token projection. The publisher declares
Apache-2.0. Models and corpus contents remain optional evaluation inputs and are
not part of the extension or its default offline BM25 path.

```sh
python3 scripts/prepare_dense_quality.py --root artifacts/quality-model --tokens --download
python3 scripts/local_quality.py --image <installed-product-image> \
  --model-inputs artifacts/quality-model --tokens
```

The fixed model contract pins all twelve files and their checksums, totaling
71,649,484 bytes. Combined with the existing dense model and runtime wheels,
declared assets total 597,249,710 bytes inside the existing 1 GiB admission limit.
Old files and partial downloads count toward that limit. The two encoders run
sequentially and exit before PostgreSQL starts; the measurement retains its
two-CPU, 2 GiB memory, no-swap and offline execution limits.

Token encoding lowercases inputs, inserts the publisher's query/document marker
after CLS, retains SEP on right truncation, excludes document punctuation and
padding from scoring, and keeps valid query tokens. Query expansion is disabled
as declared by the publisher. Query length remains 48; document length is reduced
from the publisher's 512 to the installed slot's maximum of 128. The receipt
records truncation, token counts, encoding time and peak encoder RSS. Native
batch-padding checks and scalar token-policy goldens do not establish parity with
PyTorch/PyLate or the publisher's benchmark scores.

SciFact adds three plans: BM25 then MaxSim, BM25+dense RRF then MaxSim, and
BM25+dense DBSF then MaxSim. Each recall branch and the fused candidate domain
are capped at 64. The worst declared scalar work is
`48 * 128 * 48 * 64 = 18,874,368`, below the existing 20,000,000 limit.
Fusion only selects candidates; native MaxSim supplies the final scores. Every
rerank records its candidate identities and independently checks dot/max/sum
scores and top-ten membership within that domain. Equal scores permit either
order. This does not promise whole-corpus exact top-k. Dense/fusion controls keep
their previous candidate limit of 100, so their final results are separate plans.

Token outputs use ordinary guarded source updates and committed durable tickets.
An actual source edit must invalidate the query and reject late output; restoring
the original source and reinstalling both representations must become durable
before measurement. The fixture clears both outputs before restoration so that
it consumes actual missing-output requests. Restoring identical content alone
can make matching outputs ready again under the content-fingerprint contract.
Exact text identity, little-endian float32 ranges, unit
vectors, fixed query sets, output hashes and successful resource retirement are
required. Chinese MaxSim, full-document quality, learned sparse and external
comparators remain unmeasured. This partial P2-QUALITY/P2-HYBRID evidence does not
close a formal stage gate.

Latency covers the precision SQL call, including its native candidate recall and
source JOIN. The separate candidate-inspection query, independent oracle and
explain call are outside that interval. Measurements use one sequential call per
query with no warmup or repeats; they do not establish production latency.

### Completed development measurement

Local run `quality-3f43dc40f6f67425` completed 352 ranked SQL queries: 224 English
queries across seven plans and the existing 128 Chinese control queries. The
[aggregate receipt](evidence/bounded-token-quality.json) records the frozen
measurement source (`af63c36` plus its recorded working diff and executable
hashes) separately from the reused native product (`46389c1`). Independent
recomputation checked all metrics, 1,034 dense vectors, 532 token matrices and
6,117 candidate scores across 96 MaxSim queries. The largest native score error
was `8.878469010653589e-06`, below the fixed absolute tolerance of `5e-5`.

| SciFact subset plan | Recall@10 | NDCG@10 | MRR@10 | SQL p50 / p95 ms |
|---|---:|---:|---:|---:|
| BM25 | 0.906250 | 0.826365 | 0.805208 | 191.49 / 206.83 |
| Dense | 0.859375 | 0.779364 | 0.776042 | 216.12 / 279.53 |
| RRF | 0.968750 | 0.853580 | 0.819010 | 242.27 / 297.59 |
| DBSF | 0.937500 | 0.846968 | 0.820312 | 248.77 / 265.94 |
| BM25 then MaxSim | 0.875000 | 0.838796 | 0.832589 | 284.42 / 337.05 |
| RRF then MaxSim | 0.906250 | 0.849928 | 0.837798 | 310.05 / 359.74 |
| DBSF then MaxSim | 0.906250 | 0.849928 | 0.837798 | 312.34 / 374.63 |

Each row covers the same 32 selected English queries, with no zero-hit results.
Mean candidate recall before reranking was 0.937500 for BM25 and 0.968750 for
both fusion routes. MaxSim improved MRR in this run but reduced Recall@10 for
all three routes; RRF NDCG also decreased slightly. Candidate budgets differ
between controls and reranks, and this small, truncated subset does not justify
a general model improvement or a default-plan change. Chinese controls retain
their measured quality values in the receipt; no Chinese token model ran.

The token encoder used 4.0064 seconds including loading, with peak process RSS
206,440 KiB. It truncated 488 of 532 inputs and stored 58,509 token vectors in an
11,233,728-byte float32 file. These are encoder/input figures, not native index
disk or memory measurements. All 500 token outputs became ready through 500
committed durable batches. The source-edit, stale-query, late-output and restored
representation checks also passed before retrieval.

The first run failed because its fixture assumed exact content restoration
still required a new encoding request. Its original failure and cluster evidence
remain preserved. The completed retry explicitly clears both outputs first and
passes the same refusal/durability requirements. Both owned test containers and
runner volumes, the materialized source view and 46 verified input copies
(599,093,832 bytes) were retired. The original reusable assets remain inside the
1 GiB admission policy. No model or corpus bytes are included in this repository.

A second successful run uses the exact LF model-contract bytes committed with
the harness. Encoded dense and token bytes match the earlier successful run.
The receipt records 29 changed RRF rankings, including three changes in top-ten
membership. Their score sequences and common-hit scores match within `1e-8`.
Six RRF candidate sets used by MaxSim also changed; the final MaxSim rankings
matched in these two runs. These observations do not establish native fusion
tie-order or candidate-set repeatability.

### Matched candidate budgets and loss diagnosis

The token harness additionally runs `matched_text`, `matched_rrf` and
`matched_dbsf`: actual SQL top-ten queries with candidate limit 64, matching
the three precision routes. All original controls now explicitly request 100
candidates, including text and dense; earlier text/dense controls used the SQL
default of 50. The original measurements above remain historical observations.
English coverage becomes ten plans for all 32 fixed queries; the four Chinese
controls remain required. No query or positive judgment is removed.

Each MaxSim result records a separate native recall observation of up to 64
identities. Its first ten identities form a recorded-domain control, distinct
from the independently executed matched SQL query. Relevant top-ten losses are
partitioned into documents absent from that observed candidate set and documents
present but ranked below ten after MaxSim. Gains and metric deltas are also
recorded. Fusion ties can vary between calls: matching budgets alone does not
prove identical candidate domains, and these partitions describe observations.

All selected queries contribute to diagnostics. Separate strata distinguish
queries with any truncated relevant document from queries with none; empty
strata have zero queries and null means. Length association does not prove that
truncation caused a loss. Full-document encoding, matched-model length ablations,
Chinese token quality, learned sparse and external comparators remain required.
