# Section 5 candidate-fusion conformance — 15 August 2026

## Result and scope

All ten Section 5 implementation tasks meet their conformance exit conditions after the 15 August working-memory remediation. Candidate fusion is versioned, deterministic, bounded, current-state-aware, integrated into transport-neutral resolution, and fail-soft. Fusion receives only the memory allowance remaining after resolver execution, returns a conservative deterministic working-set estimate, and contributes that estimate to complete resolution consumption. The initial `fusion-v1.0.0` policy is explicitly hand-authored and unfitted; its stable fingerprint is `1f9d19acaedc277b4916bc366e74dc8c03d921e335acd21ac7fc2f1b449d963c`.

Unit tests, regression cases, and deterministic acceptance fixtures were used only to prove contracts and invariants. They were not used to select weights, thresholds, source-count rules, support requirements, or the ambiguity margin. Independent empirical calibration, repeated-run statistics for nondeterministic producers, release-gate evaluation, final testing, and release approval remain owned by Section 16.

## Task evidence

| Task | Implemented evidence |
| --- | --- |
| EGR-501 | The closed 13-feature `FusionFeature` vocabulary and executable `FEATURE_DEFINITIONS` specify producer, owner, trust boundary, ranges, availability, direction, aggregation, and policy-stage role. |
| EGR-507 | `FusionPolicy`, `NormalizedFeatureSet`, `CandidateEligibility`, `FusionContribution`, `FusedCandidate`, and `FusionDecision` have strict versioned dictionary/JSON codecs, concrete absence, stable reasons, fingerprints, bounded reports, and concrete fusion working-memory consumption. |
| EGR-504 | `EngramCandidateAuthority` reuses Section 3 artifact eligibility and revalidates lifecycle, time, epoch, scope, generation, response, metadata, source, visibility, support completeness, and current support references. Missing authority abstains by default. |
| EGR-502 | `FeatureNormalizer` accepts only source-appropriate current signals, preserves raw candidate provenance, applies documented monotonic transforms, and does not conflate priority, vector weight, or legacy blended retrieval score with relevance or history. |
| EGR-503 | Per-candidate hard filtering precedes statement grouping. Canonical ordering, response consistency, candidate/evidence identity conflict handling, evidence deduplication, and resolver-family agreement are deterministic and order-invariant. |
| EGR-505 | Formula version 1 exposes every normalized value, weight, weighted contribution, conservative aggregation, score, policy value, and policy fingerprint without a model or hidden inference. |
| EGR-506 | Deterministic ranking, top-two margin, explicit conflict, conservative mismatch aggregation, support, independent-family, entity/relation/type, and current-state gates force typed abstention when safety conditions fail. |
| EGR-509 | `ResolutionOrchestrator` consumes `FusionDecision`, permits qualified non-exact ANSWER, retains response-candidate EVIDENCE, preserves exact handling, output fitting, resolver isolation, budgets, and exactly-once candidacy accounting. Resolver working memory is reserved before fusion, the remaining allowance is enforced, and final consumption includes both stages. Non-exact fusion does not implicitly record regulator acceptance. |
| EGR-508 | The unfitted conservative initial weights and gates are frozen in `FusionPolicy`, pinned by codec/fingerprint tests, included in reports and benchmarks, and documented with Section 16 as the empirical-calibration owner. |
| EGR-510 | Focused, full-suite, concrete-absence, static, security, resource, deterministic acceptance, remaining-memory, complete-consumption, and 1,000-turn MCP checks passed. |

## Verification results

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_concrete_absence.py tests/test_fusion.py tests/test_resolution_contracts.py tests/test_resolvers.py -q` | 94 passed |
| `python -m pytest -q` | 1,266 passed in 88.26 seconds; no expected failures |
| `python -m ruff check engram scripts eval tests` | all checks passed |
| `python -m black --check -q -l 132 -t py311 <Section 5 files>` with an isolated cache | all files unchanged |
| `npx --yes pyright@1.1.411` | 0 errors, 0 warnings, 0 informations |
| `python -m vulture engram scripts eval --min-confidence 65` | no findings |
| `python -m bandit -q -lll <Section 5 production files>` | no high-severity findings |
| `python -m compileall -q engram scripts eval tests` | passed |
| `git diff --check` | passed |
| `python scripts/benchmark_fusion.py --samples 200` | all six latency, measured-memory, estimated-memory, and deterministic-acceptance gates passed |
| Section 5 invocation of `scripts/run_section3_mcp_conformance.py` | 1,000/1,000 turns and 1,003 protocol calls passed |

## Performance evidence

The reproducible offline result is [benchmark-2026-08-15.json](benchmark-2026-08-15.json). It records the policy, its unfitted provenance, the explicit permissive authority used only by synthetic fixtures, all six deterministic scenario outcomes, and the host environment.

| Operation | p50 | p95 | Gate |
| --- | ---: | ---: | ---: |
| Former Section 4 reinforced-pair selection | 0.0016 ms | 0.0019 ms | comparison only |
| Section 5 reinforced-pair fusion and accounting | 2.7806 ms | 4.8659 ms | p95 < 5 ms |
| 100 distinct candidates | 91.0377 ms | 99.4548 ms | p95 < 150 ms |
| 1,000 distinct candidates | 914.1076 ms | 1,161.5992 ms | p95 < 1,250 ms |

Peak traced memory for 1,000 candidates was 5,808,561 bytes, below the 64 MiB engineering gate. The conservative fusion working-set estimate was 3,580,777 bytes, below the 16 MiB frame allowance. All six deterministic outcome fixtures passed. These measurements are engineering regression bounds, not statistical relevance evidence or Section 16 release SLOs.

## MCP evidence

The official-client artifact is [section5-mcp-conversation-1000-turns-2026-08-15.json](section5-mcp-conversation-1000-turns-2026-08-15.json). It called `engram_start`, issued 1,000 sequential `engram_send` calls, then called `engram_inspect` and `engram_stop`. Every turn returned a nonempty response; turn, inspection, and stop exchange counts reached 1,000. The run completed 1,003 MCP calls with p50/p95 turn latency of 4.3597/10.7685 ms. The bounded artifact stores no raw prompt or response bodies.

Unified resolution remains transport-neutral and Section 15 still owns its wire exposure. This MCP run is therefore a cross-interface and long-conversation regression of the repository deployment path, not proof that MCP exercised fusion.

## Residual ownership

- Section 6 owns feedback-derived history producers.
- Section 7 owns response-less Claim eligibility and usefulness.
- Section 8 owns contextual entity, relation, and object-type producers.
- Section 9 owns semantic graph conflict, trust, temporal, and visibility policy.
- Sections 12–14 own optional resolver implementations and their raw feature producers.
- Section 15 owns adapter exposure, configuration, telemetry, deployment, and rollback.
- Section 16 owns independently labeled partitions, empirical policy calibration, repeated-run analysis for nondeterministic behavior, numerical release gates, final-test execution, and release approval.

No residual item prevents Section 5 implementation completion, and no release-quality claim is made.
