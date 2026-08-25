# Accepted-response recovery runbook

Version: 1
Date: 2026-08-12
Scope: feature-level accepted-response artifacts, compatibility views, derived indexes, namespace epochs, and mutation receipts

This runbook covers accepted-response artifacts, receipts, persistence, and
repository publication. Use the deployment and rollback runbook for backup
orchestration, authorization, adapter operations, and rollback. Work from a copied
persistence file and retain request IDs with uncertain durable outcomes.

## Authority and first response

The persisted `response_state` artifacts, namespace epochs, and mutation receipts are authoritative. Compatibility statements and exact, alias, and support indexes are derived. On any recovery event:

1. stop new accepted-response mutations while allowing read-only inspection where safe;
2. preserve the original store and any same-directory temporary file as immutable evidence;
3. record the mutation `request_id`, operation, caller-visible error, store path,
   and time through content-free fields;
4. load a copy through `persistence.load_engram` or `EngramCore.open`;
5. require `response_repository.check().consistent` and a clean index check before
   returning the copy to service; and
6. retry with the original request ID and identical payload only after the outcome classification below permits it.

## Checkpoint and publication outcomes

| Outcome | Meaning | Required action |
| --- | --- | --- |
| Definite checkpoint failure | Durable and live state remain at the pre-mutation version. | Correct storage availability or permissions, then retry the identical mutation with the same request ID. |
| Indeterminate, durable state equals the candidate | Recovery publishes that exact authority. | Return or replay the recorded receipt. |
| Indeterminate, durable state equals the pre-mutation state | Live state remains unchanged. | Correct the checkpoint cause and retry the identical request ID. |
| Indeterminate, durable state differs from both states | Another writer, external edit, or storage fault produced divergent authority. | Keep mutations stopped, preserve all copies, and escalate for operator reconciliation. |
| Post-checkpoint publication fault with successful convergence | The candidate is durable; the coordinator restored repository, epoch, and receipt owners from it. | Report recovered success and retain the receipt as the retry authority. |
| Post-checkpoint publication and recovery failure | Durable authority may be ahead of some live owners. | Stop mutations immediately, restart from the preserved durable file, run all consistency checks, and require operator review before service. |

`PersistenceError.state_changed` and `MutationCoordinationError` fields distinguish these cases for core callers. A storage adapter that can fail after atomic replacement must classify that uncertainty as `CheckpointFailureError(INDETERMINATE, ...)`; reporting it as definite violates the coordinator contract.

## Startup, migration, and quarantine

- A malformed or unsupported v2 response contract, duplicate artifact ID, invalid
  epoch or receipt state, repository inconsistency, or pattern/artifact ID conflict
  blocks startup. Restore the last known-good copy or repair a new candidate through
  reviewed migration.
- Use `persistence.migrate_persistence_state` on a copy for a deterministic v1-to-v2 migration. Load the result again before promotion. Repeating migration on the same accepted input must produce the same response authority.
- Missing, malformed, or ambiguous legacy identity enters quarantine with direct
  answer eligibility disabled. An operator constructs and validates the corrected
  candidate store.
- Startup rebuilds derived response indexes from authoritative artifact projections.

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

For artifact-backed response state, startup reconstructs the repository index from
authoritative artifacts. A repository equivalence failure requires repository
recovery.

## Capacity, time, epoch, and retry diagnostics

- `REJECTED_CAPACITY` is a completed, replayable receipt that preserves repository
  authority and namespace epochs. A new admission attempt uses reviewed capacity
  configuration and a new request ID.
- DYNAMIC eviction removes physical residency. Lifecycle transitions use their named
  operations; STATIC artifacts and protected lineage remain resident.
- Expired and epoch-stale artifacts remain authoritative history but are request-ineligible. Verify the injected UTC evaluation time, epoch availability, namespace, and epoch value before considering data repair.
- A missing or unavailable epoch follows the documented availability policy. Restore
  the dependency when that policy requires an epoch.
- Exact retries use the original completed receipt and preserve checkpoint count.
  Reusing a request ID with a changed operation or payload is a stable conflict. A
  pruned receipt tombstone protects the bounded retention horizon.

## Return-to-service checklist

- The preserved recovery copy loads through the supported codec.
- Repository, compatibility view, and derived index checks are consistent.
- Namespace epoch and mutation receipt snapshots validate and retain monotonic sequences.
- Terminal, expired, and epoch-stale artifacts have direct-answer eligibility disabled.
- A representative completed request replays with the original result and unchanged write count.
- The incident classification, selected durable copy, checks, and approval use content-free records.

Executable fault cases are covered by `tests/test_coordination.py`,
`tests/test_persistence_v2.py`, and `tests/test_responses.py`. The subsystem contract
is [accepted-response contracts v1](contracts-v1.md); operator migration rules are in
[persistence schema management](../operations/persistence-schema-management-v1.md).
