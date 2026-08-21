# Section 6 feedback and negative-resolution contracts v1

## Scope and ownership

Section 6 adds two transport-neutral features. Typed external Regulator observations become durable, bounded feedback aggregates; completed knowledge misses may become short-lived, memory-only negative-resolution records. Neither feature creates a statement, Claim, response, evidence item, or implicit Regulator label.

Section 3 remains authoritative for artifact generations, lifecycle transitions, epochs, and accepted-response mutation receipts. Section 4 remains authoritative for unique candidacy and accepted-use accounting. Section 5 remains authoritative for fusion vocabulary, hard eligibility, policy, and selection. Section 15 owns adapter exposure, authorization, cross-feature schema orchestration, configuration, telemetry, and deployment. Section 16 owns empirical calibration and release gates.

The first feedback contract is `feedback-v1.0.0`, fingerprint `766ecf9a4a69e0ff791893ca28e2de5de9eccba99ed6fff1a9e308439f6ccc5f`. The hand-authored history policy fingerprint is `96d06507ef4ef512a4a8def64fd141b955709c74bb71bf89b7372bccb18f05e5`. The consumed Section 5 policy fingerprint is `1f9d19acaedc277b4916bc366e74dc8c03d921e335acd21ac7fc2f1b449d963c`.

## Feedback target and ingestion

`FeedbackObservation` binds one observation to:

- an authoritative resolution-request or regulated-proposal reference;
- candidacy or external-verdict kind and a closed outcome;
- the typed `QueryIdentity`, its exact `ScopeKey`, normalization version, and a canonical constraint fingerprint;
- statement ID, observed generation, and explicit generation availability;
- feedback-contract and fusion-policy fingerprints;
- a server-injected canonical UTC observation time; and
- a bounded optional external reason.

The v1 external verdict vocabulary is `accepted`, `rejected_quality`, `rejected_context`, `rejected_stale`, and `rejected_policy`. Every v1 verdict requires exactly one candidate statement target. Proposal-wide and statement-free policy observations are deliberately absent until their authority and isolation semantics are specified. Internal candidacy uses only the `candidate` outcome and consumes Section 4's deduplicated candidate statement set. An Engram ANSWER is never converted into external acceptance feedback.

`FeedbackStore` prepares a complete off-live candidate and a `RECORD_FEEDBACK` receipt. The application owner checkpoints that candidate before publishing it. Exact retries replay; the same request ID with different identity, target, generation, constraints, outcome, reason, contract, or policy conflicts. Server-injected observation time and lifecycle execution status are excluded from the payload signature so a later exact transport retry replays the original application and its original lifecycle result rather than conflicting or reattempting lifecycle work. Observation time remains stored in the aggregate and receipt creation time.

One request accepts from 1 through 1,000 unique observations. Retry identity uses a domain-separated streaming SHA-256 over the canonical validated observations, bounded by 64 MiB of canonical business input, so the declared batch limit does not inherit the generic receipt codec's nested-item ceiling. The receipt stores only the resulting fixed-size signature.

The regulated `propose`/`resolve` compatibility path and modern `resolve_request`/`record_resolution_feedback` path use this same owner. The core lock serializes external verdict publication; receipt identity prevents duplicate application across retries and restarts.

## Aggregate partitions and effects

Statement records are keyed by statement ID, observed generation availability and value, contract fingerprint, and fusion-policy fingerprint. Relationship records add the typed identity, exact `ScopeKey`, and constraint fingerprint. Equivalent text, different statement owners, replacement IDs, scopes, constraints, normalization versions, contracts, and policies do not merge. A current immutable statement ID may consume observations from its earlier statistic-only generations; replacements use a new statement ID.

Raw counters are retained separately for candidacy, acceptance, and each rejection. Legacy artifact `query_count` and `hit_count` remain accepted-use statistics in their existing partition and never seed typed Regulator feedback.

| Outcome | v1 effect |
| --- | --- |
| `accepted` | Increases the targeted statement and exact relationship acceptance counts; clears a matching statement/namespace/policy suppression. |
| `rejected_quality` | Increases the targeted relationship count and the statement reliability rejection input within the contract and policy partition. |
| `rejected_context` | Increases only the exact relationship rejection input; it does not weaken statement reliability. |
| `rejected_stale` | Adds a generation-scoped feedback exclusion and requests Section 3 invalidation with expected generation. |
| `rejected_policy` | Adds a statement/namespace/fusion-policy suppression; it does not assert global falsehood or change lifecycle. |

Stale handoff results are `pending`, `completed`, `conflicted`, or `failed` (`not_applicable` for other outcomes). Completed lifecycle receipt replays normalize to `completed`, keeping the feedback receipt signature stable after a feedback-checkpoint retry. The feedback exclusion is authoritative for fusion even when the Section 3 transition is pending, conflicted, or failed.

## Aging, retention, and fusion history

The unfitted `feedback-history-v1.0.0` policy uses:

- a five-verdict availability floor;
- Beta-style bounded priors of one accept and three rejects;
- a 30-day half-life;
- daily deterministic buckets represented by their interval end;
- at most 32 buckets per record;
- at most 10,000 statement records and 50,000 relationship records; and
- deterministic oldest-observation/key eviction.

Raw counters remain inspectable while old buckets compact out. Reads never refresh or extend standing. Statement reliability uses accepted versus quality-plus-stale rejection. Relationship reliability uses accepted versus all four rejection types. If both aged partitions meet the sample floor, their posterior values are averaged; otherwise the available partition is used. The feature is concretely unavailable below the floor.

When Section 3 accepted-use history and typed feedback history are both available, `EngramCandidateAuthority` averages them into Section 5 `HISTORY`. When only one is available, it uses that input. Existing hit/query counts are not relabeled as Regulator outcomes. Feedback cannot bypass lifecycle, epoch, scope, metadata, source, support, ambiguity, or other Section 5 hard gates.

## Persistence and recovery

`feedback_state` is an additive feature-owned block in persistence v2. Existing v1 and v2 files without it load an empty feedback state; their hit/query counters remain separate. The block has strict version-1 codecs for policy, keys, observations, aggregate records, buckets, suppressions, exclusions, receipts, and eviction counters. Unsupported versions and unknown or missing fields fail closed.

Checkpoint writes serialize a complete off-live feedback candidate with current Engram state. On an uncertain write failure, recovery compares deterministic before/after state signatures. A durable after-state is published; a durable before-state causes a retryable persistence error without live feedback publication; divergent state is treated as degraded. Receipts survive restart.

A replay reports `durable` only when the mutation was checkpointed or the current owner is clean against a configured durable store. Disabling checkpoint-on-mutation therefore keeps both the original result and its replay explicitly non-durable. Immutable aggregate records and cached canonical snapshots permit off-live candidate preparation to copy indexes rather than serialize and reconstruct the full state on every observation; strict state parsing reuses validated identity, scope, statement-key, timestamp, and fingerprint values without changing the persisted codec.

## Negative-resolution contract

`NegativeResolutionKey` contains the exact typed identity and `ScopeKey`, constraint fingerprint, available namespace knowledge epoch, normalization version, resolver-plan fingerprint, capability-readiness fingerprint, and fusion-policy fingerprint. Reason and expiry are record values. An unavailable epoch cannot construct, admit, or reuse a key.

Admission is limited to `insufficient_knowledge` when:

- the unified result is MISS;
- every configured resolver is available and completed with its source's explicit knowledge-miss reason;
- no resolver returned a candidate, evidence, or accounting observation;
- exact lookup had zero owners and was not truncated;
- no resource dimension, truncation, failure, collision, dependency, or indeterminate state occurred; and
- trusted time, authoritative repository availability, and knowledge epoch are available.

Policy-filtered candidates, required-filter exclusions, collisions, unavailable dependencies, failures, and exhausted execution are not admitted. This is intentionally stricter than treating every MISS as absent knowledge.

The v1 owner is memory-only, defaults to a fixed non-sliding five-minute TTL and 1,000 records, and supports deterministic oldest-created/key eviction. Lookup atomically expires records and invalidates related entries whose epoch, normalization, plan, readiness, or policy state changed. Response-state publication invalidates entries against the new namespace-epoch snapshot.

The current namespace epoch is authoritative only for accepted-response repository mutations. Consequently, v1 negative admission and reuse are restricted to an exact-only configured resolver plan. A plan containing pattern, lexical, structured-graph, or semantic sources performs ordinary resolution and invalidates a related exact negative record instead of risking a stale miss after those independently mutable knowledge sources change. Extending negative reuse to those sources requires a common authoritative knowledge-version contract.

A hit returns a bounded typed MISS with `negative_resolution_hit` and `insufficient_knowledge`, zero resolver accounting, and measured elapsed/output/diagnostic consumption. Diagnostic content is omitted, with diagnostic exhaustion recorded, when it cannot fit the request's diagnostic-byte limit; output or working-memory overflow fails open to ordinary resolution. Elapsed time is reported and does not affect the hit. Same-request retries use the transient resolution-result cache before negative lookup. Concurrent first misses may perform duplicate ordinary work, but insertion converges to one exact record; this preserves fail-open behavior and avoids making the optimization a request gate. Any negative-owner exception falls through to ordinary resolution.

## Inspection and operational boundary

`inspect_feedback_learning` returns bounded transport-neutral snapshots. Feedback inspection includes policy fingerprints, record and receipt counts, typed receipt outcomes, lifecycle results, raw counters, bucket counts, eviction totals, hashed diagnostic IDs, and omission counts. Negative inspection includes occupancy, TTL/capacity, admission, lookup, hit, miss, expiry, eviction, invalidation, bounded records, and omission counts. Raw request text and unbounded identifiers are not metric labels.

No MCP or gRPC method is added in Section 6. Adapter authorization, redaction, low-cardinality production metrics, dashboards, configuration, and rollout remain Section 15 work.
