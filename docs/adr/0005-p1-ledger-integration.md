# ADR 0005: One transactional source ledger

Status: accepted implementation direction; runtime acceptance remains separate.

The two P1 branches are consolidated into one installed ledger in
`qdrant_internal`, with public management functions in `qdrant`. The source
capture branch's owner checks and public entry points are retained. The tested
ledger branch supplies typed source keys, allocated point IDs, SHA-256 content
fingerprints, revisions, incarnation changes, TRUNCATE tombstones and sealed
ticket membership. Its independent regression fixtures remain available.

| Previous difference | Consolidated contract |
| --- | --- |
| Public-schema private tables versus experimental private schema | All product ledger objects live in `qdrant_internal` |
| MD5 and text-only identity versus tagged keys and SHA-256 | Tagged bigint/uuid/text keys, stable allocated point IDs and SHA-256 |
| Primary-key update and TRUNCATE refused versus captured | Delete old identity and upsert new identity; deterministic tombstones |
| Entire source row materialized versus narrow projection | Extract only declared key and text fields |
| Blocking initial snapshot versus 1,000-row fixture | Install capture under a short DDL lock, then bounded online backfill |
| No native apply in either branch | One managed helper; explicit flush before exact-event ACK |

No source table receives both implementations' triggers. Installation occurs
through `CREATE EXTENSION pg_qdrant`; the experimental script is not a product
installation requirement. Existing draft P2–P5 admission, budget and packaging
code remains subject to runtime validation and does not establish support.

The conservative recovery route preserves dirty storage and rebuilds from the
PostgreSQL ledger in a new generation. Edge load alone never makes an unclean
generation ready. A flush receipt identifies generation, consumer and exact
event membership; PostgreSQL ACK cannot use a maximum event ID as a watermark.
Obsolete incarnations require explicit deletion of their old point IDs.

This decision does not establish power-loss, PITR, physical replication,
failover, rich lexical, model or release acceptance.
