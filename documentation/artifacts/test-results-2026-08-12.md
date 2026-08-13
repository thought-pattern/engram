# Section 3 artifact verification results — 2026-08-12

## EGR-304 lifecycle domain

Implementation:

- `engram/artifacts.py`
- `documentation/artifacts/lifecycle-v1.md`
- `tests/test_artifacts.py`

Verification after the concrete-absence remediation:

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_artifacts.py -q` | 28 passed |
| `python -m pytest tests/test_artifacts.py tests/test_concrete_absence.py -q` | 32 passed |
| `python -m ruff check engram/artifacts.py tests/test_artifacts.py` | passed |
| `python -m black --check -l 132 -t py311 --workers 1 engram/artifacts.py tests/test_artifacts.py` | passed |
| `python -m pytest -q` | 991 passed, 1 pre-existing strict expected failure |

The full-suite remediation replaced an internal `dict.get`/`None` sentinel with explicit membership, preserving the repository's concrete-absence invariant. The tests cover the closed lifecycle vocabulary, complete base-eligibility matrix, every legal ACTIVE transition, all terminal-state rejections, operation/target mismatch, same-state retry separation, explicit historical-key reuse prerequisites, wrong concrete input types, and the rule that capacity eviction cannot mutate lifecycle.

## EGR-301 artifact contract and codecs

Implementation and contract:

- `CachedResponseArtifact`, `ArtifactProvenance`, and `ArtifactStatistics` in `engram/artifacts.py`
- `documentation/artifacts/artifact-contract-v1.md`
- expanded codec and boundary coverage in `tests/test_artifacts.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_artifacts.py -q` | 54 passed |
| `python -m ruff check engram/artifacts.py tests/test_artifacts.py` | passed |
| `python -m black --check -l 132 -t py311 --workers 1 engram/artifacts.py tests/test_artifacts.py` | passed |
| `python -m pytest -q` | 1,017 passed, 1 pre-existing strict expected failure |

The fixtures prove deterministic dictionary and compact sorted JSON codecs, byte-equal UTF-8 and Unicode-scalar response preservation, concrete absence, exact required fields, scope/identity consistency, tier and lifecycle types, support deduplication, temporal presence, integer epoch availability, supersession link consistency, nested provenance/statistics codecs, and deeply immutable bounded metadata. Malformed versions, fields, timestamps, types, null, non-finite numbers, non-string metadata keys, excessive nesting, self-links, and unsupported JSON roots are rejected without coercion.

## EGR-310 eligibility context and namespace epoch

Implementation and contract:

- `engram/eligibility.py`
- `documentation/artifacts/eligibility-context-v1.md`
- `tests/test_eligibility.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_eligibility.py -q` | 17 passed |
| `python -m pytest tests/test_eligibility.py tests/test_concrete_absence.py -q` | 21 passed |
| `python -m ruff check engram/eligibility.py tests/test_eligibility.py` | passed |
| `python -m black --check -l 132 -t py311 --workers 1 engram/eligibility.py tests/test_eligibility.py` | passed |
| `python -m pytest -q` | 1,034 passed, 1 pre-existing strict expected failure |

Coverage proves deterministic context codecs, one-read injected UTC capture, explicit uninitialized-epoch absence, independent trusted integration capture, initialized epoch zero, idempotent initialization, expected-epoch conflict, monotonic increments, bounded exhaustion, deterministic state snapshots, repository availability, wrong concrete type rejection, and concurrent single-winner increments.

## EGR-306 temporal and epoch eligibility

Implementation and contract:

- `EligibilityDecision`, `EligibilityExclusionReason`, `EpochEligibilityPolicy`, and `evaluate_artifact_eligibility` in `engram/eligibility.py`
- `documentation/artifacts/eligibility-decision-v1.md`
- expanded truth tables in `tests/test_eligibility.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_eligibility.py tests/test_artifacts.py tests/test_concrete_absence.py -q` | 99 passed |
| `python -m ruff check engram/eligibility.py tests/test_eligibility.py` | passed |
| `python -m black --check -l 132 -t py311 --workers 1 engram/eligibility.py tests/test_eligibility.py` | passed |
| `python -m pytest -q` | 1,058 passed, 1 pre-existing strict expected failure |

The truth tables cover all lifecycle states, open and closed bound combinations, exact start, exact end, before and after positions, equal and reversed intervals, required and conditional artifact epoch policies, matching and mismatching epochs, unavailable artifact/context epochs, namespace mismatch, dependency absence, stable exclusion precedence, captured-context output, and wrong concrete input types. Evaluation is pure and reads neither clock nor epoch state.

## EGR-311 projection and contextual exact refresh

Implementation and contract:

- `index_projection_from_artifact`, `ContextualExactLookup`, and `ContextualExactLookupResult` in `engram/eligibility.py`
- generic `IndexOwner.atomic_refresh_exact_lookup` in `engram/indexes.py`
- `documentation/artifacts/projection-refresh-v1.md`
- integration coverage in `tests/test_eligibility.py` and `tests/test_indexes.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_indexes.py tests/test_eligibility.py tests/test_artifacts.py tests/test_concrete_absence.py -q` | 146 passed |
| `python -m ruff check engram/indexes.py engram/eligibility.py tests/test_indexes.py tests/test_eligibility.py` | passed |
| `python -m black --check -l 132 -t py311 --workers 1 engram/indexes.py engram/eligibility.py tests/test_indexes.py tests/test_eligibility.py` | passed |
| `python -m pytest -q` | 1,069 passed, 1 pre-existing strict expected failure |

Tests prove that projection contains only Section 2 fields, decisions cannot be paired with another generation or namespace, refresh requires all and only current owners, refresh changes only eligibility fields, no-op refresh retains generation, stale state conflicts, exact expiration cannot return FOUND, epoch mismatch cannot return FOUND, a matching captured epoch can restore FOUND without artifact mutation, stale multi-owner collision is fully recomputed, missing artifact authority abstains, and empty lookup records its context without mutation.

## EGR-312 authoritative live repository

Implementation and contract:

- `engram/repository.py`
- `documentation/artifacts/repository-v1.md`
- `tests/test_repository.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_repository.py tests/test_eligibility.py tests/test_indexes.py tests/test_artifacts.py tests/test_concrete_absence.py -q` | 157 passed |
| `python -m ruff check engram/repository.py tests/test_repository.py` | passed |
| `python -m black --check -l 132 -t py311 --workers 1 engram/repository.py tests/test_repository.py` | passed |
| `python -m pytest -q` | 1,080 passed, 1 pre-existing strict expected failure |

Tests prove exact statement-ID authority; detached immutable compatibility derivation of response, identity, provenance, support, metadata, and statistics; conservative index construction; complete repository/view/index checking; off-live candidates; atomic next-generation publication; stale swap rejection; DYNAMIC-only capacity removal without lifecycle mutation; expected-generation explicit deletion; contextual refresh synchronization; view and projection corruption detection; duplicate and malformed input rejection; and concurrent single-winner publication with no partial ID sets.

## EGR-303 STATIC and DYNAMIC admission

Implementation and contract:

- `TierAdmissionPolicy`, `AdmissionPlan`, and `ArtifactRepository.plan_admission` in `engram/repository.py`
- `documentation/artifacts/tier-admission-v1.md`
- policy and integration coverage in `tests/test_repository.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_repository.py tests/test_eligibility.py tests/test_indexes.py tests/test_artifacts.py tests/test_concrete_absence.py -q` | 174 passed |
| `python -m ruff check engram/repository.py tests/test_repository.py` | passed |
| `python -m black --check -l 132 -t py311 --workers 1 engram/repository.py tests/test_repository.py` | passed |
| `python -m pytest -q` | 1,097 passed, 1 pre-existing strict expected failure |

Tests prove below-capacity DYNAMIC admission, STATIC admission without DYNAMIC displacement, explicit rejection when every victim is protected, unqueried-entry eviction, deterministic FIFO/LRU/LFU/hit-rate differentiation, multi-victim correction of migrated over-capacity state, lifecycle preservation, affected namespace reporting, candidate repository/view/index equivalence, atomic publication, ID collision rejection, and policy bound/type validation.

## EGR-313 durable mutation receipts

Implementation and contract:

- `engram/mutations.py`
- `documentation/artifacts/mutation-receipts-v1.md`
- `tests/test_mutations.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_mutations.py tests/test_import_hygiene.py tests/test_concrete_absence.py -q` | 21 passed |
| `python -m ruff check engram/mutations.py tests/test_mutations.py` | passed |
| `python -m black --check -l 132 -t py311 --workers 1 engram/mutations.py tests/test_mutations.py` | passed |
| `python -m pytest -q` | 1,112 passed, 1 pre-existing strict expected failure |

Tests prove order-independent Unicode-sensitive payload signatures; deterministic deeply immutable receipt codecs; exact completed replay; conflicting operation/payload secrecy; prepared/in-progress and one-way completion; exact sequence assignment; bounded result and tombstone retention; stable EXPIRED behavior; restart-equivalent replay/conflict/expiry and next sequence; concurrent single-winner sequence use; concrete before/after generations; and malformed, null, unsupported, or wrongly typed boundary rejection. The full-suite audit also remediated a function-local import to preserve repository import hygiene.

## EGR-309 persistence v2 and legacy migration

Implementation and contract:

- response state integration and migration in `engram/persistence.py`
- initialized response repository, epoch state, receipt ledger, and quarantine on `Engram`
- `documentation/artifacts/persistence-v2.md`
- `documentation/artifacts/persistence-v2-response-state.json`
- retained v1 fixture at `documentation/baseline/fixtures/regulated-response-v1.json`
- `tests/test_persistence_v2.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_persistence_v2.py tests/test_core.py::TestEngramPersistence tests/test_service.py::test_core_flush_restores_shared_state_but_not_transient_proposals tests/test_baseline_gaps.py::test_sanitized_regulated_response_fixture_matches_persistence_v1 tests/test_concrete_absence.py tests/test_import_hygiene.py -q` | 40 passed |
| `python -m pytest tests/test_persistence_v2.py tests/test_concrete_absence.py tests/test_import_hygiene.py -q` | 21 passed after the static fixture and quarantine-bound addition |
| `python -m ruff check engram/persistence.py engram/core.py tests/test_persistence_v2.py` | passed |
| `python -m black --check -l 132 -t py311 --workers 1 engram/persistence.py engram/core.py tests/test_persistence_v2.py` | passed |
| `python -m pytest -q` | 1,127 passed, 1 pre-existing strict expected failure |

Tests prove v2 artifact/epoch/receipt/quarantine restart; absence and startup reconstruction of derived response indexes; authoritative compatibility replacement; exact Unicode v1 response preservation; tier, scope, aliases, support, provenance, statistics, and metadata migration; missing and malformed identity quarantine; ambiguous owner quarantine plus COLLISION; non-mutating exact idempotence; prepared-receipt restart; strict response-state validation; quarantine codecs; static v1/v2 fixtures; existing config/session/general-statement compatibility; concrete absence; and import hygiene.

## EGR-314 atomic mutation coordinator

Implementation and contract:

- `engram/coordination.py`
- object-identity-preserving epoch and receipt restore in `engram/eligibility.py` and `engram/mutations.py`
- repository coordination and durable convergence in `engram/repository.py`
- `documentation/artifacts/mutation-coordinator-v1.md`
- `tests/test_coordination.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_coordination.py -q` | 11 passed |
| `python -m pytest tests/test_coordination.py tests/test_concrete_absence.py tests/test_import_hygiene.py -q` | 17 passed |
| `python -m ruff check engram/coordination.py engram/eligibility.py engram/mutations.py engram/repository.py tests/test_coordination.py` | passed |
| `python -m pytest -q` | 1,137 passed, 1 pre-existing strict expected failure after concrete-absence remediation |

Tests prove off-live candidate construction; repository/view/index/epoch/receipt equivalence; in-memory publication; checkpoint-before-publication ordering; exactly one configured checkpoint; definite failure without live change; indeterminate recovery of before, after, and divergent durable states; post-checkpoint convergence without a second write; explicit recovery-required reporting; unchanged capacity-rejection receipt publication; receipt-effect and namespace rejection; object-identity-preserving restore; and concurrent single-winner execution. The first repository-wide run found two callback return annotations that violated the concrete-absence AST gate; protocol callback types removed those literals, and the focused concrete-absence/import-hygiene rerun passed.

## EGR-302 transport-neutral base commit

Implementation and contract:

- `AcceptedResponseService.commit_response` and `ResponseMutationResult` in `engram/responses.py`
- candidate-aware atomic persistence in `engram/persistence.py`
- `documentation/artifacts/base-commit-v1.md`
- `tests/test_responses.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_responses.py tests/test_coordination.py tests/test_concrete_absence.py tests/test_import_hygiene.py -q` | 32 passed before lifecycle-test expansion |
| `python -m ruff check engram/coordination.py engram/responses.py engram/persistence.py tests/test_responses.py` | passed |
| `python -m pytest -q` | 1,153 passed, 1 pre-existing strict expected failure |

Tests prove complete artifact validation; exact Unicode preservation; empty and complete normalized IDK rejection; base-generation and ACTIVE restrictions; reserved audit metadata protection; named canonical/alias collision owners; cross-scope independence; exact replay with no second checkpoint; changed retry conflict; concurrent same-request single checkpoint; deterministic DYNAMIC eviction effects; protected-capacity receipt without repository/epoch change; real atomic-file restart; and indeterminate committed-file recovery using the durable authority signature.

## EGR-305 audited invalidation and retirement

Implementation and contract:

- typed lifecycle reasons and terminal commands in `engram/responses.py`
- reserved persistent `lifecycle_audit` artifact metadata
- `documentation/artifacts/lifecycle-mutations-v1.md`
- expanded `tests/test_responses.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_responses.py tests/test_concrete_absence.py -q` | 25 passed |
| `python -m ruff check engram/responses.py tests/test_responses.py` | passed |
| `python -m pytest -q` | 1,159 passed, 1 pre-existing strict expected failure |

Tests prove legal ACTIVE-to-INVALIDATED and ACTIVE-to-RETIRED transitions; exact generation effects; caller, typed reason, request, detail, and timestamp audit persistence; exact response preservation; lifecycle-ineligible projections; namespace epoch increments; exact replay; changed retry and stale generation conflict; terminal-state rejection; concurrent invalidation-versus-retirement single winner; tier independence including STATIC retirement; and artifact/audit/receipt restart.

## EGR-307 explicit supersession

Implementation and contract:

- `AcceptedResponseService.supersede_response` in `engram/responses.py`
- multi-artifact candidate composition in `engram/repository.py`
- `documentation/artifacts/supersession-v1.md`
- expanded `tests/test_responses.py`

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_responses.py tests/test_repository.py tests/test_concrete_absence.py -q` | 59 passed |
| `python -m ruff check engram/repository.py engram/responses.py tests/test_responses.py` | passed |
| `python -m pytest -q` | 1,165 passed, 1 pre-existing strict expected failure before EGR-308 |

Tests prove explicit expected-owner canonical/alias reuse; named other-owner collision; new statement identity; old generation, SUPERSEDED state, link, exact response, and audit preservation; replacement creation; complete receipt effects; exact replay; DYNAMIC rejection when lineage would be evicted; unrelated victim eviction with STATIC lineage; namespace effects; concurrent single-winner generation checking; and artifact/link/receipt restart.

## EGR-308 LearnResponse compatibility

Implementation and contract:

- compatibility artifact construction and authoritative query/hit accounting in `engram/responses.py`
- coordinator integration, exact proposal preference, legacy view synchronization, user context, and durable failure translation in `engram/service.py`
- candidate-aware compatibility persistence in `engram/persistence.py`
- statistics-only epoch classification in `engram/coordination.py`
- `documentation/artifacts/learn-response-compatibility-v1.md`
- expanded service, MCP, gRPC, graph-support, and baseline regression tests

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_service.py tests/test_mcp_server.py tests/test_grpc_server.py tests/test_baseline_gaps.py tests/test_responses.py tests/test_coordination.py tests/test_persistence_v2.py tests/test_concrete_absence.py tests/test_import_hygiene.py -q` | 111 passed before final wrapper/accounting additions |
| `python -m ruff check engram/responses.py engram/repository.py engram/service.py engram/persistence.py` | passed |
| `python -m pytest -q` | 1,170 passed; no expected failures |

Tests prove DYNAMIC ACTIVE construction; deterministic statement identity; exact Unicode, scope, provenance, metadata, support, user context, and IDK behavior; durable exact retry across restart without a second checkpoint; changed retry; named collision instead of implicit replacement; distinct when/where exact retrieval; required-metadata filtering; query/hit generation and view equivalence without epoch churn; terminal exclusion; vector support; checkpoint-before-publication failure with `state_changed=false`; and compatible Python, MCP, and gRPC results. The Section 0 strict expected failure is now a normal passing regression.

## EGR-315 Section 3 conformance gate

Final review remediations:

- bounded internal query/hit receipt identities use a deterministic SHA-256 derivation rather than prefixing maximum-length caller IDs;
- stale MCP replacement documentation now matches base-commit collision and explicit-supersession semantics;
- production codec, repository, persistence, receipt, and service mapping boundaries validate and narrow dynamic objects under strict typing;
- accepted resolution initializes its artifact-owner set on every control-flow path; and
- the MCP harness follows the concrete-absence policy and captures server metadata before its client context closes.

| Command | Result |
| --- | --- |
| `python -m pytest -q tests/test_artifacts.py tests/test_eligibility.py tests/test_repository.py tests/test_mutations.py tests/test_persistence_v2.py tests/test_coordination.py tests/test_responses.py tests/test_service.py tests/test_mcp_server.py tests/test_grpc_server.py tests/test_indexes.py` | 289 passed in 35.09s |
| `python -m pytest -q` | 1,171 passed in 70.02s; no expected failures |
| `python -m ruff check engram scripts tests eval` | all checks passed |
| `python -m black --check -l 132 -t py311 --workers 1 <Section 3 implementation and tests>` | 24 files unchanged |
| `npx --yes pyright@1.1.411` | 0 errors, 0 warnings, 0 informations |
| `python -m vulture engram scripts eval --min-confidence 65` | no findings |
| `python -m bandit -q -lll <Section 3 production files>` | no high-severity findings |
| `python -m compileall -q engram scripts` | passed |
| `python scripts/benchmark_indexes.py` | every applicable ADR 0004 gate passed |
| `python scripts/run_section3_mcp_conformance.py --turns 1000` | 1,000/1,000 sequential MCP conversation turns passed; 1,003 total protocol calls |

The fresh benchmark measured exact lookup p95 0.006717/0.006554 ms at 10,000/100,000 projections (0.9757x slope), full support-proposal p95 0.7611/1.3051/1.7207 ms at fan-out 1/10/100, rebuild p95 358.3101 ms at 5,000 projections, and peak traced build memory 3,981,121 bytes. Every absolute, slope, baseline-relative, rebuild, and memory gate passed.

The recorded official MCP client run called `engram_start`, issued 1,000 sequential `engram_send` calls, then called `engram_inspect` and `engram_stop`. Turn numbering, nonempty response presence, inspection count, and final exchange count all equaled 1,000. Evidence is in `mcp-conversation-1000-turns-2026-08-12.json`; the reproducible runner stores no raw prompt or response bodies.

The complete evidence matrix, remediation narrative, benchmark hashes, and exit assessment are in `section3-conformance-2026-08-12.md`. Operational response for definite and indeterminate checkpoint failures, post-publication recovery, startup corruption, migration quarantine, index repair, capacity, time/epoch exclusions, and retry conflicts is in `section3-recovery-runbook.md`.

## EGR-417 Section 4 unified resolution conformance gate

Implementation and evidence:

- versioned resolution contracts and frame construction in `engram/resolution.py`;
- pure resolver adapters, deterministic planning, bounded fail-soft execution, centralized accounting, and baseline orchestration in `engram/resolvers.py`;
- side-effect-free discovery and score components in `engram/core.py` and `engram/scoring.py` with unchanged legacy wrappers;
- atomic accepted-response query/success finalization in `engram/responses.py` and `engram/mutations.py`;
- transport-neutral `EngramCore.resolve_request` in `engram/service.py`;
- `documentation/artifacts/resolution-contract-v1.md` and `documentation/artifacts/section4-conformance-2026-08-12.md`;
- `scripts/benchmark_resolution.py`; and
- expanded contract, resolver, core, persistence, accounting, and wrapper tests.

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_resolution_contracts.py tests/test_resolvers.py -q` | 52 passed |
| `python -m pytest -q` | 1,223 passed in 68.48 seconds; no expected failures |
| `python -m ruff check engram scripts eval tests` | all checks passed |
| isolated-cache `python -m black --check -q -W 1 -l 132 -t py311 <Section 4 files>` | all files unchanged |
| `npx --yes pyright@1.1.411` | 0 errors, 0 warnings, 0 informations |
| `python -m vulture engram scripts eval --min-confidence 65` | no findings |
| `python -m bandit -q -lll <Section 4 production files>` | no high-severity findings |
| `python -m compileall -q engram scripts eval tests` | passed |
| `git diff --check` | passed |
| `python scripts/benchmark_resolution.py` | all seven gates passed |
| Section 4 invocation of `scripts/run_section3_mcp_conformance.py` | 1,000/1,000 turns passed; 1,003 protocol calls |

The benchmark measured frame construction p95 1.2112 ms, exact adapter p95 1.0181 ms, lexical adapter p95 48.2692 ms over 1,000 statements, completed/failed executor p95 0.1860/0.1149 ms, resolver-result codec p95 0.9917 ms, and peak traced lexical memory 599,396 bytes. All declared engineering gates passed.

The official MCP client run called the persistent server for 1,000 sequential conversation turns plus start, inspect, and stop. Turn numbering, nonempty response presence, inspection count, and stop exchange count all equaled 1,000; p95 turn latency was 10.6658 ms. Evidence is in `section4-mcp-conversation-1000-turns-2026-08-12.json` and contains no raw prompt or response bodies.

The comprehensive review additionally remediated stale caller deadlines, availability-check exceptions, nested and over-reported resource bounds, complete serialized-result envelope measurement and truncation, a two-receipt accounting crash window, restart replay credit, vector limit pushdown, duplicate preprocessing, leaked compatibility result keys, private matcher access, nested JSON bounds, and every pinned type-check finding. The complete matrix is in `section4-conformance-2026-08-12.md`.

## 13 August 2026 independent Section 4 evaluation remediation

The independent evaluation found six additional correctness and contract gaps. The implementation now determines the final output-budget outcome before accepted-success accounting; checks deadlines cooperatively inside resolver work, rejects late returned results, and bounds retained local working sets; uses bounded, cache-synchronized retry retention with elapsed-independent signatures; enforces lossless `ResolutionResult` state invariants; preserves unavailable resource measurements; and exposes semantic similarity independently from priority, vector weight, and the legacy combined retrieval score. The development plan and tracker now advance to Section 5 rather than describing completed Section 4 work as next.

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_resolution_contracts.py tests/test_resolvers.py -q` | 60 passed |
| `python -m pytest tests/test_concrete_absence.py tests/test_resolution_contracts.py tests/test_resolvers.py -q` | 64 passed |
| `python -m pytest -q` | 1,231 passed in 75.83 seconds; no expected failures |
| `python -m ruff check engram scripts eval tests` | all checks passed |
| isolated-cache `python -m black --check -q -W 1 -l 132 -t py311 <Section 4 files>` | all files unchanged |
| `npx --yes pyright@1.1.411` | 0 errors, 0 warnings, 0 informations |
| `python -m vulture <Section 4 production files> --min-confidence 65` | no findings |
| `python -m bandit -q -lll <Section 4 production files>` | no high-severity findings |
| `python -m compileall -q engram scripts eval tests` | passed |
| `git diff --check` | passed |
| `python scripts/benchmark_resolution.py` | all seven gates passed |

The fresh 200-sample benchmark measured frame construction p95 1.0460 ms, exact adapter p95 1.2379 ms, lexical adapter p95 53.1342 ms over 1,000 statements, completed/failed executor p95 0.2686/0.1475 ms, resolver-result codec p95 0.9209 ms, and peak traced lexical memory 108,950 bytes. The timestamped result is in `section4-benchmark-2026-08-12.json`; all declared engineering gates passed.

The 12 August MCP evidence remains the transport regression for Section 4. Unified resolution is intentionally not exposed on the wire until Section 15, so these remediations do not alter the MCP request path.
