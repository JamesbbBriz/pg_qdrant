# Logical source restore and fresh index registration

Status: development recovery slice; full L12, P3-RECOVERY and release support
remain open. The installed test uses matching Linux PG17 extension/helper
binaries, a custom-format source database dump and a fresh database in the same
disposable cluster. It rebuilds text and explicitly declared BYOV indexes from
restored ordinary tables.

## What the dump retains

PostgreSQL dumps extension installation separately from extension-owned table
contents. Configuration tables require explicit registration with
`pg_extension_config_dump`; see the [PostgreSQL 17 extension contract](https://www.postgresql.org/docs/17/extend-extensions.html#EXTEND-EXTENSIONS-CONFIG-TABLES).
The current private catalog/outbox/receipt tables are not configuration-dump
tables. A logical dump retains user source facts and their capture triggers,
but does not restore index declarations, source identities, change tickets,
native shards or readiness. Do not copy private catalog rows or native files
into the restored database to manufacture a ready generation.

Retain the reviewed `qdrant.create_index` declarations separately, together
with the exact extension/helper version and source schema. This is explicit
source recovery, not automatic pg_dump index-metadata restoration.

## Tested recovery sequence

For a text-only source and a fresh target using the same build:

```sh
pg_dump --format=custom --no-owner --no-acl --file=app.dump app
createdb --template=template0 restored_app
pg_restore --exit-on-error --no-owner --no-acl --dbname=restored_app app.dump
```

The tested commands intentionally assign restored objects to the restoring
role and omit source ACLs. Reestablish reviewed owners and grants before
application access. They do not prove role/ACL migration or cross-version
compatibility. The target needs the matching extension installation files and
managed helper before restoring `CREATE EXTENSION`.

In the restored database, execute the retained declaration as the current
source owner:

```sql
SELECT qdrant.create_index(
  'articles', 'public.articles', 'id',
  '{"text":{"fields":["body"]}}');
SELECT qdrant.index_status('articles');
-- Continue polling until engine_index_ready=true and pending_events=0.
SELECT * FROM qdrant.search('articles', 'recovery');
```

Before re-registration, the restored capture triggers cannot find their private
catalog binding and source DML fails. Re-registration holds the source DDL lock,
rechecks the effective caller's current owner membership, and only replaces a
complete pair of enabled, unconditional triggers with the exact names,
functions, event types and index-name arguments. Partial, disabled, altered or
foreign trigger pairs are rejected with `55000`; an already registered source
or index name is rejected with `23505`. Other user triggers are preserved.
Model-output guards require the retained representation declarations described
below; omitting a guarded representation is rejected with `55000`.

Replacement, catalog creation and new capture installation share one
transaction. Invalid later configuration or explicit rollback restores the
original trigger identities. A concurrent owner change while registration
waits for the table lock is rechecked before any trigger or catalog mutation.

The new registration allocates fresh generation/incarnation identities,
backfills restored source rows and requires real native flush/ACK before
readiness. Old backup tickets are unknown (`22023`) and never imply durability
in the target. Ordinary target DML and newly committed fixed-membership tickets
then use the normal source-to-Edge pipeline.

## Restored model outputs

Retain each named dense, learned sparse and token-vector declaration, including
model/tokenizer/version, vocabulary/IDF, dimensions and the five output-column
names. Pass these explicit `representations` to the same `create_index` call.
Before retaining an existing model guard, registration checks its exact name,
function, source/index/slot arguments, enabled state, unconditional BEFORE ROW
UPDATE event and complete output-column set. Altered guards and undeclared old
guards fail with `55000`. Matching guards keep their identities and use the new
catalog binding; missing declared guards are installed normally. Validation
shares the source lock and transaction with registration: an invalid later slot
or rollback preserves all original trigger identities. The normal source-DDL
invalidation remains active, including for a later guard drop or alteration.

The restored columns retain their values, but fresh source incarnations make
those old model outputs **stale**. Text retrieval can become ready while model
queries still fail with `55000`. For every slot, request current encoding inputs:

```sql
SELECT qdrant.encoding_inputs('articles', 'dense');
```

The application must verify or regenerate outputs against the returned source
text and complete model contract. Submit each output through ordinary source
UPDATE using the returned source fingerprint and **new incarnation**, together
with the declared model ID/version. See the [dense BYOV SQL example](../crates/pg_qdrant/tests/verify_models.py)
for the output-column and committed-ticket flow. Wait for the new ticket to be
durable before relying on native model results. Late output carrying a backup
incarnation is rejected; keeping an old vector column is insufficient evidence
of its model or source freshness. No model inference is performed by restore.

## Boundaries and verification

`verify_logical_restore.py` executes real `pg_dump` and `pg_restore`, checks
restored source rows and empty private state, tests owner/trigger/configuration
negatives and rollback, queries rebuilt native points, and verifies restored
delete/key reuse and updates through new durable tickets. The complete local
act product gate invokes this test; a scoped development run does not replace
full exact-revision CI.

`verify_model_restore.py` adds all three BYOV kinds to the actual dump, rejects
omitted and altered model guards, checks transactional cleanup rollback, proves
stale restored values and rejected old identities, then resubmits current
outputs and queries real native dense, sparse and MaxSim results after durable
tickets.

Automatic declaration export or restore, changed analyzers/configuration,
cross-version/cluster upgrades, default-layout base backup, PITR, physical
replication and failover remain unsupported pending
their own implementation and end-to-end evidence. Retained BYOV columns alone
do not establish readiness under new incarnations. No full L12 or stage
acceptance is promoted by this recovery slice.

A separate [physical recovery slice](physical-source-restore.md) verifies a
matching-build standalone hot backup with explicitly external native storage
and fresh source-derived replay. It does not establish the modes above.
