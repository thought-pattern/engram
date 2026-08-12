# Atomic mutation coordinator contract v1

Status: Implemented by EGR-314  
Authority: ADR 0002 and Section 3 of `ENGRAM-DEVELOPMENT.md`

## Owned state and visibility

`AtomicMutationCoordinator` is the mutation and read-snapshot boundary for the accepted-response repository, its compatibility views and Section 2 indexes, namespace epochs, and the mutation receipt ledger. These owners are supplied at construction so persistence and legacy integration can retain object identity, but callers must not mutate or read their individual state during a coordinated mutation. A coordinator snapshot is the only cross-owner live-state view.

The lock order is coordinator, repository, private Section 2 index owner. Candidate construction and execution hold the coordinator and repository re-entrant locks. Repository readers therefore see either the old complete repository or the new complete repository; coordinator readers see either the complete `before` or complete `after` state. No callback may acquire these locks in reverse order.

## Candidate invariant

`build_candidate` snapshots all live response state and constructs an immutable `CoordinatedMutationCandidate` without changing live state. It rejects the candidate unless:

- a changed repository advances exactly one state generation, while an unchanged capacity-rejection candidate retains its generation;
- repository artifact, compatibility-view, and index equivalence checks pass;
- the receipt's ordered artifact generations exactly equal the repository artifact diff;
- affected epoch namespaces exactly equal the namespaces whose artifact residency or lifecycle changed;
- every affected namespace epoch is initialized if absent and incremented exactly once; and
- the receipt can be recorded at the ledger's exact next sequence.

The candidate's live signature covers authoritative artifact content, repository generation, namespace epochs, and the complete receipt/tombstone ledger. Its durable signature excludes transient repository and derived-index generations because neither is persisted and both are rebuilt from the same authoritative artifacts. Stale live execution uses the live signature; indeterminate storage recovery uses the durable signature.

## Single-checkpoint state machine

Execution rejects a stale `before` signature before storage is touched. In configured durable mode it then invokes the checkpoint callback exactly once with the complete `after` state. Only after the checkpoint returns successfully does live publication begin. In-memory mode follows the same candidate and publication state machine, skips the checkpoint, and reports no durability.

Checkpoint callbacks must classify known storage failures:

| Outcome | Coordinator behavior |
| --- | --- |
| Success | Treat the candidate as durable and publish it. |
| `DEFINITE` failure | Keep live state unchanged; report no durable candidate and no recovery requirement. |
| `INDETERMINATE` failure, durable state equals `before` | Keep live state unchanged and report a safe failure. |
| `INDETERMINATE` failure, durable state equals `after` | Publish the recovered durable candidate without another checkpoint. |
| `INDETERMINATE` failure, invalid/divergent/unavailable durable state | Keep the old coordinated live snapshot and require operator recovery. |

An unclassified checkpoint exception is treated as a definite failure. A storage adapter that can fail after replacing durable state must translate that uncertainty to `CheckpointFailureError(INDETERMINATE, ...)`; otherwise it violates this contract.

## Publication and recovery

Publication replaces the repository, runs the integration publication hook, then restores epoch and ledger owners from the same validated candidate while all coordinator readers remain blocked. A post-checkpoint publication fault triggers convergence from the already-durable candidate without a second checkpoint. Successful convergence reports `recovered=true`.

If convergence itself fails, `MutationCoordinationError` reports the checkpoint count, whether the candidate is durable, whether repository live state changed, and that recovery is required. Callers must not report mutation success in that case. Restart recovery loads the durable candidate, rebuilds repository views and indexes, and restores epochs and receipts before accepting new mutations.

## Retry and concurrency

Receipt lookup and sequence allocation occur through the coordinator lock. Base mutation operations must resolve `REPLAY`, `IN_PROGRESS`, `EXPIRED`, and `CONFLICT` before building a candidate. Exact completed replay therefore performs no mutation and no checkpoint. Concurrent candidates derived from the same `before` state have one winner; later execution observes a stale signature and conflicts before another checkpoint.

## Fault-injection evidence

`tests/test_coordination.py` covers off-live construction, in-memory operation, checkpoint-before-publication ordering, one checkpoint attempt, definite failure, each indeterminate recovery branch, post-checkpoint publication recovery, recovery failure reporting, unchanged capacity-rejection candidates, receipt/namespace equivalence rejection, object-identity-preserving owner restore, and concurrent single-winner publication.
