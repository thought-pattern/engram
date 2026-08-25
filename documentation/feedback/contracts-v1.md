# Feedback and negative-resolution contracts v1

## Scope and ownership

Typed external Regulator observations become durable feedback aggregates;
completed knowledge misses may become short-lived, memory-only negative records.
Artifact lifecycle remains with the accepted-response repository, candidacy with
resolution, and selection with fusion.

The first feedback contract is `feedback-v1.0.0`, fingerprint `766ecf9a4a69e0ff791893ca28e2de5de9eccba99ed6fff1a9e308439f6ccc5f`. The hand-authored history policy fingerprint is `96d06507ef4ef512a4a8def64fd141b955709c74bb71bf89b7372bccb18f05e5`. The consumed fusion policy fingerprint is `1f9d19acaedc277b4916bc366e74dc8c03d921e335acd21ac7fc2f1b449d963c`.

## Feedback target and ingestion

`FeedbackObservation` binds one observation to:

- an authoritative resolution-request or regulated-proposal reference;
- candidacy or external-verdict kind and a closed outcome;
- the typed `QueryIdentity`, its exact `ScopeKey`, normalization version, and a canonical constraint fingerprint;
- statement ID, observed generation, and explicit generation availability;
- feedback-contract and fusion-policy fingerprints;
- a server-injected canonical UTC observation time; and
- a bounded optional external reason.

The v1 external verdict vocabulary is `accepted`, `rejected_quality`,
`rejected_context`, `rejected_stale`, and `rejected_policy`. Each verdict targets
one candidate statement. Internal candidacy uses `candidate` and resolution's
deduplicated statement set. External acceptance enters through an explicit caller
verdict.

`FeedbackStore` checkpoints an off-live candidate before publication and records a
`RECORD_FEEDBACK` receipt. Exact retries replay; reusing a request ID with a
different business payload conflicts. A request accepts 1–1,000 unique
observations, with retry identity derived from at most 64 MiB of canonical input.
Both public feedback paths use this owner and replay behavior.

## Aggregate partitions and effects

Statement records partition by statement ID, observed generation, contract, and
fusion policy. Relationship records also partition by identity, exact `ScopeKey`,
and constraints. Replacements use a new statement ID.

Raw counters are retained separately for candidacy, acceptance, and each rejection.
Artifact `query_count` and `hit_count` remain in the accepted-use partition.

| Outcome | v1 effect |
| --- | --- |
| `accepted` | Increases the targeted statement and exact relationship acceptance counts; clears a matching statement/namespace/policy suppression. |
| `rejected_quality` | Increases the targeted relationship count and the statement reliability rejection input within the contract and policy partition. |
| `rejected_context` | Increases the exact relationship rejection input; statement reliability remains unchanged. |
| `rejected_stale` | Adds a generation-scoped feedback exclusion and requests artifact invalidation with expected generation. |
| `rejected_policy` | Adds a statement/namespace/fusion-policy suppression; lifecycle remains unchanged. |

Stale handoff results are `pending`, `completed`, `conflicted`, or `failed`
(`not_applicable` for other outcomes). Its feedback exclusion applies in fusion
regardless of lifecycle handoff result.

## Aging, retention, and fusion history

The unfitted `feedback-history-v1.0.0` policy uses:

- a five-verdict availability floor;
- Beta-style bounded priors of one accept and three rejects;
- a 30-day half-life;
- daily deterministic buckets represented by their interval end;
- at most 32 buckets per record;
- at most 10,000 statement records and 50,000 relationship records; and
- deterministic oldest-observation/key eviction.

Raw counters remain inspectable while old buckets compact out. New observations and
aging update standing. Statement reliability uses accepted versus quality-plus-stale
rejection. Relationship reliability uses accepted versus all four rejection types.
If both aged partitions meet the sample floor, their posterior values are averaged;
otherwise the available partition is used. The feature is unavailable below the floor.

When accepted-use history and typed feedback history are both available,
`EngramCandidateAuthority` averages them into fusion `HISTORY`; otherwise it uses
the available input. Fusion still applies lifecycle, epoch, scope, metadata,
source, support, and ambiguity gates.

## Persistence and recovery

`feedback_state` is an additive versioned block in persistence v2; a missing block
loads as empty feedback state. Checkpoints include feedback and receipts. Recovery
compares deterministic before/after signatures after uncertain writes, and
replays report durability from the resulting store state.

## Negative-resolution contract

`NegativeResolutionKey` contains the exact typed identity and `ScopeKey`, constraint
fingerprint, available namespace knowledge epoch, normalization version,
resolver-plan fingerprint, capability-readiness fingerprint, and fusion-policy
fingerprint. Reason and expiry are record values. Key construction requires an
available epoch.

Admission is limited to `insufficient_knowledge` when:

- the unified result is MISS;
- every configured resolver is available and completed with its source's explicit knowledge-miss reason;
- resolver candidate, evidence, and accounting sets are empty;
- exact lookup completed with zero owners;
- resource, truncation, failure, collision, dependency, and indeterminate-state
  sets are empty; and
- trusted time, authoritative repository availability, and knowledge epoch are available.

Policy exclusions, collisions, unavailable dependencies, failures, and exhausted
execution bypass negative caching.

The v1 owner is memory-only, defaults to a fixed non-sliding five-minute TTL and 1,000 records, and supports deterministic oldest-created/key eviction. Lookup atomically expires records and invalidates related entries whose epoch, normalization, plan, readiness, or policy state changed. Response-state publication invalidates entries against the new namespace-epoch snapshot.

Because the namespace epoch tracks accepted-response mutations, v1 negative
admission and reuse apply only to exact-only resolver plans. Plans containing
pattern, lexical, structured-graph, or semantic sources run ordinary resolution
and invalidate a related exact negative record.

A hit returns a typed MISS with `negative_resolution_hit`,
`insufficient_knowledge`, zero resolver accounting, and measured consumption.
The transient result cache is checked first; budget exhaustion or owner failure
runs ordinary resolution.

## Inspection and operational boundary

`inspect_feedback_learning` returns bounded snapshots of policies, aggregates,
receipts, lifecycle results, negative-cache activity, evictions, and omissions.
The Python core exposes these features through `inspect_feedback_learning`.
