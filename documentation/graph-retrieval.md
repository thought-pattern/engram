# Graph retrieval contracts

**Status:** Current implemented behavior

Graph retrieval is optional and read-only. It enriches a resolution request with
canonical identity and retrieves bounded Claim evidence through fixed parameterized
capabilities with typed identity and relation inputs.

## Context and canonical identity

`EngramCore` retains one compact previous frame per user for bounded elliptical
follow-ups. The state contains only the prior request, resolved text, canonical
entities, relation, object type, inheritance provenance, evaluation time, and
knowledge epoch. It expires with the configured context TTL; accepted knowledge
remains in the repository.

Contextual enrichment may inherit an unambiguous prior subject when the current
request supplies a relation but omits the subject. Canonical entity and predicate
resolution uses bounded fixed graph lookups. Missing, ambiguous, conflicting, or
unavailable identity produces typed diagnostics and an `EVIDENCE` or `MISS` outcome.

## One-hop relations

The relation path compiles one canonical subject and predicate into the fixed
`relation_one_hop_claim_projections` capability. Returned rows are strict
`ClaimProjection` values plus a bounded object label and type. Current lifecycle,
system time, valid time, trust inputs, ownership, scope, and publication identity are
revalidated before a row becomes evidence.

A unique eligible `SINGLE` relation may become a direct candidate through the common
fusion policy. Multiple values, unknown cardinality, ambiguity, or incomplete trust
remain evidence. Rendering changes the bounded output phrase and preserves the
canonical Claim record.

## Temporal and conflict policy

Temporal parsing supports current requests, `as of`, `during`, and `latest`, with an
explicit valid-time or system-time axis. Intervals are lower-inclusive and
upper-exclusive. Historical direct output requires closed bounds on the requested
axis. Unqualified requests use the captured evaluation time.

Engram consumes source-calibrated trust and its version exactly as supplied by the
Claim. Known `MULTI` cardinality yields evidence; conflicting `SINGLE` rows abstain.

## Two-hop composition

Composition accepts a conservative possessive or `of` form that resolves one root
entity and exactly two ordered predicates. The compiler emits only a closed two-step
plan over the one-hop capability. Execution is bounded by request budgets and uses
the same temporal, trust, visibility, and revalidation rules at both steps.

Composed evidence uses `ClaimEvidenceRecord` schema version 2 and evidence-package
wire version 2. Its path contains one or two typed steps. Cycles, excessive fan-out,
ambiguous roots or predicates, conflicting terminal values, incomplete trust, and
unknown cardinality produce `EVIDENCE` or `MISS`.

## Availability and timing

Graph readiness is reported separately from core readiness. A graph connection,
query, or optional vector-index failure makes the affected resolver unavailable and
local resolvers continue. Cooperative cancellation is checked around graph calls but
an executing driver call continues until the driver returns.

Evaluation artifacts report p50, p95, p99, and maximum resolution time.

Focused behavior is covered by `tests/test_contextual.py`, `tests/test_temporal.py`,
`tests/test_composition.py`, `tests/test_claim_projection.py`, and
`tests/test_claim_eligibility.py`.
