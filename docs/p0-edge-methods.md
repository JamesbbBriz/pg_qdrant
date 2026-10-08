# Fixed-version Edge shard method audit

Scope: the public methods declared on `EdgeShard`, `EdgeShardRead`,
`ReadOnlyEdgeShard`, and `UpdateOnlyEdgeShard` in the published `qdrant-edge
0.8.0` package. The audit finds 44 declarations: 20 ordinary-shard methods,
11 read-trait methods, six read-only methods, and seven update-only methods.
Trait implementations reuse the eleven declarations; they are not counted as
new methods. Auxiliary request/configuration builders and raw segment/storage
interfaces remain in the wider [capability inventory](capabilities.md).

The [compile inventory](../crates/edge-probe/src/api_inventory.rs) contains
concrete argument-bearing call bodies for 43 declarations. `refresh_with` has
the explicit public-construction exclusion below. The
[local compilation record](evidence/p0-method-input-rebuild-local.json) verifies
these bodies with the pinned lockfile and Rust 1.96.0. This is compile evidence for their signatures and
arguments; it does not execute the methods, expose a supported SQL entry point,
or complete a capability. Independent runtime observations remain scoped by
the [probe README](../crates/edge-probe/README.md).

Every operation-bearing body requires a reference to a private, uninhabited
`CompileOnly` enum. Safe code cannot construct that reference. The functions
are neither runtime tests nor registered commands, and contain no unsafe
escape. Rust still checks their arguments and return types. This prevents
adding the inventory from accidentally opening a shard, taking its ownership,
performing an update/optimization, or moving recovery files. A list of method
calls does not fail merely because upstream adds a method: repeat this source
audit on each dependency upgrade as well as retaining exhaustive enum checks.

## Complete declaration mapping

Source locations below are relative to the
[fixed registry package](https://docs.rs/crate/qdrant-edge/0.8.0/source/).
`E` denotes `src/edge/`. The source hashes at the end bind the reviewed files.
Every listed ID retains its complete product acceptance requirements.

| Public surface | Fixed source | IDs | Compile-call or scope disposition |
| --- | --- | --- | --- |
| `EdgeShard::new` | `E/edge_shard/mod.rs:55` | L01 | `lifecycle_and_read_compile_surface`; concrete two-dimensional named-vector config and path |
| `EdgeShard::load` | `E/edge_shard/mod.rs:110` | L01, L04 | Same probe, explicit optional config; load may repair/write files |
| `EdgeShard::config` | `E/edge_shard/mod.rs:192` | L07 | Clone the effective config, releasing the read guard before setters |
| `EdgeShard::path` | `E/edge_shard/mod.rs:196` | L01 | Typed borrowed path; never a public SQL path input |
| `EdgeShard::set_hnsw_config` | `E/edge_shard/mod.rs:201` | Q13, L07 | Concrete `HnswIndexConfig`, bounded indexing threads |
| `EdgeShard::set_vector_hnsw_config` | `E/edge_shard/mod.rs:209` | Q13, Q14, L07 | Explicit `dense` vector name and config |
| `EdgeShard::set_optimizers_config` | `E/edge_shard/mod.rs:236` | L03, L07 | Concrete `EdgeOptimizersConfig`; no scheduling/runtime claim |
| `EdgeShard::flush` | `E/edge_shard/mod.rs:254` | L01, L04 | Concrete ordinary-shard receiver; not a PostgreSQL ACK or durable cursor |
| `EdgeShard::update` | `E/edge_shard/update.rs:14` | L02 | Explicit point-delete operation with one numeric point ID |
| `EdgeShard::optimize` | `E/edge_shard/optimize.rs:28` | L03, L10 | Typed boolean result; synchronous, no caller cancellation token |
| `EdgeShard::search` | `E/edge_shard/shard_read.rs:47` | Q01 | Concrete `SearchRequest`; deprecated compatibility sentinel only |
| `EdgeShard::query` | `E/edge_shard/shard_read.rs:51` | Q01–Q08, Q10 | Concrete named dense query, exact flag and limit two; other variants have separate probes |
| `EdgeShard::scroll` | `E/edge_shard/shard_read.rs:55` | Q10 | Concrete request with limit two |
| `EdgeShard::retrieve` | `E/edge_shard/shard_read.rs:62` | Q10 | One explicit numeric ID |
| `EdgeShard::count` | `E/edge_shard/shard_read.rs:66` | Q11 | Concrete `CountRequest`, typed point-count result |
| `EdgeShard::facet` | `E/edge_shard/shard_read.rs:70` | Q11, Q12 | Explicit `tenant` field path |
| `EdgeShard::info` | `E/edge_shard/shard_read.rs:74` | L07 | Typed `ShardInfo`; no READY/coverage inference |
| `EdgeShard::unpack_snapshot` | `E/edge_shard/snapshots.rs:11` | L04, L12 | `snapshot_compile_surface`; explicit archive and unpack paths |
| `EdgeShard::snapshot_manifest` | `E/edge_shard/snapshots.rs:15` | L04 | Inferred public return values passed directly into recovery |
| `EdgeShard::recover_partial_snapshot` | `E/edge_shard/snapshots.rs:19` | L04, L12 | Both manifests and both directory arguments supplied; previous owners dropped first |
| `EdgeShardRead::config_snapshot` | `E/read_view/shard_read.rs:58` | L07 | `read_method_calls`; typed `Arc<EdgeConfig>` |
| `EdgeShardRead::path` | `E/read_view/shard_read.rs:60` | L01 | Typed path borrow |
| `EdgeShardRead::search` | `E/read_view/shard_read.rs:63` | Q01 | Concrete deprecated request; not selected for the SQL adapter |
| `EdgeShardRead::query` | `E/read_view/shard_read.rs:65` | Q01–Q08, Q10 | Concrete named dense request |
| `EdgeShardRead::scroll` | `E/read_view/shard_read.rs:67` | Q10 | Concrete limit-two request |
| `EdgeShardRead::retrieve` | `E/read_view/shard_read.rs:72` | Q10 | Explicit one-ID request |
| `EdgeShardRead::count` | `E/read_view/shard_read.rs:74` | Q11 | Concrete count request |
| `EdgeShardRead::facet` | `E/read_view/shard_read.rs:76` | Q11, Q12 | Explicit field path |
| `EdgeShardRead::search_matrix` | `E/read_view/shard_read.rs:78` | Q11 | Sample two, one neighbor per sample, explicit `dense` representation |
| `EdgeShardRead::query_groups` | `E/read_view/shard_read.rs:80` | Q09 | Two document groups, one hit each, explicit `document_id` path |
| `EdgeShardRead::info` | `E/read_view/shard_read.rs:82` | L07 | Typed information result |
| `ReadOnlyEdgeShard::open_mmap` | `E/read_only/lifecycle.rs:19` | L04 | `read_only_compile_surface`; pins the concrete mmap result type |
| `ReadOnlyEdgeShard::open` | `E/read_only/lifecycle.rs:59` | L04, V05 | Same result type as `open_mmap`, inferred default mmap filesystem, explicit config and retrieve load profile |
| `ReadOnlyEdgeShard::path` | `E/read_only/mod.rs:67` | L04 | Typed path borrow |
| `ReadOnlyEdgeShard::segments_count` | `E/read_only/mod.rs:72` | L04, L07 | Typed segment count, never a document count |
| `ReadOnlyEdgeShard::refresh` | `E/read_only/refresh.rs:41` | L04 | Concrete follower receiver; not a committed-change wait |
| `ReadOnlyEdgeShard::refresh_with` | `E/read_only/refresh.rs:50` | L04, L10 | Not adopted: hidden hardware-counter input cannot be constructed through the selected public surface |
| `UpdateOnlyEdgeShard::open_mmap` | `E/update_only/lifecycle.rs:19` | L02, L04 | `update_only_compile_surface`; fixed-release empty bootstrap is unavailable |
| `UpdateOnlyEdgeShard::open` | `E/update_only/lifecycle.rs:35` | L02, L04 | Inferred default mmap filesystem, explicit public local enumerator; same result type as `open_mmap` |
| `UpdateOnlyEdgeShard::path` | `E/update_only/mod.rs:71` | L04 | Typed path borrow |
| `UpdateOnlyEdgeShard::segments_count` | `E/update_only/mod.rs:76` | L04, L07 | Typed segment count |
| `UpdateOnlyEdgeShard::segment_configs` | `E/update_only/mod.rs:83` | L02, Q14 | Typed `Vec<SegmentConfigInfo>` |
| `UpdateOnlyEdgeShard::preview_batch` | `E/update_only/preview.rs:123` | L02 | Concrete sequence-number/delete batch, typed preview |
| `UpdateOnlyEdgeShard::apply_batch` | `E/update_only/apply.rs:78` | L02, L04 | Same concrete batch, typed outcome; Delete/Store runtime branches are unavailable in this version |

`read_method_calls` is type-checked with both ordinary and mmap read-only
receivers. `UpdateOnlyEdgeShard` does not implement this read trait. The trait
is sealed; implementing a substitute external shard is outside this API.

## Fixed-version dispositions and remaining gates

- **Snapshot archive and recovery (L04, L12):** the public shard API exposes
  inspection, unpack and partial recovery, but no snapshot-archive producer.
  `SnapshotManifest` is nameable through `internal` only. Its public return
  value can instead be inferred and passed to the public recovery method;
  these probes use that route, without importing `internal`, inventing an
  archive format, or serializing it into a public SQL contract. Recovery moves
  and removes files and calls writable `load`. Archive format compatibility,
  archive validation/budgets, disposable-target recovery, rollback and PG
  timeline reconciliation remain runtime/design gates. The existing quiescent
  fixture copy and manifest inspection are not an archive restore.
- **Hidden I/O types (L04, V05):** mmap `open` calls use an inferred
  `Default` filesystem value; the two branch results constrain it to the
  public `open_mmap` specialization. No private I/O trait or type is imported.
  This only covers the local mmap backend. Custom/blob/S3 implementations of
  the hidden `UniversalRead`/`UniversalReadExt`/filesystem traits are outside
  the current adapter scope, irrespective of the generic source signatures.
- **Hardware counter (L04, L10):** `refresh_with` requires
  `HardwareCounterCell`, whose module is private and whose constructor is not
  reexported. Its `Default` implementation is behind upstream `testing`, not
  enabled by the fixed core graph. The retained route is `refresh()`, which
  creates an internal disposable counter. Do not manufacture an argument via
  unsafe code, a diverging expression, or an unsupported feature just to make
  a compile claim. Upstream export and an accounting contract are prerequisites
  for adopting caller-supplied counters.
- **Private seams (L01, L04, L10):** `update_segment_manifest`, read-only
  `open_with_enumerator`, `ReadViewProvider` and its pool/raw-segment accessors
  are `pub(crate)`. They are excluded from external adapter calls. Public
  `SegmentsManifest` fixture serialization is a separate experiment, not
  proof of an automatic manifest publisher. No public ordinary-shard WAL
  replay, durable-sequence, WAL reclamation or native caller-cancellation
  method is declared by these four surfaces.
- **Update-only availability (L02, L04):** callable method signatures do not
  repair the observed `todo!` paths for Store, Delete and empty bootstrap.
  The internal segment flush is also unimplemented, and no public
  `UpdateOnlyEdgeShard::flush` exists. Its preview/no-write observations stay
  separate from the ordinary-shard write/flush contract. It is not the selected
  writer or durability workaround.
- **Deprecated search (Q01):** upstream documents `SearchRequest`/`search`
  as deprecated in favor of `QueryRequest`/`query`. The concrete legacy calls
  retain compile coverage solely for compatibility review. They introduce no
  extra product endpoint or obligation to execute that alternate path.
- **Maintenance and reads (L03, L07, L10):** concrete calls do not measure
  blocking, simultaneous queries/config changes, caller cancellation or memory
  placement. `refresh()` can return success without convergence after its
  bounded retries. Raw `path`, config and information values remain internal;
  they do not grant users filesystem access or certify index readiness.

The local compilation checkpoint passed the following command against
`crates/edge-probe/src/api_inventory.rs` SHA-256
`d58e698089206cfa282b2154e4e1a5abe71191b84191074f930db076ce7e9a38`
and the pinned lockfile:

```sh
cargo check --locked -p pg-qdrant-edge-probe
```

The same [local record](evidence/p0-method-input-rebuild-local.json) separately
records 25 normal engine tests and four PostgreSQL profile compilation checks.
Those tests have their own fixtures and assertions; their success is not
runtime coverage of all 43 method call bodies. PostgreSQL profile compilation
is not a SQL execution result.

The [negative-input matrix](evidence/p0-negative-inputs-local.json) and
[dirty-generation reconstruction experiment](evidence/p0-dirty-rebuild-local.json)
provide two additional bounded engine/controller observations. In particular,
a callable method can accept an invalid product input, panic after unchecked
input construction, or operate on a generation the controller must refuse.
The signature audit does not replace adapter validation, readiness policy or
the durability constraints in [ADR 0003](adr/0003-edge-durability-and-recovery.md).

This method audit adds no dependencies and changes neither the 54 capability
IDs nor their release status. It does not claim exhaustive runtime coverage
of arguments, query variants or feature combinations.

## Reviewed source hashes

Registry archive SHA-256:
`0b8072302c87506a34bffec9bc16dbdcd36df8ab1321406b6e141530348c7e54`.
The following hashes are of the unpacked fixed-version files; `E` again means
`src/edge/`.

<!-- Source hash table is generated from the verified local registry package. -->

| Registry source | SHA-256 |
| --- | --- |
| `src/lib.rs` | `d47a1116a8195647f6b8d48c736c48adba9aef75202d8ce9a14ed557b6f6733a` |
| `E/mod.rs` | `727ac45608d0741c98308f7a0313351923b95864762c52e8677a915cd2ad1424` |
| `E/reexports.rs` | `e55d3ec968fcfbc6092c121e82d33ea982f82ed198b5c47254874ac125fcb755` |
| `E/edge_shard/mod.rs` | `e3dd9b41b20ced8b5565432fb28241bd6a97a8b49f9ce8d6f5e5d3851f3e0191` |
| `E/edge_shard/update.rs` | `a3811395984f3c2fcc52b67da0def1c679d975b63ab3e7f1393de5820b2bbf85` |
| `E/edge_shard/optimize.rs` | `c09c656f5461ff47ac6a43e867a01bd38e95e47a5d4ff64dacc6fd9d5b8dde8f` |
| `E/edge_shard/shard_read.rs` | `9bab1e079065a3773761452cf800a958987f39ea306c4cdc8ebebf2055bee04d` |
| `E/edge_shard/snapshots.rs` | `3e182f53cd126e9fb8b2e50f77ccc634418f27a28c26acefbc2354307d5d5b87` |
| `E/read_view/shard_read.rs` | `f87dc0057e09519bb735535b33d64b31defcbd2719fbca9f7b6d90a95d12bff0` |
| `E/read_only/lifecycle.rs` | `5d60de9a467ad9f1dde089b4b9bf54f3e763557b78e4e689c6d2d60919155139` |
| `E/read_only/mod.rs` | `44ccc37eddab1703a39b3a2622691682385c7a85bbb657fbec45d5ec25e7b0a4` |
| `E/read_only/refresh.rs` | `19dcbfb06c457484bcee1961834ea3d2ae42618a7bde268088c12969b7b70651` |
| `E/update_only/lifecycle.rs` | `e36f23524b3e2867d31f8ba6aa472ef851db4a34192162c284bbc6029ca1f62e` |
| `E/update_only/mod.rs` | `13752151247f5bbc861938b431f6594888a855b375656a92105778a6c0468617` |
| `E/update_only/preview.rs` | `dd1d42ce46a0dd94bbebd74451a85bf186a95ab6bdf4abf48183394e8ee7d60b` |
| `E/update_only/apply.rs` | `02f50995b7ee1058353a7c1b542316487ef68fc9f45b03650f7ed2968091ff20` |
| `src/common/universal_io/mmap/mod.rs` | `c5c8b5ac8adbb91749f87531eba366036beaea5ad355edd5e589dfe280755f38` |
| `src/common/counter/hardware_counter.rs` | `d0b9c298b36194f1604a6fb0095fca1f2218047c66fae2a6361424b90d44f5e9` |
