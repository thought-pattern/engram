# ADR 0002: Versioned normalization, collision rejection, and lifecycle concurrency

- Status: Accepted
- Date: 2026-08-11
- Applies from: Accepted-response identity and lifecycle

## Context

Accepted responses need identity-preserving normalization, exact canonical and alias
ownership, and explicit lifecycle transitions.

## Decision

Normalization version 1 (`n1`) is an identity-preserving algorithm:

1. validate bounded Unicode text and apply Unicode NFKC;
2. case-fold, normalize recognized apostrophe variants, trim, and collapse whitespace;
3. preserve interrogative operator, negation, count, comparison, temporal, location, and other qualifiers before any lexical filtering;
4. preserve internal technical punctuation in identifiers, versions, symbols, paths, error codes, and URLs;
5. preserve symbolic comparisons and identity-bearing technical operators, while treating Unicode prose dashes and non-semantic edge punctuation as separators; and
6. serialize the typed `QueryIdentity` fields and exact scope deterministically.

The normalization-v1 fixture defines the released keyspace and includes cases for `<`, `>`, `<=`, `>=`, `==`, `!=`, `$`, `%`, `|`, `&`, and `*`. Any key-changing behavior requires v2.

Canonical requests and retrieval aliases use the same explicit version. AIML
`pattern_aliases` remain separate executable matcher data. Key-changing behavior
requires a new normalization version; existing keys retain their assigned version.

A canonical or alias key maps to one ACTIVE artifact in an exact scope. A conflicting
commit is rejected. Replacement requires explicit supersession naming the expected
statement ID and generation. Legacy collisions remain quarantined until repaired.

Lifecycle and eviction tier are separate fields. Lifecycle has four persisted states:
ACTIVE, SUPERSEDED, INVALIDATED, and RETIRED. All identity, artifact, lifecycle,
and derived-index mutations occur under the core's re-entrant mutation lock and
commit as one live-state transition. Supersession and competing transitions use
optimistic concurrency through expected statement ID and generation. A retry with
the same request identity and payload returns the original result; a changed payload
conflicts.

## Consequences

- Identity preserves `when`/`where`, current/historical, positive/negative, and similar pairs.
- Migration quarantines ambiguous legacy keys.
- Tier controls capacity eviction; lifecycle controls truth and eligibility.
- Adversarial normalization fixtures and concurrent transition tests verify direct exact lookup.
