# ADR 0004: Evaluation time, knowledge epoch, and release qualification

- Status: Accepted and qualified
- Date: 2026-08-11
- Applies from: All resolution paths

## Context

Validity, freshness, negative resolution, and concurrent graph/cache changes use one time and knowledge-state reference per request. Release criteria are fixed before feature tuning.

## Decision

The core captures one timezone-aware UTC `evaluation_time` at request entry and
passes it unchanged to every resolver and eligibility check. Tests inject the
clock. Standalone adapters use the core clock; trusted Tapestry integration may
supply the time. Wire output uses RFC 3339 and `evaluation_time_available`.

`knowledge_epoch` is a non-negative integer associated with a namespace. Standalone Engram owns and increments it for accepted artifact eligibility changes and activated graph snapshots. Tapestry may provide its authoritative namespace epoch through the trusted integration contract. Each resolution records the evaluated epoch; negative-resolution records require the same epoch, and a concurrent epoch change makes the candidate stale before commit. Epoch `0` with `knowledge_epoch_available=false` means unavailable.

Numerical criteria are governed by the evaluation foundation manifest. Its status is `approved_project_qualification`; the project owner approved the values before release-gate execution. Tuning, release-gate, and final-test data are project-owned, versioned, and disjoint by request and scoped contrast identity. Only tuning data may select parameters, and the final test executes after a passing release gate. A direct-answer correctness regression blocks component promotion even when recall improves.

## Consequences

- All resolvers evaluate temporal eligibility consistently within one request.
- A relevant knowledge change invalidates negative results.
- Engineering benchmarks detect regressions; project qualification owns release approval.
- The project qualification contract defines evaluation partitions, numerical approval, ordered qualification, and release authority.
