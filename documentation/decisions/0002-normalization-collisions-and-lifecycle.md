# ADR 0002: Versioned normalization, collision rejection, and lifecycle concurrency

- Status: Accepted
- Date: 2026-08-11
- Applies from: Increment A

## Context

The current learned-response path derives a set of lexical keywords and replaces an entry with the same scoped set. That erases identity-bearing words such as `when` and `where`. There is no exact or alias identity index, lifecycle is inferred from tier and physical presence, and retirement is allowed only for DYNAMIC patternless statements.

## Decision

Normalization version 1 (`n1`) is an identity-preserving algorithm:

1. validate bounded Unicode text and apply Unicode NFKC;
2. case-fold, normalize recognized apostrophe variants, trim, and collapse whitespace;
3. preserve interrogative operator, negation, count, comparison, temporal, location, and other qualifiers before any lexical filtering;
4. preserve internal technical punctuation in identifiers, versions, symbols, paths, error codes, and URLs;
5. preserve symbolic comparisons and identity-bearing technical operators, while treating Unicode prose dashes and non-semantic edge punctuation as separators; and
6. serialize the typed `QueryIdentity` fields and exact scope deterministically.

The Section 1 remediation corrected normalization v1 before it was released, persisted, indexed, or exposed through Python, MCP, or gRPC integration fields. The remediated fixture is therefore the first releasable v1 keyspace rather than a migration to v2. It adds explicit adversarial cases for `<`, `>`, `<=`, `>=`, `==`, `!=`, `$`, `%`, `|`, `&`, and `*`; any subsequent key-changing behavior requires v2.

Canonical requests and retrieval aliases use the same explicit version. AIML `pattern_aliases` remain executable matcher data and are never used as retrieval aliases. Any behavior change that can alter an identity key requires a new normalization version; existing keys are not silently reinterpreted.

A canonical or alias key may map to only one ACTIVE artifact in the same exact scope. A conflicting commit is rejected. Replacement requires explicit supersession naming the expected current statement ID and generation. Legacy collisions may coexist only in a quarantined, non-direct-answer state until repaired; they are never resolved by ordering or score.

Lifecycle is independent of eviction tier and has four persisted states: ACTIVE, SUPERSEDED, INVALIDATED, and RETIRED. All identity, artifact, lifecycle, and derived-index mutations occur under the core's re-entrant mutation lock and commit as one live-state transition. Supersession and other competing transitions use optimistic concurrency through expected statement ID and generation. A retry with the same request identity and payload returns the original result; a changed payload conflicts.

## Consequences

- `when`/`where`, current/historical, positive/negative, and similar pairs cannot replace each other merely because their lexical keywords match.
- Migration must quarantine ambiguous legacy keys rather than guess.
- Tier continues to control capacity eviction only; it no longer implies truth, eligibility, or lifecycle.
- Increment A needs adversarial normalization fixtures and concurrent transition tests before direct exact lookup is enabled.
