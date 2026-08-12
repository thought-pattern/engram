# Authoritative live artifact repository contract v1

Status: Implemented by EGR-312  
Authority: ADR 0001, ADR 0003, and Section 3 of `ENGRAM-DEVELOPMENT.md`

## One live authority

`ArtifactRepository` owns one immutable `RepositoryState` containing:

- authoritative `CachedResponseArtifact` records keyed by statement ID;
- complete legacy statement compatibility views derived from those records; and
- one checked Section 2 `IndexState` derived from the same records.

Artifacts are frozen domain records. Compatibility statements are deeply frozen inside the live state and returned to callers as detached mutable copies. A caller cannot rewrite response text, provenance, metadata, or statistics through a legacy view. The repository's artifact lookup is the authoritative statement-ID lookup.

## Compatibility projection

`compatibility_statement_from_artifact` emits the current statement shape, including exact text and tier; canonical acceptance time; identity lexical terms; patternless matcher fields; source and caller provenance; authoritative hit, query, and last-hit statistics; support references; retrieval representations; scope; metadata; and a compact response-artifact lifecycle record.

This shape exists only for older Python and internal consumers. It is rebuilt after every mutation and restart. No field in it may override the artifact.

## Conservative index construction

Off-live `build_repository_state` emits complete identity and support projections, but initially marks direct-answer eligibility false with `eligibility_context_required` for ACTIVE artifacts. This is intentionally conservative: only EGR-311 contextual lookup may make an exact key directly answerable under a captured request context. Non-ACTIVE artifacts receive their lifecycle exclusion reason.

Every candidate passes `check_repository_state`, which verifies equal artifact/view/projection ID sets, exact compatibility projection, artifact/projection identity and generation, retrieval bindings, support IDs, and the complete Section 2 index consistency report.

## Atomic ownership and lock order

The lock order is always:

1. repository re-entrant lock;
2. private Section 2 `IndexOwner` lock.

The index owner is not exposed, preventing lock-order inversion. `atomic_replace` requires the exact current repository state generation and a candidate exactly one generation later. It checks the candidate, atomically swaps its checked index under the repository lock, then publishes the completed repository state. Readers using repository APIs cannot observe a partial artifact, view, or index mutation.

These are live-state primitives only. EGR-314 later places durable checkpoint-before-publication semantics around candidate publication.

## Deletion and capacity eviction

Physical removal is separate from lifecycle. `candidate_without_artifact` requires statement ID, expected artifact generation, and a typed reason. `CAPACITY_EVICTION` accepts DYNAMIC only and confirms that eviction leaves lifecycle unchanged. `EXPLICIT_DELETE` is the low-level administrative removal primitive. Either removal rebuilds views and indexes in the candidate, and publication removes all three together.

EGR-303 owns bounded tier admission and eviction-victim selection. EGR-305 owns authoritative invalidation and retirement, which retain the artifact and change its lifecycle rather than deleting it.

## Contextual exact lookup

Repository exact lookup holds the repository lock while EGR-311 revalidates all key owners and refreshes the private index. When eligibility fields change, the repository publishes a new state generation pointing to that post-refresh index. The artifact and compatibility views remain unchanged; equivalence checks deliberately validate identity and support while accepting context-dependent eligibility state.
