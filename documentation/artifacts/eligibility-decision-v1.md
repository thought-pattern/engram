# Accepted-response eligibility decision contract v1

Status: Implemented by EGR-306  
Authority: ADR 0002, ADR 0004, and Section 3 of `ENGRAM-DEVELOPMENT.md`

## Pure evaluation

`evaluate_artifact_eligibility(artifact, context, epoch_policy)` is deterministic and side-effect free. It performs no clock read, epoch read, I/O, index access, mutation, graph lookup, or truth inference. The returned `EligibilityDecision` records the artifact statement ID and generation, lifecycle base result, final direct-answer result, one stable reason, and every captured context field plus epoch policy.

The decision's deterministic `context_signature()` is consumed by EGR-311 to detect projections evaluated under an earlier time, epoch, availability state, namespace, or policy.

## Ordered exclusion pipeline

The first applicable outcome wins in this fixed order:

1. artifact repository unavailable;
2. evaluation time unavailable;
3. namespace mismatch;
4. non-ACTIVE lifecycle;
5. malformed interval where available `valid_from >= valid_until`;
6. evaluation time before `valid_from`;
7. evaluation time at or after `valid_until`;
8. required artifact epoch unavailable;
9. context epoch unavailable for an epoch-bearing artifact;
10. knowledge epoch mismatch; or
11. eligible.

Availability exclusions are abstentions, not claims that the artifact is false. Lifecycle base eligibility remains separately visible even when a dependency exclusion has precedence.

## Temporal semantics

Both artifact bounds and the request time are canonical RFC 3339 UTC values validated by their owning contracts. Evaluation is half-open:

```text
valid_from <= evaluation_time < valid_until
```

An unavailable bound is open. Exact start is eligible; exact end is expired. When both bounds are available, equality and reversed ordering are invalid regardless of the current time.

## Epoch policies

`REQUIRE_MATCH` requires both artifact and context epochs to be available and equal. `MATCH_WHEN_ARTIFACT_AVAILABLE` treats an artifact without an epoch as unconstrained; when the artifact carries an epoch, the context must still carry the same epoch. Neither policy invents an unavailable epoch or compares epochs as dates.

The caller chooses policy at the trusted core boundary. Adapter exposure and authorization are Section 15 responsibilities.

## Stable reasons

The closed exclusion vocabulary is:

- `artifact_repository_unavailable`
- `evaluation_time_unavailable`
- `scope_namespace_mismatch`
- `lifecycle_superseded`
- `lifecycle_invalidated`
- `lifecycle_retired`
- `validity_interval_invalid`
- `not_yet_valid`
- `expired`
- `artifact_epoch_unavailable`
- `context_epoch_unavailable`
- `knowledge_epoch_mismatch`
- `eligible`
