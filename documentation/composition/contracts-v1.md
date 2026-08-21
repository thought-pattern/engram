# Bounded graph composition contracts v1

Section 10 composes only canonical Claim projections obtained through Engram's fixed, parameterized one-hop capability. It does not accept caller Cypher, graph labels, property names, or executable query fragments.

## Algebra and plan

`GraphCompositionOperator` is closed to `LOOKUP`, `EXISTS`, `COUNT`, `AND`, `OR`, `NOT`, `MIN`, `MAX`, and `ORDER`. A `CompositionPlan` contains an exact versioned field set: operator, selected root identity and label, ordered steps, terminal binding, aggregation inputs, sort direction, and non-time resource limits.

Each step declares one branch and hop, its input binding, an explicit root identity only on the first hop, one canonical Predicate ID and label, its output binding, expected object type, and candidate limit. Bindings use bounded `$name` identifiers. Every branch begins at `$root`, consumes only its preceding output, ends at the common terminal binding, and has contiguous hop numbers. This rejects unbound inputs, cartesian expansion, binding cycles, missing Predicate identities, excess branches, and unsupported fields.

The v1 maxima are:

| Resource | Maximum |
| --- | ---: |
| Hops per branch | 2 |
| Graph rows, including by-ID revalidation | 64 |
| Logical branches | 4 |
| Candidates per step | 8 plus one completeness sentinel read |
| Claims per evidence path | 2 |
| Binding UTF-8 bytes | 64 |
| Predicate lookup surfaces | 12 |

There is no latency answer budget, knowledge deadline, p95 gate, or timeout field in the plan. Cooperative cancellation is checked before lookup and during traversal. An enabled Memgraph deployment separately requires the server-side query-execution timeout in the [deployment runbook](../operations/deployment-and-rollback-v1.md), because a cooperative Python check cannot interrupt a driver call already in progress. Durations are reported as observations and never determine answer eligibility.

## Compiler boundary

The structured resolver first delegates ordinary one-hop questions to the Section 8 relation path. A conservative composed request must expose a possessive or `of` structure, resolve exactly one canonical root Entity, and resolve exactly two ordered Predicate occurrences from at most 12 one- to three-token surfaces. Repeated uses of the same Predicate are preserved when they occur at distinct text positions. Ambiguous, missing, unsupported, or underconstrained identities abstain with a typed reason.

The compiler emits only the closed plan above. Runtime traversal invokes `relation_one_hop_claim_projections(subject_id, predicate_id, limit, include_historical)` for each bound state. It cannot substitute a dynamic query template.

## Execution and answer safety

Each discovered Claim passes the common temporal and visibility evaluator and is then re-read by Claim ID immediately before publication. Subject and Predicate identities must equal the active binding. Object types must be known and match when the step declares a concrete type. Entity histories reject cycles. Complete and partial paths are Claim-ID deduplicated and canonically ordered; terminal entities are deduplicated by canonical ID, while inconsistent labels or types fail closed.

The executor uses a one-row sentinel to detect per-step truncation. Row exhaustion, candidate truncation, branch/path limits, dependency failures, and cycles retain any established prefix as evidence. A truncated result cannot become direct unless the Boolean result is already invariant: a found `EXISTS` or a true `OR` cannot be changed by unseen rows.

Boolean operators return a value only when the necessary branches are complete or a positive result is invariant. `COUNT` requires known Predicate cardinality, complete traversal, and no duplicate terminal cardinality. `MIN`, `MAX`, and `ORDER` require complete NUMBER or DATE terminal values. Missing types, unknown completeness, non-finite numbers, invalid dates, and result-changing truncation suppress direct output.

The resolver additionally requires supplied trust and trust-version fields and non-unknown Predicate cardinality on every direct path. Historical direct output requires closed bounds on the requested temporal axis. These checks reuse Section 9 policy; known `MULTI` metadata is not treated as unknown.

## Evidence wire version 2

Section 7 schema-1 records retain singleton string paths. A composed record uses `ClaimEvidenceRecord.schema_version = 2`, which advances the package wire version to 2 and replaces the singleton path with one or two exact `ClaimEvidencePathStep` objects.

Each path step contains only:

- schema version and zero-based position;
- Claim, subject Entity, Predicate, and object Entity IDs;
- algebra operator;
- input and output bindings;
- allow-listed filters (`canonical_identity`, `object_type`, `publication_revalidation`, `temporal_eligibility`, and `visibility`); and
- terminal aggregation bindings when applicable.

Validation requires contiguous positions and bindings, adjacent subject/object continuity, unique Claim IDs, no Entity cycle, inclusion of the record's terminal Claim ID, and the existing ten-record/64-KiB package limits. Arbitrary graph properties, prose, proof bodies, Cypher, and embeddings remain outside the evidence contract.

## Current live behavior

The configured MemGraph probe resolves Sarah → `married_to` → Abraham → `married_to` → Keturah through the real structured resolver. The competing Abraham → Sarah edge is identified as a cycle. The result is `EVIDENCE`, not a direct answer, because the live Claim metadata does not meet all direct trust/cardinality requirements. The ordered two-Claim path is retained in [the live report](live-memgraph-2026-08-20.json).
