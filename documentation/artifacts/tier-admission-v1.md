# STATIC and DYNAMIC tier admission contract v1

Status: Implemented by EGR-303  
Authority: Section 3 of `ENGRAM-DEVELOPMENT.md`

## Capacity domain

`TierAdmissionPolicy.dynamic_capacity` bounds resident DYNAMIC accepted-response artifacts only. STATIC artifacts do not consume that capacity and ordinary capacity eviction cannot select them. Neither tier establishes lifecycle, truth, temporal validity, epoch validity, or direct-answer eligibility.

Admission is planned off-live by `ArtifactRepository.plan_admission`. The immutable `AdmissionPlan` contains the outcome, complete repository candidate, admitted and evicted statement IDs, affected epoch namespaces, and separate residency/lifecycle flags. The later EGR-314 coordinator owns epoch increment, checkpoint, and publication.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `ADMITTED` | The artifact fits, or is STATIC; no existing artifact is evicted. |
| `ADMITTED_WITH_EVICTION` | Enough unprotected DYNAMIC victims are removed from the candidate to satisfy capacity. |
| `REJECTED_CAPACITY` | Too few unprotected victims exist; the candidate is the unchanged live snapshot and no state change is reported. |

The bounded artifact path does not inherit the legacy statement store's “admit over capacity when protected” behavior. It never silently drops the incoming artifact and never exceeds the configured DYNAMIC capacity after a successful admission. A migrated over-capacity state removes as many eligible victims as necessary before admitting one new DYNAMIC artifact.

## Protection and ordering

An artifact is protected by `minimum_protected_hit_rate` only after at least one query and when its exact hit/query ratio reaches the threshold. An untouched artifact's default 0.5 selection value does not protect it.

Victims are selected deterministically:

- FIFO: oldest canonical acceptance time;
- LRU: oldest last hit, or acceptance time when never hit, with never-hit losing timestamp ties;
- LFU: lowest hit count, then oldest acceptance time; or
- HIT_RATE: lowest exact hit/query ratio, then oldest acceptance time.

Statement ID breaks every remaining tie. Only DYNAMIC artifacts participate.

## State and epoch effects

Every selected victim is physically removed through the repository candidate boundary, so the artifact, detached compatibility statement, retrieval ownership, direct view, and support mappings remain synchronized. Victim lifecycle is inspected and left unchanged; eviction is residency loss, not invalidation or retirement.

Successful admission reports the sorted set of namespaces affected by incoming and evicted artifacts. Accepted-artifact availability changed in those namespaces, so EGR-314 must coordinate their standalone epoch changes with persistence and publication. Rejected admission reports no affected namespace and no epoch or lifecycle change.
