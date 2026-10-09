# Logical source restore and fresh text registration

Status: development recovery slice; full L12, P3-RECOVERY and release support
remain open. The installed test uses matching Linux PG17 extension/helper
binaries, a custom-format source database dump and a fresh database in the same
disposable cluster. It rebuilds a text-only index from restored ordinary tables.

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
Orphan model-output guards are also rejected with `55000`; this text-only
recovery path does not silently remove or reinterpret their contracts.

Replacement, catalog creation and new capture installation share one
transaction. Invalid later configuration or explicit rollback restores the
original trigger identities. A concurrent owner change while registration
waits for the table lock is rechecked before any trigger or catalog mutation.

The new registration allocates fresh generation/incarnation identities,
backfills restored source rows and requires real native flush/ACK before
readiness. Old backup tickets are unknown (`22023`) and never imply durability
in the target. Ordinary target DML and newly committed fixed-membership tickets
then use the normal source-to-Edge pipeline.

## Boundaries and verification

`verify_logical_restore.py` executes real `pg_dump` and `pg_restore`, checks
restored source rows and empty private state, tests owner/trigger/configuration
negatives and rollback, queries rebuilt native points, and verifies restored
delete/key reuse and updates through new durable tickets. The complete local
act product gate invokes this test; a scoped development run does not replace
full exact-revision CI.

Model-output trigger/representation migration, automatic declaration export or
restore, changed analyzers/configuration, cross-version/cluster upgrades, base
backup, PITR, physical replication and failover remain unsupported pending
their own implementation and end-to-end evidence. Retained BYOV columns alone
do not establish readiness under new incarnations. No full L12 or stage
acceptance is promoted by this text-only recovery slice.
