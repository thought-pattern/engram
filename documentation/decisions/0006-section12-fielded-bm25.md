# ADR 0006: Rebuildable fielded BM25 for sparse retrieval

- Status: Accepted
- Date: 2026-08-21
- Applies from: Section 12
- Supersedes: No prior decision

## Context

Engram's IDF-weighted keyword overlap is deliberately small and predictable, but
it does not model field importance, phrase order, token proximity, prefixes, or
technical identifiers. Section 12 requires a local sparse-retrieval option that
improves those cases without making a secondary index authoritative or adding an
external service dependency.

The repository-visible engineering benchmark compares the existing IDF overlap,
unfielded BM25, SQLite FTS5, and a fielded BM25 implementation. Its 28 synthetic
queries cover phrases, proximity, prefixes, character trigrams, symbols, versions,
paths, error codes, identifier boundaries, technical identifiers, and negative
queries. The comparison is component evidence, not a Section 16 release
partition.

## Decision

Engram selects `fielded_bm25_v1`, implemented in project-owned Python code, as an
optional resolver that is disabled by default.

The selected index:

- projects only accepted-response artifacts and never becomes authoritative;
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
eligibility, fusion, and accounting contracts. It cannot bypass scope, lifecycle,
metadata, temporal, source, or policy checks.

## Alternatives

### Existing IDF overlap only

It has the lowest conceptual cost and remains available, but reached 13/24 positive
top-one cases and 20/24 recall-at-five cases in the Section 12 comparison. It did
not satisfy the declared relevance gate.

### Unfielded BM25

It reached 23/24 top-one and 24/24 recall-at-five, but it cannot expose the required
field contributions and gives up the explicit authority distinction between query
representations and opt-in response prose.

### SQLite FTS5

It also reached 23/24 top-one and 24/24 recall-at-five and was fastest in the small
comparison. It requires a Python SQLite build compiled with FTS5, and its query
syntax/tokenization would need additional adaptation for the required technical
signals and complete-or-abstain resource accounting. It remains a valid future
candidate if scale evidence changes the decision.

### External search service

Rejected for this stage because Section 12 requires local, offline execution and
does not justify another service, persisted authority, or deployment boundary.

## Consequences

- Enabling sparse retrieval increases startup CPU and memory because the index is
  rebuilt from authoritative artifacts.
- The 10,000-document engineering profile records 8,708 ms p50 build time,
  9,545 ms p95 build time, 0.361 ms p50 query time, 0.440 ms p95 query time,
  and 366,632,633 bytes peak measured memory.
- One incremental add took 52.363 ms, replaced 0.241% of posting terms, and matched
  a clean rebuild; statistics-only changes replaced no postings.
- Corruption or synchronization failure makes only the sparse resolver unavailable.
  Other local and optional resolvers continue according to the common plan.
- Changing document, tokenizer, index, field-weight, or response-text semantics
  requires a version/configuration-fingerprint change and a clean rebuild.
- Section 16 retains release authority. This decision promotes the component only.

## Verification basis

The decision is supported by the [version-1 sparse contract](../sparse/contracts-v1.md),
[benchmark corpus](../../eval/section12-sparse-v1.json),
[benchmark artifact](../sparse/benchmark-2026-08-21.json), and
[provenance/license inventory](../sparse/provenance-and-licensing-2026-08-21.md).
