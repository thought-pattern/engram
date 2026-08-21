# ADR 0004: Evaluation time, knowledge epoch, and first release gates

**Timing-policy update (2026-08-20):** Numerical latency gates in this decision are superseded. Benchmarks report p50, p95, p99, and maximum turn lengths without making timing a pass/fail condition.

- Status: Accepted
- Date: 2026-08-11
- Applies from: All increments

## Context

Validity, freshness, negative resolution, and concurrent graph/cache changes need one time and knowledge-state reference per request. The development plan also requires numerical gates before feature tuning. Section 0 measured the current offline implementation on the audited machine at corpus sizes 100, 1,000, and 5,000.

## Decision

The core captures one timezone-aware UTC `evaluation_time` at request entry and passes it unchanged to every resolver and eligibility check. Tests inject the clock. Standalone adapters use the core clock. A Tapestry-supplied time is accepted only through an explicitly trusted integration field; ordinary client metadata cannot set it. Wire output uses an RFC 3339 string and an explicit `evaluation_time_available` boolean rather than null.

`knowledge_epoch` is a non-negative integer associated with a namespace. Standalone Engram owns and increments it for accepted artifact eligibility changes and activated graph snapshots. Tapestry may provide its authoritative namespace epoch through the trusted integration contract. Each resolution records the evaluated epoch; negative-resolution records require the same epoch, and a concurrent epoch change makes the candidate stale before commit. Epoch `0` with `knowledge_epoch_available=false` means unavailable.

The first release gates are measured with the Section 0 harness and its declared machine profile:

| Measure | Gate |
| --- | --- |
| Critical identity adversarial cases | 0 false direct answers and 100% correct abstention/selection |
| Held-out false-direct-answer rate | At most 0.5%, and never worse than the previous released policy |
| Useful EVIDENCE rate | At least 60% of emitted packages judged useful on the held-out evidence set |
| Scoped exact lookup | p95 at most 5 ms at 100,000 artifacts; latency slope from 10,000 to 100,000 at most 1.5x |
| Lexical proposal | At 5,000 artifacts, p95 at most 5 ms and no more than 25% above the Section 0 p95 |
| Support-aware proposal | At 5,000 artifacts and fan-out 1/10/100, p95 at most 30 ms and no more than 25% above the matching Section 0 p95 |
| Cold eager startup | p95 at most 35 seconds and no more than 20% above the Section 0 p95 |
| Index rebuild | p95 at most 1 second at the approved maximum corpus before persisted snapshots are reconsidered |
| Persistence at 5,000 artifacts | Atomic save p95 at most 600 ms; load p95 at most 350 ms; neither more than 25% above baseline |
| Peak measured build memory at 5,000 artifacts | At most 11 MiB and no more than 25% above the matching Section 0 path |

Timing measurements use at least 30 samples after warm-up and report p50 and p95 without a timing verdict. Accuracy tuning, release-gate evaluation, and final testing use disjoint datasets. A direct-answer correctness regression blocks promotion even if recall improves.

## Consequences

- All resolvers evaluate temporal eligibility consistently within one request.
- Negative results cannot survive a relevant knowledge change unnoticed.
- The gates are strict enough to detect regressions yet allow the measured eager startup and current scan baseline while indexes are built.
- New machine classes require a recorded baseline and explicit gate translation; they do not silently replace this artifact.
