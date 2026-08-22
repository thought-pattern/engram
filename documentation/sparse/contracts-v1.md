# Sparse retrieval contract v1

**Status:** Section 12 implementation contract  
**Date:** 2026-08-21  
**Owner:** EGR-1201 through EGR-1208

## Authority and enablement

Persisted accepted-response artifacts are authoritative. The sparse index is an
in-memory projection that may be discarded and rebuilt without losing knowledge.
It is disabled by default through `sparse.enabled: false` and adds no service,
model, artifact-download, or persisted-index dependency.

Enabling sparse retrieval permits the resolver to contribute candidates; it does
not authorize a direct answer. Every candidate still passes shared scope,
lifecycle, source, metadata, temporal, fusion, and ambiguity rules.

## Document schema

Each active accepted-response artifact produces one version-1 document.

| Field | Weight | Source | Default behavior |
| --- | ---: | --- | --- |
| `canonical` | 3.00 | Canonical retrieval request | Included |
| `aliases` | 2.50 | Retrieval aliases | Included |
| `entities` | 2.25 | Entity surfaces and canonical IDs | Included |
| `relation` | 2.00 | Relation surface and canonical ID | Included |
| `keywords` | 1.50 | Query-identity lexical terms | Included |
| `technical_identifiers` | 3.50 | Extracted from the preceding request fields | Included |
| `response_text` | 0.25 | Accepted response prose | Excluded unless `include_response_text: true` |

The low, opt-in response weight prevents answer prose from becoming the primary
intent representation. Documents retain statement ID, exact `ScopeKey`, lifecycle,
schema version, normalized field text, field tokens, and technical identifiers.
Field values and tokens have fixed count and byte bounds.

## Normalization and technical tokenization

Natural-language text uses Unicode NFKC normalization, case folding, bounded word
tokens, and the existing stopword set. Technical extraction additionally retains
meaningful punctuation in paths, versions, error codes, namespaced values, mixed
letter/digit identifiers, and camel-case identifiers. Examples include
`C:\\api\\v2\\users`, `/srv/api/v2/users`, `ERR_CONNECTION_RESET`, `v2.4.1`,
`std::vector`, and `customerID42`.

Canonical and alias tokens of at least six characters and all technical identifiers
produce bounded prefixes. Technical identifiers also produce character trigrams.
The query path applies the same normalization and limits prefix expansion.

## Scoring and ordering

The base score is BM25 with `k1 = 1.2`, `b = 0.75`, per-field document lengths,
document frequency, and the fixed field weights above. Bounded additive signals
cover:

- exact phrase presence;
- minimum token proximity within eight positions;
- prefix match ratio;
- technical character-trigram similarity; and
- exact technical-identifier ratio.

Scores are normalized to `[0, 1]`. Results sort by descending score and then
statement ID, making ties deterministic. Candidate features expose the normalized
score and each sparse signal. Candidate diagnostics expose field contributions,
phrase/proximity fields, minimum proximity, prefix count, and matched-term count.
Resolver diagnostics expose query-term and posting-visit counts.

## Bounds and absence

Configuration fixes positive limits for query terms, posting visits, and prefix
expansions. The common resolver budget additionally limits candidates and estimated
working memory. Searches are complete-or-abstain:

- exceeding query, posting, or memory limits returns no partial candidates and a
  typed exhausted reason;
- a disabled or unhealthy index returns no candidates and `sparse_unavailable`;
- an ordinary complete miss returns no candidates and `sparse_miss`.

Sparse failure is isolated to this optional resolver. It never changes mutation
success and cannot prevent exact, pattern, lexical, graph, conversation, or
regulated-response paths from operating.

## Index lifecycle

`SparseIndexOwner` owns one immutable generation. A clean rebuild constructs and
validates a complete candidate off-live, then atomically swaps the reference.
Readers retain either the old complete generation or the new complete generation.

Committed, superseded, invalidated, retired, and capacity-evicted statement IDs are
synchronized from the mutation receipt. Only affected posting tuples are replaced.
Repository changes that affect only accounting/statistics update the repository
generation while retaining the exact postings object. Persistence load rebuilds
the sparse projection from decoded authoritative artifacts; postings are never
written to the JSON store.

The explicit consistency check independently reprojects authoritative artifacts
and compares:

- configuration fingerprint;
- self/document fingerprint;
- authoritative content; and
- posting and average-field-length structure.

A failed synchronization marks the index unhealthy, logs only the exception class,
and leaves mutation outcome authoritative. Operators may inspect the check report
and perform a clean rebuild.

## Compatibility

Document schema, index schema, index implementation, and tokenizer versions are all
`1`. The configuration fingerprint binds index/tokenizer versions, response-text
selection, and field weights. A mismatch is not adapted in place; the disposable
projection is rebuilt from authoritative state.

## Configuration

```yaml
sparse:
  enabled: false
  include_response_text: false
  max_query_terms: 64
  max_posting_visits: 100000
  max_prefix_expansions: 64
```

The operational inspection methods are `sparse_index_snapshot()`,
`check_sparse_index()`, and `rebuild_sparse_index()`. Transport-neutral
`core.status()` also reports `components.sparse.enabled` and
`components.sparse.ready`. Sparse remains optional: an unavailable index reports
not ready without changing overall core readiness or health.

## Engineering evidence

The [synthetic benchmark corpus](../../eval/section12-sparse-v1.json) and
[reproducible result](benchmark-2026-08-21.json) compare all four evaluated engines
and record relevance, p50/p95 latency, startup build, memory, persisted bytes, and
write amplification. The [decision record](../decisions/0006-section12-fielded-bm25.md)
and [license inventory](provenance-and-licensing-2026-08-21.md) complete the engine
selection evidence. These are repository-visible engineering fixtures; they are
not independent release evidence.
