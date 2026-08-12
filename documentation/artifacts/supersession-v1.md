# Explicit supersession contract v1

Status: Implemented by EGR-307  
Authority: ADR 0002 and Section 3 of `ENGRAM-DEVELOPMENT.md`

## Required command identity

`AcceptedResponseService.supersede_response` requires the expected current statement ID and positive generation, a fully validated new generation-1 ACTIVE artifact with a different statement ID, typed reason, caller ID, durable request ID, and optional audit detail. It is the only operation that can assign `SUPERSEDED` or populate `superseded_by`.

The replacement can reuse canonical or alias keys owned by the expected current artifact. Any key owner with another statement ID blocks the command and every conflicting owner is named. The new statement ID must not already exist. This is the explicit exception to base commit's historical-key-reuse prohibition.

## Atomic lineage change

A successful command creates one repository candidate that:

- advances the current artifact exactly one generation;
- changes its lifecycle from ACTIVE to SUPERSEDED and links `superseded_by` to the replacement;
- preserves its exact response and all other authority;
- writes the supersession audit including replacement ID;
- creates the replacement artifact at generation 1;
- applies any unrelated DYNAMIC eviction selected by tier admission;
- rebuilds compatibility views and every retrieval/support index; and
- increments each affected namespace once and records every changed/created/evicted generation in one completed receipt.

Old and new owners can coexist in retrieval history, but only the contextually eligible ACTIVE replacement may become a direct result.

## Capacity and concurrency

Historical lineage is never silently destroyed to make space. If bounded DYNAMIC admission would evict the artifact being superseded, or no safe capacity exists, the operation records a replayable `REJECTED_CAPACITY` receipt and leaves repository and epochs unchanged. A STATIC lineage can admit a DYNAMIC replacement while evicting an unrelated DYNAMIC victim.

The expected generation is checked beneath the coordinator lock. Competing supersessions have one winner; the other receives a stable generation conflict before checkpoint. Exact retry replays without a checkpoint, while changed request content conflicts. Restart restores both generations, their link, audit data, indexes, epochs, and receipt.

## Evidence

`tests/test_responses.py` covers expected-owner key reuse, other-owner collision, distinct replacement ID, exact lineage/effects, exact retry, protected history at capacity, unrelated eviction, namespace effects, competing writers, exact-text preservation, and real checkpoint/restart.
