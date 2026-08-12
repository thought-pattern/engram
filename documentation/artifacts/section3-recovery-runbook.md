# Section 3 accepted-response recovery runbook

Version: 1  
Date: 2026-08-12  
Scope: feature-level accepted-response artifacts, compatibility views, derived indexes, namespace epochs, and mutation receipts

This runbook covers the Section 3 authority boundary. Cross-feature backup orchestration, authorization, adapter operations, and downgrade/rollback remain Section 15 work. Never edit a live persistence file in place and never discard a request ID whose durable outcome is uncertain.

## Authority and first response

The persisted `response_state` artifacts, namespace epochs, and mutation receipts are authoritative. Compatibility statements and exact, alias, and support indexes are derived. On any recovery event:

1. stop new accepted-response mutations while allowing read-only inspection where safe;
2. preserve the original store and any same-directory temporary file as immutable evidence;
3. record the mutation `request_id`, operation, caller-visible error, store path, and time without copying raw request or response content into logs;
4. load a copy through `persistence.load_engram` or `EngramCore.open`; do not parse and reinterpret response authority manually;
5. require `response_repository.check().consistent` and an index check with no unexplained issue before returning the copy to service; and
6. retry with the original request ID and identical payload only after the outcome classification below permits it.

## Checkpoint and publication outcomes

| Outcome | Meaning | Required action |
| --- | --- | --- |
| Definite checkpoint failure | The candidate was not durably installed and live authority was not published. | Correct storage availability or permissions, then retry the identical mutation with the same request ID. |
| Indeterminate, durable state equals the candidate | Recovery has proved the candidate durable and publishes that exact authority without a second checkpoint. | Return or replay the recorded receipt. Do not issue a new request ID. |
| Indeterminate, durable state equals the pre-mutation state | Recovery has proved that the candidate was not installed. | Treat the attempt as failed without live change; after correcting the checkpoint cause, retry the identical request ID. |
| Indeterminate, durable state differs from both states | Another writer, external edit, or storage fault produced divergent authority. | Keep mutations stopped, preserve all copies, and escalate for operator reconciliation. Do not retry or synthesize a receipt. |
| Post-checkpoint publication fault with successful convergence | The candidate is durable; the coordinator restored repository, epoch, and receipt owners from it. | Report recovered success and retain the receipt as the retry authority. |
| Post-checkpoint publication and recovery failure | Durable authority may be ahead of some live owners. | Stop mutations immediately, restart from the preserved durable file, run all consistency checks, and require operator review before service. |

`PersistenceError.state_changed` and `MutationCoordinationError` fields distinguish these cases for core callers. A storage adapter that can fail after atomic replacement must classify that uncertainty as `CheckpointFailureError(INDETERMINATE, ...)`; reporting it as definite violates the coordinator contract.

## Startup, migration, and quarantine

- A malformed or unsupported v2 response contract, duplicate artifact ID, invalid epoch or receipt state, repository inconsistency, or pattern/artifact ID conflict must block startup. Restore the last known-good copy or repair into a new file through a reviewed migration; do not delete fields until loading succeeds.
- Use `persistence.migrate_persistence_state` on a copy for a deterministic v1-to-v2 migration. Load the result again before promotion. Repeating migration on the same accepted input must produce the same response authority.
- Missing, malformed, or ambiguous legacy identity is quarantined rather than guessed. Quarantined artifacts remain inspectable but cannot supply a direct exact result. Preserve the quarantine record and repair identity only through an explicit future operator workflow.
- Derived response indexes are intentionally absent from `response_state`. Startup rebuilds them. A derived-index discrepancy is repaired from authoritative artifact projections, never by changing accepted response text or receipts.

## Consistency and derived-index recovery

For a safely loaded copy, require all of the following before promotion:

```python
from engram import persistence

engram = persistence.load_engram("recovery-copy.json")
assert engram.response_repository.check().consistent
assert engram.check_indexes().consistent
```

If only the generic derived index is inconsistent, preview and then apply a rebuild from current authoritative views:

```python
preview = engram.rebuild_indexes(apply=False)
# Review preview before mutation.
applied = engram.rebuild_indexes(apply=True)
assert engram.check_indexes().consistent
assert engram.response_repository.check().consistent
```

For artifact-backed response state, normal startup already reconstructs the repository's index from authoritative artifacts. A repository equivalence failure is not an index-only incident and must not be hidden by a generic rebuild.

## Capacity, time, epoch, and retry diagnostics

- `REJECTED_CAPACITY` is a completed, replayable receipt. It does not change repository authority or namespace epochs. Increase or change capacity policy only through reviewed configuration, then use a new request ID for a deliberately new admission attempt.
- DYNAMIC eviction is physical residency loss, not retirement or invalidation. STATIC artifacts and protected lineage are not ordinary eviction victims.
- Expired and epoch-stale artifacts remain authoritative history but are request-ineligible. Verify the injected UTC evaluation time, epoch availability, namespace, and epoch value before considering data repair.
- A missing or unavailable epoch never authorizes a guessed match. Restore the dependency or use the explicitly documented availability policy.
- Exact retries use the original completed receipt and perform no second checkpoint. Reusing a request ID with a changed operation or payload is a stable conflict. A pruned receipt tombstone prevents unsafe reapplication until its bounded horizon expires; do not bypass it with manual ledger edits.

## Return-to-service checklist

- The preserved recovery copy loads through the supported codec.
- Repository, compatibility view, and derived index checks are consistent.
- Namespace epoch and mutation receipt snapshots validate and retain monotonic sequences.
- Terminal, expired, and epoch-stale artifacts cannot produce a direct exact result.
- A representative completed request replays with the original result and no second write.
- The incident classification, selected durable copy, checks, and approval are recorded without raw accepted-response content.

Executable fault cases are covered by `tests/test_coordination.py`, `tests/test_persistence_v2.py`, and `tests/test_responses.py`. The normative contracts are `mutation-coordinator-v1.md`, `persistence-v2.md`, and `mutation-receipts-v1.md` in this directory.
