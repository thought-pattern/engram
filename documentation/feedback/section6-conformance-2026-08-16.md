# Section 6 feedback and negative-resolution conformance — 16 August 2026

## Result and scope

All fifteen Section 6 tasks meet their exit conditions after the findings review. External Regulator observations are strictly targeted, durable, idempotent, conflict-detecting, bounded, aged, and isolated by statement lineage, exact relationship, contract, and fusion-policy partitions. Stale and policy outcomes cannot bypass Section 3 lifecycle authority or Section 5 hard eligibility. Because the current namespace epoch versions accepted-response knowledge only, a completed knowledge miss is reusable solely for an exact-only resolver plan through an exact, memory-only negative key with available epoch and fully ready/completed resolver state; the optimization fails open.

The hand-authored `feedback-history-v1.0.0` formula, five-sample floor, priors, daily buckets, 30-day half-life, capacities, and negative TTL were specified before the conformance and benchmark results. No coefficient, prior, sample floor, TTL, threshold, or admission reason was learned from unit tests, synthetic fixtures, or benchmark samples. Project-partition calibration and release-quality evaluation are Section 16 work.

## Task evidence

| Task | Implemented evidence |
| --- | --- |
| EGR-601 | `FeedbackObservation`, statement and relationship keys, closed kinds/outcomes, policy and contract fingerprints, strict codecs, bounded constraints/reasons, concrete generation availability, and injected UTC time are implemented in `engram.feedback`. |
| EGR-602 | `FeedbackStore` owns durable `RECORD_FEEDBACK` receipts; exact retries replay, business-input changes conflict, server execution metadata does not destabilize retries, and both unified resolution and regulated proposals consume the owner. A bounded streaming signature supports the declared 1,000-observation batch without the generic receipt item ceiling. |
| EGR-603 | Per-statement-generation records retain candidacy, acceptance, and every rejection counter without merging text, owners, replacement IDs, unavailable generations, contracts, or policy partitions. |
| EGR-604 | Relationship records add exact `QueryIdentity`, `ScopeKey`, bounded constraint fingerprint, statement generation, contract, and policy. Cardinality and inspection output are bounded. |
| EGR-605 | Additive `feedback_state` v1 persistence has deterministic strict codecs, restart-safe receipts, unsupported-version rejection, optional migration from older files to empty typed state, checkpoint-before-publication, and before/after recovery signatures. A definite before-state stays healthy and retryable, an after-state publishes, divergence degrades, and an uncheckpointed replay never claims durability. Legacy hit/query counts remain separate. |
| EGR-606 | Accepted feedback strengthens only the targeted statement and relationship; quality rejection contributes only to statement reliability plus its targeted relationship. Raw artifact fields are unchanged. |
| EGR-607 | Context rejection contributes only to the exact relationship. Policy rejection suppresses only statement, namespace, and fusion-policy partition; acceptance of that exact partition clears it. |
| EGR-608 | Stale feedback immediately excludes the matching artifact lineage or legacy candidate and requests Section 3 expected-generation invalidation. Pending, completed, conflicted, and failed results are retained and replayed without reattempting completed feedback requests. |
| EGR-609 | Raw counters plus bounded daily buckets, injected UTC time, deterministic interval-end decay, half-life, sample floor, compaction, capacities, canonical order, and deterministic eviction are covered by tests and inspection. Reads do not extend standing. |
| EGR-610 | `EngramCandidateAuthority` produces Section 5 `HISTORY` only after the feedback floor and deterministically averages it with separately identified accepted-use history. Stale/policy exclusions remain hard gates. |
| EGR-611 | `NegativeResolutionKey` and `NegativeResolution` have strict codecs and exact identity, scope, constraints, available epoch, normalization, resolver-plan, readiness, and policy partitions. Admission uses an explicit knowledge-miss allow-list, is exact-plan-only until all knowledge sources share authoritative versioning, and excludes filtered, unavailable, failed, exhausted, truncated, collision, and indeterminate results. |
| EGR-612 | `NegativeResolutionStore` is memory-only, atomic, fixed-TTL, non-sliding, capacity-bounded, deterministically evicted, exact-key isolated, and invalidated by related state or namespace-epoch publication. Non-exact plans invalidate related records and execute normally, so newly added lexical or graph knowledge cannot be hidden. |
| EGR-613 | Unified resolution consults the negative owner only after frame/epoch/plan construction. Hits return bounded typed MISS with zero resolver/accounting work and complete time, output, working-memory, and diagnostic-budget enforcement. Same-request cache ordering, concurrent convergence, expiry, and exception fail-open behavior are specified and tested. |
| EGR-614 | `inspect_feedback_learning` returns bounded feedback partitions, receipt outcomes/lifecycle results, retention/evictions, negative occupancy and counters, hashed diagnostic IDs, and omission counts without raw request labels. |
| EGR-615 | Focused, compatibility, full-suite, restart/degradation, concurrency, concrete-absence, static, security, 5,000-record scale, benchmark, and 1,000-turn official MCP gates pass. |

## Verification results

| Command | Result |
| --- | --- |
| `python -m pytest -q tests/test_concrete_absence.py tests/test_feedback.py tests/test_service.py tests/test_fusion.py -x` | 86 passed in 40.81 seconds |
| `python -m pytest -q` | 1,292 passed in 96.72 seconds; no expected failures |
| `ruff check engram scripts eval tests` | all checks passed |
| `python -m black --check -q -l 132 -t py311 <Section 6 files>` with an isolated cache | all files unchanged |
| `npx --yes pyright@1.1.411` | 0 errors, 0 warnings, 0 informations |
| `python -m compileall -q engram scripts eval tests` | passed |
| `python -m vulture engram scripts eval --min-confidence 65` | no findings |
| `python -m bandit -q -lll <Section 6 production files>` | no high-severity findings |
| JSON validation and `git diff --check` | passed |
| `python scripts/benchmark_feedback.py --samples 100 --memory-records 100 --scale-records 5000` | all nine engineering gates passed |
| Section 6 invocation of `scripts/run_section3_mcp_conformance.py` | 1,000/1,000 turns and 1,003 protocol calls passed |

The initial complete-suite run identified the repository's concrete-absence rules in new annotations and sentinels. The completion review replaced them with concrete empty values and a read-only aggregate protocol, then added contract-version history filtering, generation-unavailable isolation, pre-lifecycle feedback replay checks, bounded constraint serialization, canonical state ordering, and proposal-path stale/policy filtering.

The findings review then closed five additional gaps: non-exact negative records can no longer hide newly added lexical knowledge; negative hits obey every request budget; 1,000-observation ingestion no longer collides with the generic receipt item limit; definite-before checkpoint recovery and uncheckpointed replay report accurate durability; and off-live preparation/state loading now has a representative 5,000-record regression gate. Focused, static, complete-suite, and benchmark checks were rerun after remediation. The prior MCP artifact remains applicable because Section 6 adds no MCP method and the remediated paths are transport-neutral below that unchanged adapter.

## Engineering benchmark

The reproducible offline result is [benchmark-2026-08-16.json](benchmark-2026-08-16.json), version `section6-feedback-negative-v1.1`. It uses synthetic content, a local in-memory Engram, no graph/model/network access, 100 timing samples, 100 records for traced-memory cases, and 5,000 preloaded feedback records for the scale case (the probe creates record 5,001).

| Operation | p50 | p95 | Gate |
| --- | ---: | ---: | ---: |
| Feedback ingestion with off-live snapshot and receipt | 41.8920 ms | 79.7913 ms | p95 < 250 ms |
| Feedback history lookup | 0.1304 ms | 0.1516 ms | p95 < 25 ms |
| Feedback-state JSON round trip | 25.4299 ms | 27.3049 ms | p95 < 250 ms |
| Ordinary completed exact miss | 6.0514 ms | 6.6940 ms | comparison |
| Negative-resolution hit | 1.8999 ms | 2.3146 ms | lower than ordinary p95 |
| Prepare against 5,000 records | 19.8090 ms | single scale probe | < 250 ms |
| 5,001-record state JSON round trip | 2,612.3414 ms | single scale probe | < 5,000 ms |

Negative-hit p95 was 34.58% of ordinary completed-miss p95. One hundred feedback records peaked at 278,802 traced bytes, and 100 negative records peaked at 154,103 bytes, below the 64 MiB and 32 MiB engineering gates. The populated 100-receipt feedback state serialized to 52,962 bytes versus 635 bytes empty. The 5,001-statement/5,001-relationship scale state serialized to 10,163,495 bytes, below its 64 MiB gate. These are regression measurements, not release SLOs.

## MCP evidence

The official-client artifact is [section6-mcp-conversation-1000-turns-2026-08-15.json](section6-mcp-conversation-1000-turns-2026-08-15.json). It called `engram_start`, issued 1,000 sequential `engram_send` calls, then called `engram_inspect` and `engram_stop`. The runner evaluated every turn for sequence and a nonempty response. Turn, inspection, and stop exchange counts all reached 1,000; 1,003 total protocol calls completed in 10.931 seconds with 4.4026/10.8212 ms p50/p95 turn latency. The bounded artifact stores no prompt or response bodies.

Section 15 still owns feedback/inspection adapter exposure. This MCP run is a cross-interface and long-conversation regression of the shared deployment path, not a claim that MCP exercised the unexposed Section 6 operations.

## Residual ownership

- Section 7 owns response-less Claim evidence handoff and usefulness.
- Section 15 owns MCP/gRPC exposure, authorization, redaction, cross-feature configuration and migration orchestration, operational metrics, dashboards, deployment, and rollback.
- Section 16 owns project-owned disjoint evaluation, empirical calibration, numerical release gates, final release testing, and approval.

No residual item prevents Section 6 implementation completion, and no release-quality claim is made.
