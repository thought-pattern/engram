# ADR 0004: Evaluation time, knowledge epoch, and provisional gate process

**Gate-authority update (2026-08-24):** The evaluation-time and knowledge-epoch decisions remain accepted. The original Section 0 numerical table was an engineering proposal and is superseded by the approved project-owned Section 16 gates. Benchmarks report p50, p95, p99, and maximum turn lengths without making timing a pass/fail condition.

- Status: Evaluation time and knowledge epoch accepted; numerical table superseded
- Date: 2026-08-11
- Applies from: All increments

## Context

Validity, freshness, negative resolution, and concurrent graph/cache changes need one time and knowledge-state reference per request. The development plan also requires numerical gates before feature tuning. Section 0 measured the current offline implementation on the audited machine at corpus sizes 100, 1,000, and 5,000.

## Decision

The core captures one timezone-aware UTC `evaluation_time` at request entry and passes it unchanged to every resolver and eligibility check. Tests inject the clock. Standalone adapters use the core clock. A Tapestry-supplied time is accepted only through an explicitly trusted integration field; ordinary client metadata cannot set it. Wire output uses an RFC 3339 string and an explicit `evaluation_time_available` boolean rather than null.

`knowledge_epoch` is a non-negative integer associated with a namespace. Standalone Engram owns and increments it for accepted artifact eligibility changes and activated graph snapshots. Tapestry may provide its authoritative namespace epoch through the trusted integration contract. Each resolution records the evaluated epoch; negative-resolution records require the same epoch, and a concurrent epoch change makes the candidate stale before commit. Epoch `0` with `knowledge_epoch_available=false` means unavailable.

Numerical criteria are governed only by the Section 16 foundation manifest. Its status is `approved_project_qualification`; the project owner approved the exact values before release-gate execution. Tuning, release-gate, and final-test data are project-owned, versioned, and disjoint by request and scoped contrast identity. Only tuning data may select parameters, and the final test executes only after a passing release gate. Timing measurements are descriptive and carry no pass/fail threshold. A direct-answer correctness regression still blocks component promotion even when recall improves.

## Consequences

- All resolvers evaluate temporal eligibility consistently within one request.
- Negative results cannot survive a relevant knowledge change unnoticed.
- Engineering benchmarks may detect regressions without being represented as release approval.
- Section 16 owns the project evaluation partitions, numerical approval, ordered qualification, and release authority.
