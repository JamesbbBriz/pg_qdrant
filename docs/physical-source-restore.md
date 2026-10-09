# Physical source backup and native replay

Status: matching-build development recovery slice, verified in scoped local act. Full L12, P3-RECOVERY and
release support remain open. This test uses matching Linux PostgreSQL 17
extension/helper binaries and a standalone restored primary. It does not
establish PITR, replication, failover, cross-version upgrades or production
backup support.

## Recovery contract under test

PostgreSQL source rows, catalog, identities, outbox and fixed ticket membership
are the recovery authority. Native files must be outside PGDATA for this tested procedure. PostgreSQL
restores facts and receipts; neither copied receipts nor separately retained
native files establish an atomic PostgreSQL/Edge snapshot.

The restored helper must receive a new engine identity. Every registered index
must rotate its storage epoch and consumer identity, rebuild from restored
committed source state, flush and ACK its exact restored event sets. A retained
ticket can become durable only against this current owner and generation.
Previously retained native generations remain uncertain and must not serve reads.

Physical recovery preserves PostgreSQL generation/incarnation identities and
current model-output contracts. This differs from [logical source
re-registration](logical-source-restore.md), which deliberately allocates new
identities and requires explicit resubmission of retained model outputs. The
physical test requires real dense, learned sparse and MaxSim queries after
source-derived replay; vector columns alone are not readiness evidence.

## Required storage layout

The default development layout puts Edge files below
`PGDATA/pg_qdrant_p0/db-<database_oid>.indexes`. Actual PG17 base backup of
this layout fails with `file name too long for tar format`. Edge internal paths
exceed the 99-byte limit in PostgreSQL 17
[`tarCreateHeader`](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/port/tar.c).
Shortening only the generation directory is insufficient. Physical backup of
the default layout is therefore unsupported. No source or backup assertion is
skipped to hide this failure.

For this development procedure, the database administrator provisions the
`.indexes` path as a symlink to a private directory outside PGDATA **before the
first helper starts**. The server account owns both directories with mode 0700.
Obtain the database OID from `pg_database`; do not select storage from ordinary
SQL user input. Existing live storage requires a separately planned stopped
migration; the test does not move or delete it. PostgreSQL base backup warns
and skips this ordinary symlink. The backup contains the complete PostgreSQL
ledger and zero Edge files.

Before the restored helper starts, provision that clone
`.indexes` symlink to a **different, empty, private external directory**. Never
point two clusters at the same native storage or reuse the original target.
The clone then reconstructs derived points from its recovered PostgreSQL
state. This is an explicit operator-managed development layout, not automatic
storage relocation or a production installation default. Retained old storage
needs a separate reclamation policy.

## Executable test

`crates/pg_qdrant/tests/verify_physical_restore.py` creates two independent
disposable clusters with fsync and synchronous commit enabled. It runs:

```sh
pg_basebackup --pgdata=backup --format=plain --wal-method=stream \
  --checkpoint=fast --max-rate=4M --no-password --label=physical-recovery
pg_verifybackup backup
```

The [PostgreSQL 17 base-backup contract](https://www.postgresql.org/docs/17/app-pgbasebackup.html)
describes online cluster backups and streaming the WAL needed for recovery.
The test uses a local trusted replication connection in its disposable source
cluster. Production authentication, authorization, tablespaces and backup
retention need independent operational configuration and validation.

The test observes `streaming database files` before committing ordinary UPDATE,
DELETE and primary-key reuse, and verifies that the backup process remains
active through that commit. It checks the backup manifest and required WAL
with the complete default `pg_verifybackup` verification. As its
[documentation explains](https://www.postgresql.org/docs/17/app-pgverifybackup.html),
backup-file verification alone does not prove successful recovery; the test
also starts the clone and queries actual native results.

The clone must include the concurrent commit and exclude a separately committed
write performed after the backup has exited. Assertions compare restored
PostgreSQL identities, copied old native receipts and newly established native
owners; they check BM25 and all three BYOV kinds. Further clone DML/key reuse
must reach fresh durable receipts without changing results in the still-running
original cluster.

Scoped local act run `393b0ec17909c944` passed all four recovery groups with
matching installed binaries from `cffdae8f18b520299e4b09c439ada999dd0e079f`
and hashed test inputs. The 25 MB fixture had 1,052 verified backup files and
114 external native files; zero native files entered the backup. Old copied
ACKs reported zero pending events but `applied=false` and `durable=false`
until the clone established its new owner and rebuilt. All three BYOV queries
returned the expected source key. These fixture measurements are not a
performance claim.

The product gate invokes this test. Its report records the backup manifest
digest, source/clone owner identities, tickets, native model results and cluster
shutdown outcomes. Failures retain the owned source and clone directories;
successful runs remove only their own temporary root. A scoped run does not
replace complete CI for the exact source revision.

## Remaining boundaries

The test covers a small matching-build standalone fixture. It does not cover
interrupted/corrupt backups, external tablespaces, large recovery backlogs,
long-running transactions, changed model/analyzer contracts, timeline changes,
recovery targets, physical standby read admission, promotion, orphan native
storage reclamation or power loss. These remain required for their respective
support claims. No acceptance requirement is removed or promoted by this test.
