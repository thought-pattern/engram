# Accepted-response lifecycle contract v1

Status: Implemented by EGR-304  
Authority: ADR 0002 and Section 3 of `ENGRAM-DEVELOPMENT.md`

## Domain separation

`LifecycleState` records the authoritative validity disposition of an accepted-response artifact. `Tier` controls capacity admission and eviction. Residency records whether an artifact is currently stored. Neither tier nor residency can infer or mutate lifecycle.

Capacity eviction physically removes a resident DYNAMIC artifact through the later repository mutation boundary. It does not change ACTIVE, SUPERSEDED, INVALIDATED, or RETIRED before removal. STATIC prevents ordinary capacity eviction only; it does not make an artifact permanently true or directly answerable.

## Base eligibility

| Lifecycle | Lifecycle base eligibility | Stable reason |
| --- | --- | --- |
| ACTIVE | eligible | `eligible` |
| SUPERSEDED | ineligible | `lifecycle_superseded` |
| INVALIDATED | ineligible | `lifecycle_invalidated` |
| RETIRED | ineligible | `lifecycle_retired` |

This is only the lifecycle stage. EGR-306 adds temporal, epoch, and dependency checks before an artifact may produce a direct response.

## Legal transition matrix

| Current | `SUPERSEDE` | `INVALIDATE` | `RETIRE` |
| --- | --- | --- | --- |
| ACTIVE | SUPERSEDED | INVALIDATED | RETIRED |
| SUPERSEDED | forbidden | forbidden | forbidden |
| INVALIDATED | forbidden | forbidden | forbidden |
| RETIRED | forbidden | forbidden | forbidden |

The three non-ACTIVE lifecycle states are terminal in version 1. Repeating the same operation is handled by the durable request receipt planned by EGR-313 and is not represented as a second lifecycle transition. No generic lifecycle setter exists. Only the named supersession operation may set ACTIVE to SUPERSEDED.

## Historical retrieval-key reuse

Base commit may not reuse a canonical or alias key already owned by any artifact in the same exact scope. Reuse requires the explicit replacement operation plus the expected owner statement ID and positive generation. The repository work in EGR-312 must also prove that the named owner is the applicable owner and that the replacement would leave at most one ACTIVE owner.

When the named owner is ACTIVE, replacement changes it to SUPERSEDED and records lineage atomically. If a later replacement policy permits a terminal historical owner to be named, its lifecycle remains terminal; the operation must preserve the old artifact and record explicit lineage rather than silently reactivating or overwriting it. EGR-307 owns that repository-level composition.

## Stable implementation surface

The transport-neutral contract is implemented in `engram/artifacts.py`:

- `LifecycleState` and `LifecycleOperation` define the closed vocabularies.
- `LEGAL_LIFECYCLE_TRANSITIONS` and `TERMINAL_LIFECYCLE_STATES` publish the matrix.
- `lifecycle_base_eligibility` is the tier-independent base check.
- `lifecycle_transition_decision` and `require_lifecycle_transition` apply named operations.
- `historical_key_reuse_decision` rejects blind key reuse.
- `lifecycle_after_capacity_eviction` makes the no-transition eviction rule executable.
