# Bounded directional synonym queries

Implementation status: partial SQL integration verified by scoped local CI.
Full F16, P4-LEXICAL and release acceptance remain open.

`qdrant.search_synonyms(index_name, q, policy, top_k DEFAULT 10,
options DEFAULT '{}')` uses the same complete durable authorized source
snapshot as [bounded lexical queries](source-bounded-lexical.md). The policy is
an explicit request-local dictionary with an ID, revision and directional rules.
It changes the query, so it does not mutate the stored source or require index
rebuilding. Persisted shared dictionary management is not implemented.

```sql
SELECT qdrant.search_synonyms('articles', 'car insurance',
  '{"id":"transport","revision":1,"rules":[
    {"from":["car"],"to":[["automobile"],["motor","vehicle"]]}
  ]}', 10,
  '{"filter":{"field":"category","eq":"Allow"}}');
```

This example requires an existing ready index named `articles`, its declared
keyword payload alias `category`, and the usual owner/source SELECT permissions.
It matches the original contiguous phrase `car insurance` or the contiguous
phrases `automobile insurance` and `motor vehicle insurance`. Reversed words and
intervening words do not match. A single-word phrase uses a native term query;
multiword alternatives use native Tantivy positional phrase queries with zero
slop. Native Boolean OR combines the alternatives; matching several alternatives
can contribute to the native score. Scores use the filtered query-local corpus,
without mixing Edge BM25 or vector scores. Relevance improvement is unmeasured.

Matching is directional. A rule from `car` to `automobile` does not expand an
`automobile` query to `car`. At each position in the original query, the longest
matching source phrase wins. The original is always retained. Rules run once
against the original query: generated alternatives never trigger more rules.
An explicitly different reverse or chained rule applies only when its source
appears in the original query. Duplicate normalized source phrases are refused.

Each word is one alphabetic Unicode token of at most 64 UTF-8 bytes before and
after lowercasing. Query words use whitespace boundaries; the query has at most
eight words and 256 bytes. Matching uses Tantivy's `simple_lower_v1` analyzer,
with positions and lowercase but no stemming, stopwords or synonym injection at
index time. Literal Chinese words are accepted as supplied; Chinese segmentation,
canonical accent normalization and language-specific synonym morphology remain
unsupported. Identifier digits/punctuation and operator symbols are refused.
There is no implicit Boolean grammar, fuzzy expansion or phrase slop in this API.

A policy has a 1–64-byte ASCII alphanumeric/underscore/dot/hyphen ID, revision
1–1,000,000,000 and 1–16 rules. Each source phrase has 1–4 words, and each rule
has 1–4 destination phrases of 1–4 words. The SQL policy JSON is at most 8192
bytes. Complete expansion permits at most 32 distinct phrases, 16 words per
phrase and 256 total term occurrences. Exceeding expansion bounds refuses the
whole query with `54000`; malformed input uses `22023`. Nothing is truncated to
produce a partial result.

Results include the policy ID/revision, a SHA-256 digest of its canonical
normalized contents, complete expanded phrases, applied-rule count, limits and
execution semantics. Canonicalization sorts rules and deduplicates/sorts
alternatives after lowercasing; request rule order does not change the digest.
The digest also includes ID and revision. Reusing an ID/revision with different
contents produces a different digest; no registry enforces immutable revisions.
Applications retain the explicit policy and returned digest to reproduce a query.

Mandatory source matching/filter conditions enter the complete snapshot before
expansion candidates are selected. The existing 1000-live-point/1-MiB snapshot,
native ownership, generation/epoch, source fingerprint, exact durable receipt
and post-execution SQL source checks remain required. Concurrent source changes
reject the whole response; SQL cancellation retains ownership of native work
until it finishes. RLS and unsupported transaction snapshots are refused.
Snippets contain actual original-source term highlights, including matched
aliases, under the same source permissions and UTF-8 offset contract.

This scope does not establish large-corpus indexing, general search-mode
synonyms, cross-engine fusion, persisted policy upgrades, stemming/segmentation
compatibility, or held-out query quality. Those remain formal requirements.

Scoped local validation `synonyms-9fd6ba48f68ce55b` rebuilt the helper, extension
and installation SQL with locked dependencies. Native tests passed (26 helper,
2 auxiliary and 23 protocol); seven installed PostgreSQL test groups passed
directional/multiword/Unicode matching, canonical identity and expansion limits,
actual-source highlights/counts, permissions, ordinary DML/key reuse, in-flight
source changes, cancellation, explicit RLS refusal and DDL re-registration.
The input overlay and log hashes, installed binary/SQL hashes and test report
are retained locally. The full revision crash matrix remains pending for this
addition; this scoped result does not close formal capability acceptance.
