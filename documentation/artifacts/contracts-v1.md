# Accepted-response contracts v1

**Status:** Current implemented contract

This document defines the accepted-response subsystem: authoritative artifacts,
admission, lifecycle, request-time eligibility, mutation receipts, repository
publication, and compatibility behavior.

## Authoritative record

`CachedResponseArtifact` is the persisted authority for an accepted response. It
contains the exact response text, query identity, retrieval representations, exact
scope, residency tier, lifecycle state, temporal and namespace-epoch inputs,
support Claim IDs, lineage, provenance, statistics, metadata, schema version, and
generation.

The response text is stored and returned unchanged. Canonical requests and aliases
are retrieval data; executable AIML patterns are separate. Every field has one
concrete runtime type and a deterministic codec. Persistence loaders translate the
documented legacy omissions; current calls use concrete absence values.

## Admission and identity

Base commit validates the complete artifact before allocating a statement ID. A
canonical or alias key can have only one owner in an exact scope, including an
inactive historical owner. A collision preserves the existing owner and reports
the conflict. Replacement uses explicit supersession with the expected
owner statement ID and generation.

`STATIC` and `DYNAMIC` control residency only. STATIC artifacts are protected from
ordinary capacity eviction. DYNAMIC admission uses the configured deterministic
victim policy and reports admitted, evicted, and affected namespace identifiers.
Capacity eviction removes residency and preserves lifecycle state.

## Lifecycle and eligibility

Lifecycle values are `ACTIVE`, `SUPERSEDED`, `INVALIDATED`, and `RETIRED`. Only an
ACTIVE artifact can answer. Invalidation, retirement, and supersession are named
operations with optimistic generation checks.
Historical artifacts remain available for audit and lineage.

Each request captures one timezone-aware UTC evaluation time and one namespace-epoch
snapshot. Eligibility evaluates, in order:

1. lifecycle;
2. valid-time bounds;
3. namespace match and epoch availability;
4. epoch equality or the configured older-artifact policy; and
5. repository availability.

The decision records a stable exclusion reason and a context signature. Exact lookup
revalidates every current key owner against that request context and atomically
refreshes the derived eligibility projection. The captured time and epoch determine
request authority.

## Repository and mutation publication

`ArtifactRepository` owns one complete live state containing artifacts,
compatibility statement views, index projections, and a checked disposable index.
Candidate states are built off-live and checked before publication. The lock order is
mutation coordinator, repository, then private index owner.

`AtomicMutationCoordinator` publishes repository state, namespace epochs, and the
mutation receipt ledger as one candidate. With persistence configured, the complete
candidate is checkpointed before live publication. An indeterminate write is
resolved by comparing the durable state with the before and candidate signatures;
the matching signature determines the outcome.

Every mutation has a bounded `request_id`, closed operation, and canonical payload
signature. An identical completed request replays its recorded result and preserves
the mutation/checkpoint counts. Reusing the request ID with a different operation
or payload conflicts. Bounded receipt tombstones prevent an expired ID from being
silently reapplied during the retention horizon.

## Persistence and compatibility

Persistence v2 stores authoritative artifacts, namespace epochs, mutation receipts,
quarantine records, feedback state, and the cross-feature version manifest. Startup
rebuilds response indexes and compatibility projections from that state.

Persistence v1 remains readable through explicit migration. Ambiguous legacy
identity enters quarantine with answer eligibility disabled. Rollback uses a
compatible backup. Migration and recovery procedures are documented
in [persistence schema management](../operations/persistence-schema-management-v1.md)
and the [accepted-response recovery runbook](accepted-response-recovery.md).

`LearnResponse` remains the compatibility entry point used by MCP and gRPC. It
constructs the authoritative artifact, rejects normalized `IDK`, preserves existing
identity, and delegates admission, receipts, checkpointing, and publication to the
coordinator. Explicit supersession handles replacement; compatibility statement
views mirror repository authority.

## Verification

The executable contract is covered by `tests/test_responses.py`,
`tests/test_coordination.py`, `tests/test_persistence_v2.py`,
`tests/test_persistence_management.py`, and the adapter tests. Coverage includes
identity collisions, capacity, lifecycle, eligibility refresh, concurrency,
idempotent replay, checkpoint uncertainty, migration, quarantine, restart, and
derived-index consistency.
