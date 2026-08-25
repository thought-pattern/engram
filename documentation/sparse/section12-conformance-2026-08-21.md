# Section 12 sparse retrieval conformance — 2026-08-21

## Outcome

Section 12 is component-complete. Engram now has a default-off, project-owned,
fielded BM25 resolver for technical and long-tail retrieval. The index is a
disposable in-memory projection of authoritative accepted-response artifacts,
integrates through the common resolver/fusion/budget contracts, and passed every
declared engineering promotion gate.

This is component promotion, not release approval. The corpus and results are
repository-visible engineering evidence; Section 16 owns project evaluation,
rollout, and release authority.

## Task evidence

| Task | Evidence |
| --- | --- |
| EGR-1201 | The [version-1 contract](contracts-v1.md) fixes seven fields and weights: canonical 3.0, aliases 2.5, entities 2.25, relation 2.0, keywords 1.5, technical identifiers 3.5, and opt-in response text 0.25. |
| EGR-1202 | [`eval/section12-sparse-v1.json`](../../eval/section12-sparse-v1.json) has 28 synthetic documents and 28 queries across phrase, proximity, prefix, character n-gram, symbol, version, path, error-code, technical-identifier, identifier-boundary, and negative families. |
| EGR-1203 | [ADR 0006](../decisions/0006-section12-fielded-bm25.md) and the [benchmark artifact](benchmark-2026-08-21.json) compare existing IDF overlap, unfielded BM25, SQLite FTS5, and fielded BM25 on relevance, latency, operations, licensing, and portability. |
| EGR-1204 | `SparseIndexOwner` constructs immutable generations off-live, atomically replaces the live reference, fingerprints configuration/content, compares full posting structure, rebuilds on recovery, and persists zero index bytes. |
| EGR-1205 | NFKC/case-folded language tokens and bounded punctuation-preserving technical identifiers support paths, versions, error codes, namespaces, symbols, mixed identifiers, prefixes, and character trigrams. Fixed field weights and low opt-in response weight prevent technical or answer-prose noise from dominating ordinary language. |
| EGR-1206 | `SparseResolver` emits `CandidateSource.SPARSE`, normalized score/signal features, field and phrase/proximity diagnostics, posting/query diagnostics, accounting observations, and exact common budget consumption. Sparse and legacy lexical evidence share one fusion family and cannot create false independent agreement. |
| EGR-1207 | The focused suite covers commit, supersession, invalidation, DYNAMIC eviction, persistence recovery, disabled-mode cost, corruption/check/rebuild, unhealthy recovery, scope isolation, read-only generations, concurrent readers, query/posting/memory exhaustion, and clean/incremental equivalence. The scale profile uses 10,000 STATIC documents. |
| EGR-1208 | All 11 declared relevance and operational verdicts pass. The final official-MCP run evaluates and passes every one of 1,000 Sarah turns with sparse enabled and five live MemGraph results. |

## Engine and promotion results

The final source-bound artifacts record governed source SHA-256
`b5a5f892896757b671ac95a79fcd13838fd27abe4d5cf6872451eec71081fc81`.

| Measure | Result | Gate |
| --- | ---: | ---: |
| Positive top-one recall | 24/24 (1.0000) | at least 0.90 |
| Positive recall@5 | 24/24 (1.0000) | 1.00 |
| Existing IDF top-one recall | 13/24 (0.5417) | fielded gain at least 0.08 |
| Negative candidate rate | 0/4 (0.0000) | 0.00 |
| 10,000-document build p50 / p95 | 8,484.0692 / 8,788.0011 ms | p95 at most 15,000 ms |
| 10,000-document query p50 / p95 | 0.3242 / 0.4005 ms | p95 at most 25 ms |
| Peak measured memory | 366,632,633 bytes | at most 536,870,912 bytes |
| Persisted sparse index | 0 bytes | 0 bytes |
| Incremental add | 53.8185 ms | at most 250 ms |
| Incremental posting replacement | 0.002410 | at most 0.10 |
| Statistics-only update p95 | 0.2719 ms | descriptive |
| Statistics-only posting replacements | 0 | 0 |
| Incremental/clean equivalence | exact | required |

SQLite FTS5 and unfielded BM25 each reached 23/24 top-one and 24/24 recall@5.
The selected implementation reached 24/24 while supplying the required field and
technical diagnostics without an extension/service dependency. The complete
[provenance and license inventory](provenance-and-licensing-2026-08-21.md) records
Apache-2.0 project code and the benchmark-only SQLite public-domain dependency.

## Mutation, recovery, and failure behavior

Authoritative mutation receipts identify affected statement IDs. Content changes
replace only affected immutable posting tuples; statistics-only changes retain the
same postings object. Disabled sparse configuration projects no authoritative
content. Failed synchronization never changes mutation success, marks only the
sparse resolver unavailable, and is repaired by a later clean rebuild or explicit
operator rebuild. Stale concurrent rebuilds cannot replace a newer repository
generation.

Queries are scope-filtered before exact technical fallback selection. Query-term,
posting-visit, and working-memory exhaustion return no partial candidates. Sparse
and legacy lexical candidates are one correlated fusion family, so enabling the
feature cannot manufacture independent evidence.

## Verification

- Focused sparse, graph, source-contract, fusion, resolver, readiness, and optional
  graph-isolation tests: **128 passed** in 37.99 seconds.
- Complete repository suite: **1,535 passed** in 96.08 seconds.
- Pyright: **0 errors, 0 warnings, 0 information messages**.
- Ruff: passed; Black: all 132 checked files unchanged after final formatting.
- Bandit: no medium- or high-severity findings; 18 low-severity findings remain
  manually classified in the security review.
- Benchmark: all **11/11** declared verdicts pass.

## Official MCP and live MemGraph

The [final MCP artifact](section12-mcp-sarah-preferences-memgraph-enabled-1000-turns-2026-08-21.json)
uses the official MCP client against the repository MCP server. Its ephemeral
configuration retains the configured MemGraph endpoint and sets `sparse.enabled`
to true. MemGraph reported enabled and ready. Sarah declared that she likes sushi
and cats and dislikes dogs.

All **1,000 requested turns completed**, all **1,000 were evaluated**, and all
**1,000 passed**, with zero failed checks. The run made 1,003 MCP tool calls and
produced 995 pattern responses plus five explicit live-MemGraph responses at turns
200, 400, 600, 800, and 1,000. Observed turn duration was 2.5253 ms p50,
3.7728 ms p95, and 3,810.8931 ms maximum; durations are descriptive and do not
control answer eligibility.

The separate [live probe](live-memgraph-probe-2026-08-21.json) confirms five of
five graph-dependent MCP prompts changed from empty local misses to graph-sourced
responses, resolves canonical Sarah and `married_to`, returns Abraham from the
fixed one-hop query, and retains one Claim through the complete structured resolver.
