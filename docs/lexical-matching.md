# Native candidate matching

`qdrant.search` accepts a `matching` object in its `options` argument. The
ranking query remains separate from required matching. Every supplied clause
is required; `any` supplies an OR within its clause and `exclude` rejects a row
containing any analyzed excluded term. Callers cannot supply native payload
paths or override the registered owner permission domain.

```sql
SELECT source_key, score, excerpt, provenance
FROM qdrant.search('articles', 'transaction recovery', 'text', 10, '{}',
  '{"matching":{"phrase":"transaction recovery","exclude":"deprecated"}}');

SELECT source_key, score
FROM qdrant.search('articles', 'transaction', 'text', 10, '{}',
  '{"matching":{"token_prefix":"tran","key_prefix":"PG-"}}');
```

| Clause | Required native condition |
| --- | --- |
| `all` | All analyzed terms in the source body |
| `any` | At least one analyzed term in the source body |
| `exclude` | None of the analyzed terms in the source body |
| `phrase` | Contiguous analyzed phrase in the single source text field |
| `token_prefix` | One alphanumeric token prefix, 2..32 Unicode characters |
| `key_exact` | Complete primary-key text value, case sensitive |
| `key_prefix` | Prefix of the complete primary-key text value, case sensitive |

BM25 encoding and the body payload index separately compile one fixed policy:
multilingual tokenizer, lowercase, disabled extra ASCII-folding processing,
no stopwords and no stemming. The fixed tokenizer itself uses Charabia's lossy
normalization: `café` and `cafe` produce the same body/BM25 term despite
`ascii_folding=false`. The body index stores positions for contiguous phrases.
The separate prefix index uses the native prefix tokenizer with lengths 2..32
and the same explicit options, but not multilingual normalization: a `café`
token prefix preserves its accent. Primary-key matching uses a prefix-enabled keyword
index; identifiers are never tokenized or lowercased. These fixed fixtures do
not establish a general Chinese/English quality benchmark or configurable
language support.

Each value requires 1..2048 UTF-8 bytes, key values at most 1024 bytes, and
the combined object at most 8192 value bytes. Native body clauses require
1..128 distinct analyzed terms; punctuation-only or over-budget clauses fail
with an error. SQL preflight validates shapes and byte budgets; analyzed-term
validation occurs at native execution. `explain_search` reports availability
and this distinction without claiming it already executed the native compiler.

The filter reaches the root request, each BM25/dense/sparse fusion branch and
the nested candidate stage before token MaxSim reranking. It runs before
bounded top-k truncation; final authorized source JOIN/version checks remain
additional safeguards. Native BM25/engine-IDF statistics use the entire live
owned generation independently of these matching clauses. Returned statistics
still describe bounded candidates, not complete match counts.

The ranking query is not a completion query: a token prefix predicate does not
turn the BM25 query into a prefix encoder. An unrelated ranking query can yield
no sparse hits even when documents satisfy the filter. A declared dense or
token representation can rank the matching domain subject to its readiness
and existing budgets. No fuzzy, phrase slop, arbitrary Boolean nesting,
per-field boosts, snippets with match offsets, suggestions or tenant-domain
policy is claimed by this implementation; their product requirements remain.

Source contract 7 requires the matching payload indexes before flush/ACK.
New generations and owner-loss replay recreate those schemas from committed
source facts. Failed schema creation cannot mark native matching ready.
This unreleased development schema requires a fresh extension installation;
in-place migration of previous 0.0.1 catalogs remains unsupported.

`verify_lexical.py` covers source DML, key reuse, actual generation switching,
shared analyzer fixtures and malformed/unauthorized inputs. `verify_tokens.py`
uses higher-scoring outsiders and a candidate cap of one in text, dense,
sparse, RRF, DBSF and nested MaxSim plans, inspecting native identities before
the SQL JOIN. The installed recovery suite repeats these assertions after
native crash cuts and PostgreSQL immediate-stop recovery.
Private fault tests stop schema creation after the actual body index update:
native search rejects the incomplete schema, PostgreSQL records no receipts,
the failed shadow is retired by its existing owner, and the serving generation
continues answering before a fresh rebuild succeeds.
