# ADR 0006: Rebuildable fielded BM25 for sparse retrieval

- Status: Accepted
- Date: 2026-08-21
- Applies from: Sparse retrieval introduction

## Context

Engram's IDF-weighted keyword overlap is small and predictable. Fielded sparse
retrieval adds field importance, phrase order, token proximity, prefixes, and
technical identifiers while accepted-response artifacts retain authority.

The repository-visible engineering benchmark compares the existing IDF overlap,
unfielded BM25, SQLite FTS5, and a fielded BM25 implementation. Its 28 synthetic
queries cover phrases, proximity, prefixes, character trigrams, symbols, versions,
paths, error codes, identifier boundaries, technical identifiers, and negative
queries.

## Decision

Engram selects `fielded_bm25_v1`, implemented in project-owned Python code, as an
optional resolver that is disabled by default.

The selected index:

- projects accepted-response artifacts while repository artifacts retain authority;
- indexes canonical request, aliases, entities, relation, keywords, technical
  identifiers, and response text only when explicitly enabled;
- applies fixed version-1 field weights and tokenizer/index version markers;
- combines fielded BM25 with bounded phrase, proximity, prefix, character-trigram,
  and exact-technical-identifier signals;
- publishes immutable generations by atomic reference replacement;
- performs incremental posting replacement for affected statements and reuses the
  existing postings object for statistics-only repository changes;
- supports an explicit clean rebuild and authoritative-state consistency check;
- persists zero index bytes; and
- returns complete candidates or a typed unavailable/resource-exhausted result.

The resolver participates through the common `Candidate`, `FeatureSet`, budget,
eligibility, fusion, and accounting contracts, including scope, lifecycle,
metadata, temporal, source, and policy checks.

## Alternatives

### Existing IDF overlap only

It has the lowest conceptual cost and remains available, but reached 13/24 positive
top-one cases and 20/24 recall-at-five cases in the sparse-retrieval comparison,
below the declared relevance gate.

### Unfielded BM25

It reached 23/24 top-one and 24/24 recall-at-five. Fielded BM25 also exposes field
contributions and separates query representations from opt-in response prose.

### SQLite FTS5

It also reached 23/24 top-one and 24/24 recall-at-five and was fastest in the small
comparison. It requires a Python SQLite build compiled with FTS5, and its query
syntax/tokenization would need additional adaptation for the required technical
signals and complete-or-abstain resource accounting. It remains a valid future
candidate if scale evidence changes the decision.

## Consequences

- Enabling sparse retrieval increases startup CPU and memory because the index is
  rebuilt from authoritative artifacts.
- The 10,000-document engineering profile records 8,708 ms p50 build time,
  9,545 ms p95 build time, 0.361 ms p50 query time, 0.440 ms p95 query time,
  and 366,632,633 bytes peak measured memory.
- One incremental add took 52.363 ms, replaced 0.241% of posting terms, and matched
  a clean rebuild; statistics-only changes preserved postings.
- Corruption or synchronization failure makes only the sparse resolver unavailable.
  Other local and optional resolvers continue according to the common plan.
- Changing document, tokenizer, index, field-weight, or response-text semantics
  requires a version/configuration-fingerprint change and a clean rebuild.
- Project qualification retains release authority; this decision selects the component.

## Verification basis

The decision is supported by the [local resolver contracts](../local-resolvers.md),
[benchmark corpus](../../eval/section12-sparse-v1.json), and
[provenance/license inventory](../provenance-and-licensing.md).
