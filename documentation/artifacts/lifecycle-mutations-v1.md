# Audited lifecycle mutation contract v1

Status: Implemented by EGR-305  
Authority: ADR 0002 and Section 3 of `ENGRAM-DEVELOPMENT.md`

## Commands

`AcceptedResponseService.invalidate_response` and `retire_response` are the only non-supersession terminal commands. Each requires:

- statement ID and positive expected generation;
- a closed `LifecycleMutationReason` (`SOURCE_RETRACTED`, `POLICY`, `STALE`, `USER_REQUEST`, or `ADMINISTRATIVE`);
- a non-empty bounded caller identity;
- a durable request ID;
- an optional bounded audit detail; and
- an already-authorized caller at the Section 15 boundary.

Section 3 records and enforces the supplied decision; it does not decide truth or authorization. No generic lifecycle setter is exposed, and these commands cannot set `SUPERSEDED`.

## Transition and audit invariant

After receipt lookup, the command requires the exact current generation and applies the executable lifecycle matrix. Only `ACTIVE -> INVALIDATED` through invalidate and `ACTIVE -> RETIRED` through retire are legal. Terminal artifacts cannot transition again. A successful command creates the next artifact generation without changing response text, identity, retrieval representations, scope, tier, temporal/epoch inputs, provenance, support, or statistics.

The reserved artifact metadata field `lifecycle_audit` records operation, typed reason, caller ID, request ID, one injected canonical UTC time, and detail. The same time and audit values appear in the completed receipt result. Base commit rejects caller use of this reserved field.

Repository artifact, compatibility view, lifecycle-ineligible index projection, namespace epoch increment, and receipt are one coordinated candidate. The receipt records the exact before and after generation.

## Retry, concurrency, and restart

An exact retry replays the durable result without another checkpoint. Changed request-ID payload conflicts. Competing lifecycle writers with the same expected generation have one winner; the other receives a named generation conflict before storage. Checkpoint/restart restores the terminal generation, audit record, ineligible projection, epoch, and replay authority. Tier does not control truth, so STATIC and DYNAMIC artifacts follow the same lifecycle matrix.

## Evidence

`tests/test_responses.py` covers both operations, typed-boundary validation, exact response preservation, generation effects, audit persistence, projection exclusion, namespace epochs, replay and changed retry, terminal rejection, concurrent invalidation-versus-retirement, STATIC retirement, and atomic-file restart.
