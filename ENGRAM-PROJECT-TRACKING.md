# Engram Project Tracking

**Audience: Internal | Status: Enhancement program In Progress, 87/175 | 16 August 2026**

Last updated: 2026-08-16

Tracks implementation status only. Components and ordering follow [ENGRAM-DEVELOPMENT.md](ENGRAM-DEVELOPMENT.md). Each checkbox is a buildable, checkable artifact. Status is assessed against the active Engram codebase. Commit, review, and merge state are human workflow concerns and do not determine whether implementation work is Done.

This file is the implementation-status authority in this repository for the Engram enhancement program. The Engram project's upstream tracker retains upstream authority. In an integration workspace, Tapestry status remains governed by `tapestry-source/PDC-PROJECT-TRACKING.md`; that external source tree is not vendored here.

---

## Critical Policies

### Accepted response authority

Engram stores and returns the exact accepted response. Retrieval normalization, aliases, phrasing, fusion, and adapters do not rewrite it. In a Tapestry deployment, current Claim validation and the Regulator remain the release boundary.

### Identity and lifecycle

Query identity is separate from lexical search terms. Tier controls eviction; lifecycle and validity control eligibility. Retrieval aliases are non-executable data, distinct from AIML matcher aliases.

### Bounded, offline execution

Every resolver has explicit time, result, memory, graph, and output bounds. Models and language resources are provisioned before startup; tests and normal runtime do not depend on Internet downloads or uncontrolled external services.

### Graph and data boundary

Runtime graph access remains capability-limited and read-only. Metrics and logs omit request text, response text, raw customer graph content, credentials, and unbounded identifier labels by default. An Engram failure never blocks Tapestry's Actor path.

### Authoritative state and interfaces

Persisted response artifacts are authoritative; secondary indexes are rebuildable. Core behavior is transport-neutral, and Python, MCP, and gRPC adapters validate and translate rather than reimplement retrieval, lifecycle, accounting, or policy.

### Concrete absence semantics

Core, persistence, and transport outputs do not generate `None` or JSON `null`. Every field has one concrete type and a documented falsy empty value. A separate presence or availability field distinguishes unavailable data when the empty value is itself meaningful. Boundary adapters normalize omitted inputs immediately; new core annotations avoid optional union types, and §0 inventories legacy uses before migration.

## Status Legend

- [ ] Not Started
- [~] In Progress
- [x] Done
- [-] Blocked

Done means built, tested, documented, and verified with the section's exit condition met. The adjacent section evidence must identify the audited source state, focused and applicable full-suite results, migration or adapter coverage when affected, and benchmark or evaluation artifacts when retrieval or answer policy changes. Commit, review, publication, and merge are deliberately outside completion accounting. Active work does not increase the completed-item count.

Deferred work remains Not Started. Blocked means a named external decision, dependency, or authorization prevents progress; it is not a synonym for deprioritized.

## Component Overview

| § | Component | Priority | Depends on | Status | Progress |
| --- | --- | --- | --- | --- | ---: |
| 0 | Source reconciliation and baseline | P0 | — | Done | 6/6 |
| 1 | Query identity and retrieval aliases | P0 | §0 | Done | 12/12 |
| 2 | Exact, alias, and support indexes | P0 | §§0-1 | Done | 11/11 |
| 3 | Accepted response commit and lifecycle | P0 | §§1-2 | Done | 15/15 |
| 4 | Unified resolution pipeline | P0 | §§1-3 | Done | 17/17 |
| 5 | Candidate fusion and ambiguity | P1 | §4 | Done | 10/10 |
| 6 | Feedback learning and negative resolution | P1 | §§3-5 | Done | 15/15 |
| 7 | Response-less Claim evidence and Tapestry package | P1 | §§4-5 | Not Started | 0/12 |
| 8 | Contextual query-frame enrichment and relation-aware graph lookup | P2 | §§4-5, 7 | Not Started | 0/10 |
| 9 | Temporal, trust, and conflict semantics | P2 | §§7-8 | Not Started | 0/9 |
| 10 | Bounded graph composition | P2 | §§8-9 | Not Started | 0/8 |
| 11 | Symbolic retrieval rewrite layer | P3 | §§1, 4-5 | Not Started | 0/7 |
| 12 | Sparse retrieval enhancement | P3 | §§1-5 | Not Started | 0/8 |
| 13 | Standalone semantic retrieval and reranking | P3 | §§5, 12 | Not Started | 0/9 |
| 14 | Utility resolver plugins | P3 | §§4-5 | Not Started | 0/6 |
| 15 | Interfaces, migration, security, and operations | Continuous | Cross-cutting | In Progress | 1/10 |
| 16 | Evaluation, rollout, and release | Continuous | Cross-cutting | Not Started | 0/10 |
| | **Program total** | | | **In Progress** | **87/175** |

P0 establishes correctness and the shared architecture. P1 uses those contracts to improve regulated recall and evidence handoff. P2 adds structured graph depth after unified resolution is stable. P3 work is optional and advances independently only when held-out evaluation justifies its resource and operating cost. §§15-16 apply throughout.

Sections 0 through 6 are complete. The current build order continues with §7; §8 consumes its full evidence package for relation-aware fallback, §15 later exposes that stabilized core contract, and §16 independently measures evidence usefulness and avoided Tapestry work. EGR-1508 is complete: startup preflights NLTK, spaCy, graph, vector-index, model, and dimension readiness, while serving paths perform no dependency acquisition. Section 5 and Section 6 conformance fixtures are not training data. Later components may prepare research and evaluation fixtures, but implementation does not bypass the completed identity, index, lifecycle, accepted-response, unified-resolution, fusion, and feedback foundations unless the affected dependency and development plan are revised together.

## Documented Baseline

These behaviors are reported by the source documentation and were reconciled in §0. They are descriptive baseline capabilities rather than additional items in the 175-item enhancement count. The source documentation identifies a `pdc-3` branch with support-aware sentence embeddings and Schema 3.5 graph behavior; EGR-001 records the audited source state and corrections.

| Baseline area | Reported current capability |
| --- | --- |
| Core and adapters | One `EngramCore` shared by Python, CLI, MCP, and a single-instance gRPC server. |
| Statements and storage | STATIC and DYNAMIC statement tiers, JSON persistence, atomic mutation checkpoints, and configurable DYNAMIC eviction. |
| Retrieval | AIML-style pattern matching and IDF-weighted keyword retrieval with lexical normalization, statistics, and optional spaCy phrase terms. |
| Conversation | User-isolated histories, predicates, topics, entities, dialogue acts, fact learning, repetition handling, and conservative pronoun expansion. |
| Regulated cache | Exactly scoped `Propose`, typed and idempotent `Resolve`, DYNAMIC `LearnResponse`, and DYNAMIC patternless `RetireResponse`. |
| Graph | Optional read-only canonical Claim, Entity, and Predicate recall with active-Claim filtering. |
| Semantic support recall | Local CPU request embeddings search the Claim-premise vector index and intersect results with cached response support IDs. |
| Service operation | Health, typed errors, TLS, graceful shutdown, durability degradation, retry-safe regulated mutations, and bounded transient proposals. |

## Integration Gates

| Gate | Required workstreams | Outcome |
| --- | --- | --- |
| A: Baseline accepted | §0 | Audited source state, current contracts, tests, defects, and performance are reproducibly recorded. |
| B: Identity-safe cache | §§1-3 and applicable §§15-16 | Scoped canonical requests and aliases resolve one unchanged accepted response without semantic overwrite; lifecycle is explicit. |
| C: Unified resolution | §§4-7 and applicable §§15-16 | Common ANSWER, EVIDENCE, and MISS semantics, fusion, feedback, and response-less Claim evidence are available. |
| D: Structured graph resolution | §§8-10 and applicable §§15-16 | Bounded relation, temporal, conflict-aware, and shallow graph resolution passes its held-out gates. |
| E: Retrieval expansion | Any promoted subset of §§11-14 plus §§15-16 | An optional resolver is released only after measurable value and operating gates pass. |

Gate E does not require every P3 workstream. Each optional resolver is independently promotable.

## §0. Source reconciliation and baseline (6/6)

**Priority:** P0  
**Entry:** Access to the active Engram source branch and its normal development dependencies.  
**Exit:** Gate A.

1. [x] **EGR-001: Record the audited source revision.** Capture repository URL, branch, commit, Python version, dependency lock state, graph schema version, generated gRPC tool versions, and the date of the audit.
2. [x] **EGR-002: Map documented behavior to implementation.** Identify the modules, data structures, configuration fields, persistence fields, tests, and legacy `None` or optional-union uses for every row in the documented baseline table; record discrepancies as defects or document corrections.
3. [x] **EGR-003: Inventory persisted regulated responses.** Capture sanitized examples of statement, keyword source, `tapestry` metadata, support Claim IDs, statistics, tier, proposal state, and idempotency records without exposing customer content.
4. [x] **EGR-004: Reproduce known correctness gaps.** Add failing or characterization tests for equal-keyword `when`/`where` replacement, alias absence, exact lookup absence, lifecycle/tier coupling, and any other confirmed gap.
5. [x] **EGR-005: Capture baseline performance.** Measure exact repeated request behavior, lexical proposal latency, support-aware vector proposal latency as corpus size and support fan-out grow, startup, persistence, memory, p50, and p95.
6. [x] **EGR-006: Establish architecture decisions.** Record decisions for persisted artifact shape, concrete falsy absence values and presence indicators, normalization versioning, collision policy, lifecycle concurrency, derived-index persistence, evidence wire format, evaluation time, knowledge epoch, and first numerical release gates.

**Evidence required:** source-audit report, baseline test output, benchmark artifact, sanitized schema fixtures, and accepted decision records.

### Section 0 evidence

- [Source audit and implementation map](documentation/baseline/source-audit-2026-08-11.md)
- [Focused and full verification output](documentation/baseline/test-results-2026-08-11.md): initial 803 passed plus the 12 August remediation addendum; the retained replacement regression was resolved by EGR-308
- [Reproducible benchmark output](documentation/baseline/benchmark-2026-08-11.json) and harness at `scripts/benchmark_section0.py`
- [Sanitized regulated-response fixture](documentation/baseline/fixtures/regulated-response-v1.json)
- Accepted ADRs: [artifact and absence](documentation/decisions/0001-authoritative-response-artifact-and-absence.md), [normalization/collision/lifecycle](documentation/decisions/0002-normalization-collisions-and-lifecycle.md), [indexes/evidence wire](documentation/decisions/0003-derived-indexes-and-evidence-wire.md), and [time/epoch/release gates](documentation/decisions/0004-evaluation-time-epoch-and-release-gates.md)

All six deliverables exist and verify in the current worktree. Section 0 is Done and Gate A is accepted. Confirmed baseline defects are recorded in the audit and intentionally carried into their owning enhancement sections. Commit and merge handling remain a human workflow decision.

## §1. Query identity and retrieval aliases (12/12)

**Priority:** P0  
**Depends on:** §0.  
**Exit:** Raw standalone requests and authoritative identity inputs produce validated, versioned, deterministically serializable identity and retrieval contracts. Identity-bearing contrasts produce distinct scoped retrieval keys; aliases are normalized and deduplicated as non-executable data; malformed or unsupported input fails explicitly without `None`, JSON `null`, optional unions, graph access, runtime dependency acquisition, or deferred model loading.

1. [x] **EGR-101: Add the versioned `ScopeKey` contract.** Validate bounded namespace and context-fingerprint strings, define equality and deterministic ordering, and provide concrete no-null dictionary and JSON codecs.
2. [x] **EGR-102: Define the identity component contracts.** Add the closed operator vocabulary plus bounded entity reference, relation reference, and qualifier records with explicit concrete empty values and no optional union annotations.
3. [x] **EGR-103: Add the versioned `QueryIdentity` contract.** Include canonical form, operator, entities, relation, qualifiers, lexical terms, and `ScopeKey`; define equality, validation, deterministic serialization/deserialization, and explicit unsupported-version errors.
4. [x] **EGR-104: Implement retrieval normalization version 1.** Add a new identity-specific normalizer covering Unicode NFKC, case folding, apostrophes, contractions, whitespace, semantic edge punctuation, and preservation of internal technical tokens without changing the existing lexical and matcher normalization contract.
5. [x] **EGR-105: Add the `ScopedRetrievalKey` contract and builder.** Deterministically combine `ScopeKey`, normalization version, and one normalized retrieval representation into the immutable logical key consumed by §2 indexes and collision reporting.
6. [x] **EGR-106: Add `RetrievalRepresentation`.** Validate one required canonical representation and bounded aliases, normalize and deduplicate them deterministically, retain representation provenance, and keep them separate from response text and executable matcher `pattern_aliases`.
7. [x] **EGR-107: Extract operators and qualifiers before lexical filtering.** Preserve interrogative, negation, count, comparison, temporal, location, and other identity-bearing semantics in their typed identity fields without automatically injecting stopwords into lexical scoring terms.
8. [x] **EGR-108: Extract entity surfaces and technical identifiers.** Preserve explicit names, versions, symbols, paths, error codes, and similar identifiers conservatively while leaving uncertain canonical IDs as concrete empty strings and performing no graph lookup.
9. [x] **EGR-109: Extract relation surfaces and lexical terms.** Produce a conservative main-relation surface when supported, leave uncertainty concrete and explicit, and derive lexical terms separately through the existing keyword facilities without allowing those terms to define identity.
10. [x] **EGR-110: Add the standalone identity builder.** Orchestrate normalization and the surface-level extractors deterministically with no graph access, network access, runtime downloads, or deferred model loading; do not perform the contextual canonical resolution owned by §8.
11. [x] **EGR-111: Validate authoritative external identity.** Validate schema and normalization versions, bounds, scope consistency, canonical identifier syntax, representation consistency, and concrete absence values without graph existence checks or silent reinterpretation; §3 owns commit acceptance and §15 owns adapter translation.
12. [x] **EGR-112: Build the identity conformance and adversarial suite.** Cover codec round trips, determinism, normalization idempotence, malformed and oversized inputs, technical tokens, concrete absence, scope separation, and identity contrasts including `when`/`where`, `who`/`what`, current/historical, positive/negative, and count/lookup.

**Evidence required:** typed schema documentation, golden normalization fixtures, deterministic dictionary and JSON codec round trips, property tests for normalization and scoped keys, authoritative-input validation tests, concrete-absence checks, and the identity conformance corpus. Every task carries focused tests; EGR-112 supplies cross-contract coverage rather than deferring earlier verification.

**Ownership boundary:** §1 owns pure surface identity, representations, validation, and retrieval-key construction. Section 2 owns the index projection boundary, cross-artifact key mappings, collision discovery, generic index mutation, and atomic index state. Section 3 owns artifacts and receipts, request-time lifecycle/validity/epoch eligibility, feature persistence and migration, commit-time rejection, and explicit supersession. Section 8 owns contextual and graph-backed canonical entity/predicate resolution. Section 15 owns cross-feature schema/startup orchestration, operator, authorization, and transport integration, and §16 owns held-out release gates.

### Section 1 evidence

- [Version 1 identity contract](documentation/identity/contracts-v1.md)
- [Golden normalization and adversarial fixture](documentation/identity/normalization-v1.json)
- [Focused and full verification results](documentation/identity/test-results-2026-08-11.md): 103 focused tests passed; the then-retained replacement regression was resolved by EGR-308
- Implementation: `engram/identity.py` and shared dependency-free lexical selection in `engram/lexical.py`
- Conformance suite: `tests/test_identity.py`
- [Remediation benchmark](documentation/identity/remediation-benchmark-2026-08-11.json) and harness at `scripts/benchmark_identity.py`
- Cross-cutting remediation coverage: strict concrete mapping boundaries in `engram/config.py`, `engram/persistence.py`, and `engram/service.py`; transport-neutral graph/vector/spaCy preflight in `engram/core.py`; Python, MCP, and gRPC startup/status tests

All twelve identity tasks meet the Section 1 exit condition after remediation of symbolic comparison collisions, prose-dash operator loss, trailing-token relation guesses, falsey non-object mapping inputs, and enabled-component readiness. Normalization remains version 1 because no identity key had been released, persisted, indexed, or exposed through an integration boundary before correction. Gate B remains open because it also requires the §2 indexes and §3 accepted-response commit/lifecycle integration.

## §2. Exact, alias, and support indexes (11/11)

**Priority:** P0  
**Depends on:** §§0-1.  
**Exit:** Given validated index projections, exact ownership and direct lookup are constant-time relative to statement corpus size; Claim-supported lookup scales with matched Claim IDs and fan-out rather than all statements. Forward and inverse maps remain equivalent under rebuild and generic mutation, readers observe one checked `IndexState`, and collisions or unindexable legacy records produce explicit bounded reports rather than an arbitrary answer.

1. [x] **EGR-201: Define the index projection and state contracts.** Add bounded concrete `IndexProjection`, `IndexState`, typed exact-lookup result, build report, and collision report contracts. Carry statement ID, generation, retrieval-key provenance, support Claim IDs, direct-answer eligibility, exclusion reason, schema version, and normalization version without depending on §3 artifact or lifecycle types.
2. [x] **EGR-202: Implement the bidirectional exact and alias index.** Build scoped retrieval-key-to-statement and statement-to-retrieval-key maps as one invariant; retain canonical-versus-alias provenance, deduplicate representations within an artifact, expose every indexable owner to validation, and return explicit `FOUND`, `MISS`, or `COLLISION` lookup results without selecting a winner.
3. [x] **EGR-203: Implement the bidirectional support index.** Build Claim-to-statement and statement-to-Claim maps as one invariant with bounded opaque identifiers and explicit matched-Claim lookup.
4. [x] **EGR-204: Implement deterministic off-live index construction.** Build a complete candidate `IndexState` from validated projections without altering live state; deterministically classify missing identity, unsupported versions, malformed support, ineligible records, within-artifact duplicates, and cross-artifact collisions.
5. [x] **EGR-205: Add the complete index consistency checker.** Compare every forward and inverse mapping and reproducible build diagnostic; report missing, extra, asymmetric, ineligible, unindexable, conflicting, or corrupt-report entries without mutation. Self-check and explicit authoritative comparison are distinct, and an explicit empty projection set means empty.
6. [x] **EGR-206: Add the atomic live index owner.** Establish the re-entrant mutation boundary, lock order, immutable reader snapshot, and atomic completed-state swap so readers never observe half-updated maps or a rebuild with inconsistent maps or reproducible diagnostics.
7. [x] **EGR-207: Add generic incremental index mutations.** Support add, replace, remove, and support-update projections without lifecycle-specific verbs; prove each result equals a clean deterministic rebuild so §3 can safely compose commit and lifecycle operations.
8. [x] **EGR-208: Replace the support-aware statement scan.** Use the Claim-to-statement index after vector Claim matching, apply scope before bounded top-k scoring, remove work proportional to the complete statement corpus, and abstain without partial ranking when the configured matched-edge scan bound is exhausted.
9. [x] **EGR-209: Add transport-neutral index check, rebuild, and repair operations.** Return bounded dry-run diffs and perform an explicit checked atomic repair without changing authoritative response content; distinguish current-statement operations from explicit projection collections, including empty; leave persistence/startup wiring, adapter exposure, authorization, and operator procedures to §15.
10. [x] **EGR-210: Prove index invariants and concurrency safety.** Add rebuild-equivalence properties, canonical/alias collision cases, reader/writer tests, stale-state injection, abandoned candidate builds, legacy missing-identity classification, and compatibility tests for current support metadata. Each earlier task retains focused tests rather than deferring its verification here.
11. [x] **EGR-211: Prove index scalability and resource bounds.** Benchmark exact lookup through 100,000 projections, matched-Claim fan-out, rebuild, incremental mutations, and peak memory with p50, p95, workload shape, and comparison to the approved ADR 0004 engineering gates.

**Evidence required:** typed projection/state documentation, focused tests for every task, invariant and concurrency output, rebuild-equivalence artifact, collision and legacy-classification fixture, support-path integration results, complexity and memory benchmark, and transport-neutral check/repair documentation. Artifact/receipt persistence and startup derivation remain assigned to §3; cross-feature startup, adapter, operational, and release evidence remains assigned to applicable §§15-16 tasks.

**Ownership boundary:** §2 accepts projections and owns derived-index mechanics only. It does not infer lifecycle from tier, manufacture exact identity for legacy records, persist authoritative artifacts, or expose adapter operations. Section 3 creates and revalidates projections, owns artifact/receipt persistence and startup derivation, and composes generic mutations into commit and lifecycle transactions; §15 integrates cross-feature schema/readiness, adapters, authorization, backups, downgrade, and operator procedures; §16 evaluates release-scale gates.

### Section 2 evidence

- [Version 1 index contracts and transport-neutral repair behavior](documentation/indexes/contracts-v1.md)
- [Collision, legacy, unsupported-version, and malformed-support fixture](documentation/indexes/classification-v1.json)
- [Focused, full-suite, static-analysis, and benchmark results](documentation/indexes/test-results-2026-08-12.md): 36 focused tests passed; the then-retained Section 3 replacement regression was resolved by EGR-308
- Implementation: `engram/indexes.py`; live integration in `engram/core.py`, `engram/eviction.py`, and `engram/persistence.py`
- Invariant and integration suite: `tests/test_indexes.py`, with completed-boundary updates in `tests/test_baseline_gaps.py`
- [100,000-projection and full 5,000-artifact support-proposal benchmark](documentation/indexes/benchmark-2026-08-12.json) and harness at `scripts/benchmark_indexes.py`

All eleven tasks meet the Section 2 exit condition after the 12 August remediation and the fresh EGR-315 rerun. Exact lookup remained constant-time relative to corpus size with a measured 0.9757x p95 slope from 10,000 to 100,000 projections. The full 5,000-artifact regulated support proposal measured p95 0.7611/1.3051/1.7207 ms at fan-out 1/10/100 and passed both the 30 ms absolute and 125%-of-baseline gates. Scope filtering precedes bounded top-k selection, scan exhaustion abstains without a partial answer, explicit empty projection sets remain meaningful, reproducible diagnostic corruption blocks publication, forward/inverse equivalence holds under rebuild and every generic mutation, and collision or legacy exclusion never selects an arbitrary response. Section 3 now owns completed artifact/receipt persistence and startup derivation; cross-feature startup policy and adapter/operator exposure remain assigned to §15 rather than being counted here.

## §3. Accepted response commit and lifecycle (15/15)

**Priority:** P0  
**Depends on:** §§1-2.  
**Exit:** An authoritative core caller can durably create, retrieve, invalidate, retire, or explicitly supersede an unchanged accepted response as STATIC or DYNAMIC. Artifact state, compatibility views, derived indexes, namespace epoch, and mutation receipts remain equivalent across concurrency, eviction, checkpoint failure, migration, and restart. Expired, epoch-stale, conflicting, or non-ACTIVE artifacts cannot produce a direct exact result.

Tasks are listed in dependency order while retaining their stable EGR identifiers.

1. [x] **EGR-304: Define lifecycle states, legal transitions, and base eligibility.** Specify ACTIVE, SUPERSEDED, INVALIDATED, and RETIRED independently of tier and residency; publish terminal-state, historical-key-reuse, and eviction rules. Only explicit supersession may perform ACTIVE to SUPERSEDED, and lifecycle base eligibility must remain distinct from request-time temporal and epoch eligibility.
2. [x] **EGR-301: Add the deterministic `CachedResponseArtifact` contract and codecs.** Define concrete bounded fields for exact response text, query identity, retrieval representations, tier, lifecycle, scope, support, presence-bearing temporal bounds, nonnegative integer knowledge epoch plus availability, supersession, generation, provenance, statistics, metadata, and schema version. Prove round-trip Unicode equality and reject malformed or unsupported values without implicit coercion.
3. [x] **EGR-310: Add `EligibilityContext`.** Capture one injected UTC evaluation time, namespace epoch value and availability, dependency availability, and the trusted-input boundary per request. Define namespace epoch initialization and monotonic increment rules so eligibility tests never read ambient time or invent an unavailable epoch.
4. [x] **EGR-306: Implement pure temporal and epoch validation.** Validate half-open bounds, ordering, clock boundaries, epoch match policy, and unavailable dependencies against `EligibilityContext`; combine them with lifecycle base eligibility into a stable decision with bounded exclusion reason codes and truth-table tests.
5. [x] **EGR-311: Derive and safely refresh `IndexProjection`.** Project only §2 fields from a validated artifact and eligibility decision, without making the projection authoritative. Exact lookup must revalidate or lazily refresh request-dependent eligibility atomically so expiration or namespace epoch change cannot leave a stale direct-answer mapping.
6. [x] **EGR-312: Add the authoritative live artifact repository.** Provide statement-ID lookup and atomic candidate-state replacement; derive the legacy statement compatibility view and statistics from the same live artifacts. Define ownership, lock order, deletion and capacity-eviction behavior, and repository/view/index equivalence checks.
7. [x] **EGR-303: Integrate STATIC and DYNAMIC tier admission and eviction.** Protect STATIC artifacts from ordinary capacity eviction, apply bounded DYNAMIC admission and eviction, remove evicted artifacts through the repository mutation boundary, keep views and indexes synchronized, and distinguish residency loss from lifecycle or namespace-epoch change.
8. [x] **EGR-313: Add durable mutation receipts.** Persist bounded request identity, operation, canonical payload signature, result, affected generations, and completion state. Exact retries return the recorded result, conflicting retries return a stable conflict, retention is bounded, and restart behavior is specified and tested.
9. [x] **EGR-309: Add response persistence v2 and migrate legacy state.** Serialize authoritative artifacts and receipts, preserve exact text, tier, scope, support, provenance, and statistics, rebuild derived views and indexes at startup, and make v1 migration idempotent and recoverable. Quarantine unrecoverable or ambiguous identity instead of guessing; execute with the EGR-1505 operator/schema-management slice.
10. [x] **EGR-314: Add the atomic mutation coordinator.** Commit artifact state, compatibility views, indexes, namespace epoch effects, and mutation receipts as one candidate live state with exactly one configured checkpoint. Define checkpoint-before-publication, indeterminate durable outcomes, post-checkpoint publication and recovery failures, retry recovery, lock order, and proof that no partial state becomes visible.
11. [x] **EGR-302: Implement transport-neutral base `commit_response`.** Validate identity and artifact fields, reject empty and normalized complete `IDK`, preserve exact response text, and reject scoped canonical or alias collisions with named existing statement IDs. Create a new artifact or return an exact retry through the coordinator; base commit never supersedes an existing artifact.
12. [x] **EGR-305: Add audited invalidation and retirement.** Require typed reason, caller identity or provenance, request identity, expected generation, legal transition checks, and audit fields. Route each operation through the coordinator; do not expose a generic transition that can set SUPERSEDED.
13. [x] **EGR-307: Implement concurrency-safe explicit supersession.** Make supersession the only replacement path. Require the expected current statement ID and generation, create the replacement, link `superseded_by`, update retrieval mappings and receipts atomically, and return stable conflicts for stale or competing writers.
14. [x] **EGR-308: Preserve `LearnResponse` compatibility.** Implement it as a DYNAMIC ACTIVE wrapper over base commit while retaining retry identity, proposal accounting, user-context, and `IDK` behavior; it must not acquire implicit supersession semantics.
15. [x] **EGR-315: Complete the Section 3 conformance gate.** Exercise artifact/view/index/receipt equivalence under concurrent operations, restart, persistence failure, migration, clock boundaries, epoch changes, DYNAMIC eviction, STATIC retention, and exact or conflicting retries. Run the applicable Section 2 engineering performance gates and record recovery instructions.

**Ownership boundary:** Section 3 owns artifact and lifecycle contracts, the live repository, request-time validity and namespace-epoch behavior, tier admission and eviction, transport-neutral mutations, feature-level serialization and migration, durable mutation receipts, and the statement/`LearnResponse` compatibility views. Section 9 owns graph Claim validity, temporal query interpretation, trust, and conflict semantics. Section 15 owns Python, MCP, and gRPC exposure, authorization enforcement, schema-management and backup operator experience, downgrade and rollback, cross-adapter deadlines, cancellation, and concurrency, plus security, telemetry, and deployment.

**Evidence required:** deterministic artifact and projection codec fixtures; lifecycle and eligibility truth tables; injected-clock, epoch-availability, expiration, and epoch-change tests; repository/view/index equivalence; tier-capacity and eviction tests; persistent receipt exact-retry, conflicting-retry, retention, and restart tests; fixtures for every supported schema plus migration and quarantine recovery; concurrent transition and supersession tests; pre-write, during-checkpoint, and post-checkpoint publication/recovery failure tests; exact-text byte or Unicode equality; and applicable index complexity, latency, and memory gates.

### Section 3 evidence

- [Lifecycle contract v1](documentation/artifacts/lifecycle-v1.md), implemented in `engram/artifacts.py` and covered by `tests/test_artifacts.py`
- [Cached response artifact contract v1](documentation/artifacts/artifact-contract-v1.md), including deterministic concrete codecs and exact-text invariants
- [Eligibility context and namespace epoch contract v1](documentation/artifacts/eligibility-context-v1.md), implemented in `engram/eligibility.py`
- [Accepted-response eligibility decision contract v1](documentation/artifacts/eligibility-decision-v1.md), including ordered exclusion and boundary truth tables
- [Artifact projection and contextual exact refresh contract v1](documentation/artifacts/projection-refresh-v1.md), including the atomic generic index refresh primitive
- [Authoritative live artifact repository contract v1](documentation/artifacts/repository-v1.md), implemented in `engram/repository.py`
- [STATIC and DYNAMIC tier admission contract v1](documentation/artifacts/tier-admission-v1.md), including bounded rejection and deterministic eviction policies
- [Durable mutation receipt contract v1](documentation/artifacts/mutation-receipts-v1.md), implemented in `engram/mutations.py`
- [Accepted-response persistence v2 and migration contract](documentation/artifacts/persistence-v2.md), with [response-state v2](documentation/artifacts/persistence-v2-response-state.json) and [legacy v1](documentation/baseline/fixtures/regulated-response-v1.json) fixtures
- [Atomic mutation coordinator contract v1](documentation/artifacts/mutation-coordinator-v1.md), including checkpoint classification, publication recovery, and concurrency rules
- [Accepted-response base commit contract v1](documentation/artifacts/base-commit-v1.md), including exact-text, collision, retry, capacity, and real-checkpoint rules
- [Audited lifecycle mutation contract v1](documentation/artifacts/lifecycle-mutations-v1.md), covering invalidation, retirement, audit identity, generation conflicts, and restart
- [Explicit supersession contract v1](documentation/artifacts/supersession-v1.md), covering expected-owner reuse, linked generations, capacity, concurrency, and restart
- [LearnResponse compatibility contract v1](documentation/artifacts/learn-response-compatibility-v1.md), including durable retry, exact proposal lookup, accounting, support, and failure behavior
- [Section 3 conformance report](documentation/artifacts/section3-conformance-2026-08-12.md), including the complete requirement matrix, review remediations, static gates, fresh Section 2 benchmark, and 1,000-turn MCP protocol result
- [Section 3 recovery runbook](documentation/artifacts/section3-recovery-runbook.md), covering determinate and indeterminate checkpoint outcomes, publication recovery, startup, migration, quarantine, consistency repair, and safe retry
- [1,000-turn MCP evidence](documentation/artifacts/mcp-conversation-1000-turns-2026-08-12.json) and reproducible harness at `scripts/run_section3_mcp_conformance.py`
- [Incremental and final Section 3 verification results](documentation/artifacts/test-results-2026-08-12.md)

All fifteen tasks meet the Section 3 exit condition after the 12 August conformance review and remediation. The focused cross-contract suite passed 289 tests, the complete repository passed 1,171 tests with no expected failures, Pyright reported no errors or warnings, and the complete Section 3 production surface passed lint, compilation, dead-code, and high-severity security gates. The fresh Section 2 benchmark passed every applicable ADR 0004 engineering limit. A fresh official MCP client run completed 1,000 sequential conversation turns and 1,003 total protocol calls with matching inspect and stop counts. Section 4 may now consume the authoritative artifact, eligibility, index, receipt, and mutation contracts; cross-feature adapter/authorization and release-scale gates remain assigned to §§15-16.

## §4. Unified resolution pipeline (17/17)

**Priority:** P0  
**Depends on:** §§1-3.  
**Exit:** Given one validated base `QueryFrame` and configured resolver plan, every currently implemented response-retrieval path emits bounded versioned `Candidate` values and graph-only paths emit bounded `EvidenceReference` values through a common `ResolverResult`. A unique eligible exact result may produce ANSWER; bounded non-answer candidates or existing graph references produce EVIDENCE without authorizing a response; no usable output produces MISS. Resolver failure and budget exhaustion are typed and fail-soft, and candidacy and accepted-success accounting are applied centrally exactly once. Fusion, ambiguity, response-less Claim packaging, contextual graph resolution, and transport exposure remain owned by §§5, 7, 8, and 15.

Tasks are listed in implementation dependency order while retaining their stable EGR identifiers.

1. [x] **EGR-404: Define versioned resolution budget and consumption contracts.** Specify immutable total and per-resolver limits, cost classes, an injected monotonic deadline, candidates, graph rows, vector results, evidence, serialized output, diagnostics, bounded working-memory estimates, and deterministic reservation and consumption records. Distinguish configured limits, remaining allowance, exhaustion, and unavailable measurements with concrete values.
2. [x] **EGR-401: Add the versioned base `QueryFrame` contract.** Carry bounded original and conversationally resolved text, validated identity, the closed expected-object-type vocabulary, concrete inheritance and rewrite trace containers, scope, required metadata, immutable budget limits, and diagnostic identity. Section 4 defines the empty-capable fields; §§8 and 11 own contextual inheritance and rewrite population.
3. [x] **EGR-411: Build and validate the base `QueryFrame` at the trusted core boundary.** Accept authoritative or standalone §1 identity, enforce scope and representation consistency, capture one eligibility and budget context, perform shared baseline request preprocessing once, and produce deterministic frame diagnostics without graph lookup, follow-up inheritance, retrieval rewrites, runtime dependency loading, or transport-specific behavior.
4. [x] **EGR-402: Add common `Candidate`, `FeatureSet`, and minimal `EvidenceReference` contracts.** Represent exact accepted response text, resolver source, concretely typed feature values and availability, stable evidence identifiers, scope, lifecycle snapshot, provenance, and bounded diagnostics without optional unions. Section 5 owns feature meaning and normalization; §7 owns full response-less Claim evidence records and packages.
5. [x] **EGR-403: Add resolver and `ResolverResult` protocols.** Define resolver identity, availability, cost class, bounded input, completed, unavailable, skipped, exhausted, and failed states, candidates, evidence references, bounded diagnostics, elapsed time, and budget consumption. Resolver exceptions must be isolated and translated without erasing successful prior results.
6. [x] **EGR-410: Add the versioned `ResolutionResult` contract and outcome invariants.** Represent ANSWER, EVIDENCE, or MISS with concrete selected-candidate presence, bounded response candidates and evidence references, confidence plus availability, execution and baseline reason codes, frame diagnostics, resolver diagnostics, and final budget consumption. This task defines the result contract only; §5 owns fused confidence, ambiguity, and general answer policy.
7. [x] **EGR-412: Establish side-effect-free retrieval primitives.** Separate discovery from query, candidacy, hit, success, feedback, session, and persistence mutation across the existing exact, pattern, lexical, structured graph, and support-semantic paths. Return explicit accounting observations for later centralized application and retain compatibility wrappers around the legacy APIs.
8. [x] **EGR-405: Adapt exact retrieval.** Return a candidate only for one current §3 exact owner that passes scope, required metadata, source, lifecycle, validity, and epoch checks; expose canonical-versus-alias provenance and bounded collision or exclusion diagnostics. Only this baseline result may short-circuit before §5 policy exists.
9. [x] **EGR-408: Disentangle and adapt existing structured graph recall.** Remove graph fallback from the pattern result shape before declaring the pattern adapter complete, preserve the existing read-only bounded graph behavior, and emit typed evidence references instead of a statementless pattern response. Canonical entity/predicate resolution, one-hop planning, temporal policy, and response-less evidence expansion remain owned by §§8, 9, and 7.
10. [x] **EGR-406: Adapt pure pattern retrieval.** Emit pattern-backed candidates with statement identity, captures, match specificity, topic and `that` provenance, and bounded diagnostics without invoking graph fallback or mutating accounting during discovery.
11. [x] **EGR-407: Adapt lexical retrieval.** Preserve existing keyword, lemma, stem, synonym, spelling, phrase, recency, hit-rate, and priority diagnostics in normalized features without adding the sparse enhancements owned by §12 or mutating accounting during discovery.
12. [x] **EGR-409: Adapt support-aware semantic graph recall.** Preserve the fixed internal vector search, exact scope, active Claim filtering, support-to-artifact intersection, bounded scan behavior, and separate lexical and semantic features. Emit only support-linked accepted-response candidates in §4; response-less Claim evidence and standalone dense retrieval remain owned by §§7 and 13.
13. [x] **EGR-413: Add the deterministic resolver registry and execution plan.** Select only configured resolvers, record availability and skip decisions, preserve the default cost-aware runtime order, keep exact first, and produce a bounded inspectable plan that later optional resolvers can extend without changing the core protocol.
14. [x] **EGR-414: Implement the budgeted resolver executor.** Enforce total, per-resolver, result, graph, vector, evidence, output, diagnostic, memory-estimate, and cost-class allowances with an injected monotonic clock, deterministic truncation, cooperative deadline checks, and typed exhaustion. Preserve completed results when another resolver is unavailable, skipped, exhausted, or failed.
15. [x] **EGR-415: Centralize resolution accounting and finalization.** Record each unique proposed statement's candidacy once after resolver aggregation, record no speculative success, apply an accepted success exactly once through the appropriate §3 or legacy accounting boundary, preserve regulated retry behavior, and prevent duplicate credit when multiple resolvers return one artifact.
16. [x] **EGR-416: Add conservative baseline orchestration.** Build one frame and plan, execute resolvers under the shared budget, allow only a unique eligible exact result to produce ANSWER, return bounded non-answer candidates or existing graph references as EVIDENCE, otherwise return MISS, and apply execution and accounting reason codes. Section 5 replaces this baseline selection rule with fusion, ambiguity, thresholds, and stable policy reasons.
17. [x] **EGR-417: Complete the Section 4 conformance gate.** Prove contract codecs and bounds, deterministic planning and results, exact short-circuiting, pattern/graph separation, resolver failure isolation, budget boundaries, deterministic truncation, side-effect-free discovery, exactly-once accounting, compatibility-wrapper behavior, and transport-neutral core operation; record focused and full-suite verification plus applicable latency and resource measurements.

**Ownership boundary:** Section 4 owns the base frame and construction boundary, generic candidate and minimal evidence-reference containers, resolver/result/budget contracts, pure adapters for currently implemented retrieval, deterministic planning and bounded execution, centralized proposal/success accounting, and conservative exact-only baseline orchestration. Section 5 owns feature semantics, normalization, deduplication, fusion, ambiguity, confidence calibration, thresholds, and policy reasons. Section 7 owns full response-less Claim contracts, fixed projections, current disclosure eligibility, unfitted usefulness, packaging, and core handoff beyond the minimal references already surfaced from legacy graph recall. Section 8 owns contextual frame enrichment and canonical relation-aware graph plans; §9 owns requested historical-time, trust-ranking, multi-value, and conflict semantics beyond Section 7's baseline. Sections 11-14 own optional resolver implementations, §15 owns adapter and operational integration, and §16 owns held-out calibration, value, and release gates.

**Evidence required:** deterministic concrete codecs and unsupported-version tests for every new contract; frame construction fixtures; resolver conformance and fake-resolver suites; adapter-independent core tests; hard-eligibility and exact-short-circuit tests; pattern/graph separation regressions; side-effect-free discovery and exactly-once accounting tests; injected-clock boundary, reservation, exhaustion, truncation, and output-bound tests; unavailable, skipped, exhausted, and failed resolver tests; compatibility-wrapper coverage; deterministic result fixtures; applicable latency, memory, and failure benchmarks; and a Section 4 conformance report.

**Completion evidence:**

- [Unified resolution contract v1](documentation/artifacts/resolution-contract-v1.md), implemented in `engram/resolution.py`, `engram/resolvers.py`, and `EngramCore.resolve_request`
- pure discovery and compatibility wrappers in `engram/core.py`, with score components in `engram/scoring.py`
- atomic accepted-response resolution accounting in `engram/responses.py` and `engram/mutations.py`
- contract and adapter conformance suites in `tests/test_resolution_contracts.py` and `tests/test_resolvers.py`
- [Section 4 conformance report](documentation/artifacts/section4-conformance-2026-08-12.md), including the requirement matrix and completion review remediations
- [Section 4 benchmark](documentation/artifacts/section4-benchmark-2026-08-12.json) and reproducible runner at `scripts/benchmark_resolution.py`
- [1,000-turn Section 4 MCP evidence](documentation/artifacts/section4-mcp-conversation-1000-turns-2026-08-12.json) through the official client/server path
- [incremental and final verification results](documentation/artifacts/test-results-2026-08-12.md)

All seventeen tasks meet the Section 4 exit condition after the 12 August conformance review and 13 August independent evaluation remediation. The remediation moved accepted-success accounting after final output-budget outcome selection, added per-resolver cooperative deadlines, post-invocation deadline rejection, and bounded local working sets, synchronized bounded retry retention, enforced complete outcome/codec invariants, preserved consumption availability, and separated raw semantic similarity from priority and the legacy combined score. The focused contract/resolver suite passed 60 tests, the complete repository passed 1,231 tests with no expected failures, Pyright reported no errors or warnings, and lint, formatting, compilation, dead-code, high-severity security, diff, latency, and resource gates passed. The 12 August 1,000-turn MCP regression remains applicable because unified resolution is still transport-neutral and is not yet exposed by the MCP adapter. Section 5 may now consume the generic candidates and execution substrate for fusion; Sections 7, 8, and 15 retain their explicit evidence, contextual-graph, and wire responsibilities.

## §5. Candidate fusion and ambiguity (10/10)

**Priority:** P1  
**Depends on:** §4. Empirical tuning and release evaluation depend on §16 and are not Section 5 completion work.\
**Entry:** Section 4 is complete; the initial policy is explicitly hand-authored and unfitted.\
**Exit:** Every response candidate is revalidated before grouping or scoring. Eligible independent evidence can reinforce one unchanged accepted response with fully inspectable contributions, while close distinct alternatives, identity or object-type mismatch, incomplete response support, stale authoritative state, candidate inconsistency, and explicit conflict signals prevent unsafe direct selection. One deterministic versioned policy produces bounded ANSWER, response-candidate EVIDENCE, or MISS results without weakening Section 3 eligibility, Section 4 budgets and accounting, or the evidence and graph boundaries owned by later sections.

Tasks are listed in implementation dependency order while retaining their stable EGR identifiers.

1. [x] **EGR-501: Define versioned feature semantics and roles.** Specify exact, pattern, lexical, semantic, entity, relation, object type, support, history, freshness, authority, agreement, and margin features. For each feature, document its producer and owning section, trust boundary, raw and normalized ranges, availability meaning, monotonic direction, combination rule, and role as a hard gate, scoring input, derived feature, or decision-only diagnostic. Use concrete zero values plus explicit availability rather than `null`; defining a future feature here does not implement the producer owned by §§6, 8-9, or 12-14.
2. [x] **EGR-507: Define the fusion policy, decision report, and stable reason contracts.** Add a closed content-free policy reason vocabulary plus versioned bounded contracts for policy configuration, candidate eligibility, fused decisions, and inspectable contribution reports. Provide deterministic dictionary and JSON codecs, policy fingerprinting, concrete absence behavior, unsupported-version rejection, and diagnostic size limits without logging request, response, or graph content.
3. [x] **EGR-504: Centralize current-state candidate eligibility.** Revalidate each candidate against authoritative Section 3 state before grouping or scoring; apply scope, required metadata and source, lifecycle, validity, epoch, accepted-response visibility, response equality, and available support-completeness checks. Distinguish score, response-evidence, and direct-answer eligibility, retain bounded reasons, and consume rather than duplicate Section 3 eligibility. Response-less Claim current disclosure eligibility remains in §7; requested historical-time, trust-ranking, multi-value, and conflict semantics remain in §9.
4. [x] **EGR-502: Normalize resolver-specific signals.** Transform the current exact, pattern, lexical, and support-semantic raw features into deterministic documented comparable inputs with golden boundary fixtures. Preserve raw provenance and availability, apply only source-appropriate monotonic transforms, and keep semantic similarity, configured vector weight, statement priority, and the legacy combined retrieval score distinct rather than treating unrelated scales as interchangeable.
5. [x] **EGR-503: Deduplicate eligible candidates and calculate independent agreement.** Group by current authoritative statement identity only after per-candidate hard filtering; define generation and same-statement response-consistency behavior, deterministic ordering, bounded evidence and diagnostic deduplication, and resolver-family independence so correlated signals cannot manufacture agreement. Retain every permitted raw and normalized contribution and turn inconsistent groups into explicit non-answer decisions.
6. [x] **EGR-505: Implement transparent first-generation fusion.** Apply the versioned configurable hand-authored formula to eligible deduplicated candidates, expose every normalized input, weight, contribution, aggregation choice, and final score, and use no hidden generative inference. Make results invariant to resolver return order and deterministic under equal inputs.
7. [x] **EGR-506: Add margin, mismatch, and ambiguity policy.** Rank distinct fused candidates deterministically, calculate top-one versus top-two margin only when available, and lower confidence or abstain on near-ties, identity or expected-type mismatch, incomplete required support, candidate inconsistency, or an explicit resolver-supplied conflict signal even when the leading absolute score is high. Semantic detection of contradictory graph Claims remains owned by §9.
8. [x] **EGR-509: Integrate fusion into unified resolution.** Replace Section 4's exact-only baseline selection rule in the transport-neutral orchestrator and evolve `ResolutionResult` invariants to permit a qualified fused non-exact ANSWER. Preserve the safe exact short circuit, deterministic ANSWER/response-candidate-EVIDENCE/MISS finalization, output and diagnostic fitting, typed truncation or exhaustion, resolver failure isolation, and exactly-once candidacy and accepted-success accounting.
9. [x] **EGR-508: Freeze the initial conservative policy.** Record the hand-authored coefficient, ANSWER threshold, response-candidate EVIDENCE threshold, minimum independent-source rule, support rule, and ambiguity margin with provenance, version, and fingerprint. Do not fit or select policy parameters on unit tests, conformance fixtures, or regression failures. Empirical calibration on independently labeled data, repeated-run analysis for nondeterministic components, and promotion of a tuned policy are owned by §16; response-less Claim usefulness thresholds remain owned by §7.
10. [x] **EGR-510: Complete the Section 5 conformance gate.** Prove feature and policy codecs, authoritative revalidation, normalization boundaries, order-invariant deduplication and fusion, deterministic ties, ambiguity abstention, output bounds, exact-short-circuit safety, resolver failure behavior, exactly-once accounting, and adversarial false-direct-answer abstention. Run focused and full-suite verification, static checks, and before/after p50, p95, memory, and deterministic acceptance measurements. Treat fixtures as conformance evidence only; do not tune the policy from their results. Section 16 owns independently labeled evaluation, statistical release gates, empirical calibration, final-test execution, and release approval.

**Ownership boundary:** Section 5 owns the common fusion-feature vocabulary, normalization of currently available resolver signals, accepted-response candidate revalidation, deduplication and independent agreement, the versioned transparent policy, candidate-level inconsistency and ambiguity handling, response-candidate EVIDENCE fallback, policy reasons, and transport-neutral orchestration integration. Section 3 remains authoritative for artifact lifecycle, validity, epoch, and current response state; §6 owns feedback-derived history producers; §7 owns response-less Claim contracts, current disclosure eligibility, initial unfitted usefulness, and packaging; §8 owns contextual entity, relation, and object-type producers; §9 owns requested historical-time, trust-ranking, multi-value, and contradiction semantics; §§12-14 own optional resolver feature producers. Section 15 owns adapter exposure, cross-feature configuration and startup wiring, telemetry, deployment, and rollback, while §16 owns evaluation partitions, empirical calibration, value and numerical release gates, final-test execution, and release approval.

**Evidence required:** versioned feature and policy specifications; documented provenance for the conservative unfitted policy; deterministic codec and fingerprint fixtures; source-normalization golden cases; candidate-eligibility truth tables; group-consistency, resolver-independence, order-invariance, and tie properties; bounded explainability fixtures; orchestration, output-budget, failure-isolation, and exactly-once-accounting tests; adversarial false-direct-answer abstention results used only as conformance evidence; focused, full-suite, and static verification; and before/after latency, memory, and deterministic acceptance artifacts. Independently labeled partitions, repeated-run statistical analysis, calibration, numerical release gates, and release comparison artifacts remain required in §16 and must not be learned from these fixtures.

All ten tasks meet the Section 5 exit condition after the 15 August conformance review and working-memory remediation. The focused fusion, resolution-contract, orchestration, and concrete-absence suite passed 94 tests; the complete repository passed 1,266 tests with no expected failures. Fusion now receives only the working-memory allowance remaining after resolver execution, emits a deterministic conservative working-set estimate, and contributes it to complete `BudgetConsumption`; exhaustion is typed before unsafe selection. Ruff, isolated-cache Black, compilation, Vulture, high-severity Bandit, Pyright, and diff checks passed. The refreshed deterministic benchmark passed all six latency, measured-memory, estimated-memory, and acceptance gates, and the official MCP client completed 1,000/1,000 conversation turns and 1,003 protocol calls. The initial policy remains explicitly unfitted: no coefficient, threshold, source rule, support rule, or margin was learned from unit or conformance fixtures. The complete evidence is recorded in `documentation/fusion/section5-conformance-2026-08-15.md`; independent empirical calibration and release evaluation remain in Section 16.

## §6. Feedback learning and negative resolution (15/15)

**Priority:** P1\
**Depends on:** §§3-5. Section 15 consumes the resulting feature-owned codecs, inspection contracts, and core operations; empirical coefficient or threshold selection depends on §16 and is not Section 6 completion work.\
**Entry:** Sections 3 through 5 are complete. Section 4 supplies authoritative unique candidacy and accepted-success finalization, Section 3 supplies expected-generation lifecycle and durable receipt boundaries, and Section 5 supplies the versioned `HISTORY` feature contract and fusion policy fingerprint.\
**Exit:** A typed external Regulator observation targets one immutable candidate generation and version partition, exact retries apply it once, and acceptance, quality, context, stale, and policy outcomes affect only their documented statement or relationship scope. Bounded aged feedback becomes available to fusion only after its sample floor. A repeated request may bypass expensive resolver work only after the same fully completed knowledge miss under the same exact identity, scope, constraints, available knowledge epoch, resolver plan, and policy state; negative records never become statements, answers, evidence, or durable knowledge.

Section 6 has two contract-first implementation tracks. EGR-601 through EGR-610 implement feedback learning; EGR-611 through EGR-613 implement negative resolution. After the common §§3-5 entry condition, the tracks may advance independently, then converge on bounded inspection and the conformance gate.

1. [x] **EGR-601: Define versioned feedback contracts and keys.** Add bounded concrete-absence contracts for feedback targets, candidacy and verdict observations, feedback receipts, statement-generation keys, and scoped query-relationship keys. A feedback target carries the authoritative request or proposal reference, query identity and normalization version, exact `ScopeKey`, a canonical bounded constraint fingerprint, statement ID and generation when applicable, observation kind, typed external outcome, policy and contract fingerprints, and injected observation time.
2. [x] **EGR-602: Establish exactly-once feedback ingestion.** Create one transport-neutral owner that consumes Section 4's unique candidacy set and explicit external Regulator verdicts, issues durable replayable receipts, rejects conflicting retries, and routes the existing regulated proposal compatibility path through the same semantics. Define which outcomes require one candidate generation and which may target a complete proposal or exact policy scope. Do not treat Engram's own fused selection as a Regulator acceptance label or double-credit existing candidacy and success accounting. Support the declared 1,000-observation batch with a bounded streaming business-payload signature rather than inheriting the generic receipt codec's nested-item ceiling.
3. [x] **EGR-603: Add statement-generation feedback aggregates.** Persist candidate, acceptance, and each typed rejection count for one statement generation under the applicable feedback contract and policy partition. Do not merge equivalent response text, replacement generations, or separately owned statements, and keep legacy retrieval hits distinct from typed Regulator acceptance.
4. [x] **EGR-604: Add bounded scoped query-relationship aggregates.** Track the typed query identity, exact `ScopeKey`, canonical constraint fingerprint, statement generation, and version fingerprints without duplicating namespace or context outside `ScopeKey`. Bound key and record cardinality, avoid raw request text in operational labels, and prove that an observation cannot weaken another query, context, namespace, generation, or policy partition.
5. [x] **EGR-605: Add feedback persistence, migration, and recovery.** Supply deterministic feature-owned codecs, unsupported-version rejection, bounded storage, checkpoint/load integration, restart-safe receipts, and recovery behavior for feedback state. Definite durable before-state recovery remains healthy and retryable, durable after-state recovery publishes, divergence degrades, and replay reports durability only when the owner is actually checkpointed or clean. Define whether existing artifact `query_count` and `hit_count` seed an explicitly identified legacy partition or leave typed feedback unavailable; never silently reinterpret them as Regulator labels. Section 15 later owns cross-feature schema orchestration, backup, downgrade, and operator workflow.
6. [x] **EGR-606: Apply acceptance and quality semantics.** Acceptance strengthens only the targeted scoped relationship and statement-generation reliability history; `rejected_quality` weakens the targeted statement generation across its permitted relationships while remaining partitioned by the applicable ownership, contract, and policy boundary. Use bounded deterministic effects rather than directly changing unrelated artifact fields.
7. [x] **EGR-607: Apply context and policy semantics.** `rejected_context` weakens only the observed query, exact scope, constraints, and statement-generation relationship. `rejected_policy` suppresses only the applicable namespace and policy partition. Neither outcome asserts global falsehood, retires an artifact, or leaks across scopes or policy versions.
8. [x] **EGR-608: Add stale-feedback lifecycle handoff.** Make the rejected statement generation immediately ineligible through a generation-scoped feedback exclusion and request any authoritative lifecycle transition through Section 3's authorized, expected-generation, idempotent mutation boundary. Preserve the feedback receipt and a typed pending, completed, conflicted, or failed lifecycle-action result; do not add a generic lifecycle setter.
9. [x] **EGR-609: Add deterministic aging and retention.** Retain inspectable aggregate counters and bounded time buckets rather than an unbounded event log; use injected UTC time, deterministic decay and compaction, explicit sample windows, capacity limits, and overflow behavior. Old traffic and repeated reads must not create permanent standing.
10. [x] **EGR-610: Produce feedback-derived fusion history.** Derive the feedback contribution to Section 5's `HISTORY` input from the applicable aged statement and relationship partitions using a hand-authored versioned formula, minimum sample floor, bounded priors, explicit availability, and inspectable provenance. Define its deterministic combination with existing Section 3 accepted-use statistics without reinterpreting those statistics as Regulator labels. Integrate the producer without weakening Section 5 eligibility or ambiguity gates and without fitting coefficients, priors, floors, or thresholds on unit or conformance fixtures; empirical selection remains §16 work.
11. [x] **EGR-611: Define negative-resolution contracts and conservative admission.** Add bounded concrete-absence `NegativeResolutionKey` and `NegativeResolution` contracts. Key by typed query identity, exact `ScopeKey`, canonical constraint fingerprint, available knowledge epoch, and normalization, resolver-plan, capability-readiness, and policy fingerprints; keep the typed miss reason and expiry as record values. Admit only `insufficient_knowledge` after the configured resolver plan completed sufficiently to establish a knowledge miss with every required authoritative dependency and knowledge epoch available. Until every mutable knowledge source shares authoritative versioning, restrict version-1 reuse to exact-only configured plans. Exclude policy, safety, authorization, transport, dependency-unavailable, timeout, budget-exhausted, truncated, and indeterminate failures.
12. [x] **EGR-612: Implement the bounded negative-resolution owner.** Use a memory-only first-generation store with atomic lookup, insertion, expiry, and deterministic capacity eviction; fixed non-sliding TTL; exact-key isolation; and invalidation on relevant knowledge-epoch, normalization, resolver-plan, capability-readiness, or policy change. An unavailable knowledge epoch cannot admit or reuse a negative record, and a non-exact plan invalidates related records before ordinary resolution so independently added lexical or graph knowledge cannot be hidden. Persistence requires later benchmark and migration justification rather than being implicit.
13. [x] **EGR-613: Integrate negative lookup into unified orchestration.** Validate and build the base frame, capture the exact identity, scope, constraints, eligibility time, and knowledge state, then consult the negative owner before expensive resolver execution. A hit returns a bounded typed MISS with a stable negative-hit reason and accurate time, output, working-memory, and diagnostic consumption without applying candidacy or success accounting. Define concurrent identical misses and insertions, retry-cache interaction, expiry races, and fail-open behavior so the negative owner can never block the ordinary Actor or Tapestry path.
14. [x] **EGR-614: Add bounded Section 6 inspection.** Expose transport-neutral snapshots of feedback partitions, receipt results, decay and retention state, negative occupancy, admission, hit, miss, expiry, eviction, and invalidation counts with bounded identifiers and omission counts. Adapter exposure, authorization, redaction, dashboards, and low-cardinality production metrics remain owned by EGR-1502 through EGR-1504, EGR-1507, and EGR-1509.
15. [x] **EGR-615: Complete the Section 6 conformance gate.** Prove contract codecs and concrete absence, the full 1,000-observation batch, outcome targeting, exact and conflicting retry behavior, restart and definite-before/after/divergent checkpoint recovery, replay durability, legacy-statistics migration, concurrent ingestion, cross-scope, generation, and policy isolation, stale lifecycle handoff, deterministic aging and bounds, feature sample floors, negative admission and exclusion matrices, epoch-unavailable abstention, TTL and capacity behavior, non-exact knowledge safety, policy and resolver-plan invalidation, complete negative-hit budgets, orchestration accounting, and fail-open behavior. Run focused and full-suite verification, static checks, and before/after latency, memory, persistence, repeated-miss, and representative 5,000-record preparation/recovery measurements; treat fixtures as conformance evidence only and leave independently labeled calibration and release gates to §16.

All fifteen tasks meet the Section 6 exit condition after the 16 August conformance and findings reviews. Typed feedback now uses durable exact-once receipts, a declared 1,000-observation bounded signature, strict generation/relationship/version partitions, bounded aging, authorized stale handoff, accurate recovery/replay durability, and a sample-gated `HISTORY` producer; negative resolution is memory-only, exact-keyed, conservative, fixed-TTL, epoch-aware, exact-plan-only, fully budgeted, and fail-open. The focused feedback, concrete-absence, service, and fusion suite passed 86 tests, and the complete repository passed 1,292 tests with no expected failures. Ruff, isolated-cache Black, compilation, Vulture, high-severity Bandit, Pyright, JSON validation, and diff checks passed. The reproducible 100-sample/100-record plus 5,000-record scale benchmark passed all nine ingestion, history, persistence, repeated-miss, memory, preparation, recovery, and state-size gates; negative-hit p95 was 2.3146 ms versus 6.6940 ms for an ordinary completed exact miss, while the 5,001-record round trip completed in 2,612.3414 ms. The official MCP client completed and evaluated 1,000/1,000 conversation turns and 1,003 protocol calls. The hand-authored formula and admission policy remain explicitly unfitted. Complete evidence is recorded in `documentation/feedback/section6-conformance-2026-08-16.md`; adapter exposure and independent release evaluation remain in Sections 15 and 16.

**Ownership boundary:** Section 6 owns feedback targets and receipts, statement-generation and scoped relationship aggregates, their feature-owned persistence and migration contracts, deterministic aging, typed feedback effects, the feedback-derived `HISTORY` producer, the memory-only negative-resolution core, and bounded transport-neutral inspection. Section 3 remains authoritative for artifact generations, lifecycle, epoch mutation, durable accepted-response receipts, and expected-generation lifecycle operations. Section 4 remains authoritative for unique candidacy and accepted-success finalization; Section 5 owns the common feature vocabulary, fusion, eligibility, ambiguity, thresholds, and policy decisions. Section 15 owns adapter exposure, authorization, cross-feature schema and startup orchestration, operational telemetry, backup, downgrade, deployment, and rollback. Section 16 owns labeled partitions, empirical calibration, numerical release gates, final-test execution, and release approval.

**Evidence required:** versioned feedback, receipt, aggregate, and negative-resolution specifications; deterministic codec and unsupported-version fixtures; outcome-target and application truth tables; exact/conflicting retry, concurrent update, checkpoint degradation, restart, migration, and recovery tests; statement-generation, query, context, namespace, constraint, policy, and contract isolation tests; injected-clock decay, compaction, capacity, and sample-floor tests; stale lifecycle handoff tests; negative admission/exclusion, epoch-unavailable, expiry, capacity, invalidation, plan-change, concurrency, accounting, and fail-open tests; bounded inspection and cardinality review; focused, full-suite, and static verification; and before/after latency, memory, persistence, and repeated-miss benchmark artifacts. Adapter parity and operational dashboards remain §15 evidence; independently labeled calibration and release evaluation remain §16 evidence.

## §7. Response-less Claim evidence and Tapestry package (0/12)

**Priority:** P1  
**Depends on:** §§4-5. Section 8 consumes the completed package for relation-aware fallback; Section 15 owns Python, MCP, and gRPC exposure and authorization; Section 16 owns independently labeled usefulness, avoided-work, and release gates.
**Exit:** A response-cache miss can produce a versioned, bounded, currently disclosure-eligible Claim package through the transport-neutral core without authorizing an Engram answer. Adapter release and independently measured Tapestry value remain Sections 15 and 16 work.

Section 7 owns the strict full-Claim evidence and package contracts, fixed read-only Claim projections, current-time disclosure eligibility, response-less structured and semantic producers, deterministic evidence normalization, the initial unfitted usefulness policy, bounded orchestration, and core handoff semantics. Section 9 extends this baseline with requested historical time, trust ranking, multi-value, and contradiction policy. Section 10 populates multi-hop paths. Section 15 implements adapter translation, authorization, redaction, cancellation, and deployment; Section 16 calibrates numerical thresholds and decides release value.

1. [ ] **EGR-701: Resolve evidence contract versioning and compatibility.** Reconcile ADR 0003's full evidence-wire decision with the existing strict schema-version-1 `EvidenceReference`, `ResolverResult`, and `ResolutionResult` codecs. Decide whether a new `ClaimEvidenceRecord` and `EvidencePackage` require a result-version increment or a separately versioned nested contract, specify old/new decoding and omission behavior, and publish this transport-neutral compatibility decision before changing a resolver.
2. [ ] **EGR-702: Implement the strict full-Claim evidence record.** Add deterministic concrete-absence codecs for stable Claim ID, source resolver, Section 5 `FeatureSet`, permissible canonical entity and predicate references, current validity and supplied trust inputs with explicit availability, exact disclosure scope or visibility decision provenance, a singleton Claim path, and bounded stable selection reasons. Exclude raw Claim, Passage, proof, credential, Cypher, embedding, and arbitrary graph-property content.
3. [ ] **EGR-703: Implement the bounded evidence package contract.** Add a wire version, canonical ordering, Claim-ID deduplication, explicit retained and omitted counts, truncation flags and reasons, at most 10 evidence records, at most 64 KiB complete serialized size, 256-byte identifiers, and at most 16 selection reasons per record. The path schema may retain ADR 0003's two-hop ceiling for forward compatibility, but Section 7 emits only singleton paths; Section 10 owns multi-hop population and validation.
4. [ ] **EGR-704: Add the trusted fixed Claim-projection boundary.** Extend the allow-listed read-only structured and vector queries to project only the fields required by EGR-702: stable and canonical IDs, current lifecycle/system state, valid-time inputs, ownership category, supplied trust fields and versions, and structured or similarity measurements. Strictly decode and bound graph rows, reject malformed or conflicting projections, preserve fixed query/index identifiers, and expose no caller-supplied Cypher or arbitrary properties.
5. [ ] **EGR-705: Implement current disclosure eligibility and revalidation.** Evaluate every projected Claim at the frame's one injected time; require active and system-current state, current valid-time inclusion, proof-canonical subject/predicate/object identity, and retrieval-only exclusion. Permit explicitly public Claims under the documented public rule; permit company/customer Claims only when a configured trusted visibility authority maps the caller's exact `ScopeKey`; otherwise exclude them rather than deriving authorization from namespace or context text. Preserve missing trust as unavailable, and revalidate eligibility immediately before package publication. Historical/as-of selection, trust ranking, and conflicts remain Section 9 work.
6. [ ] **EGR-706: Upgrade structured response-less Claim discovery.** Replace or enrich the existing minimal structured-graph references with strict EGR-702 records produced from the EGR-704 projection, without phrasing graph text or publishing an ineligible row. Preserve deterministic identifiers, side-effect-free discovery, graph-row and working-memory bounds, cooperative deadlines, typed failures, and the legacy minimal-reference compatibility path selected by EGR-701.
7. [ ] **EGR-707: Add semantic response-less Claim discovery.** Use the existing fixed Claim-vector search to emit strict eligible Claim evidence even when no accepted response references a hit. Keep the existing support-to-artifact intersection and accepted-response candidate behavior unchanged, retain raw similarity as an explicit feature, enforce vector/result/memory/deadline bounds, and fail soft on model, index, dimension, or graph unavailability.
8. [ ] **EGR-708: Canonicalize and merge response-less evidence.** Deduplicate the same Claim across structured and semantic producers, retain bounded source contributions and feature availability, reject incompatible identity or canonical-reference projections instead of choosing by resolver order, produce deterministic ordering and stable reasons, and apply no response candidacy or accepted-success accounting to Claim-only evidence.
9. [ ] **EGR-709: Define the initial evidence-usefulness policy.** Specify one versioned, inspectable, hand-authored policy for structured match, semantic similarity, supplied trust availability/value, canonical completeness, and source agreement. Supplied trust may enforce only a configured evidence-inclusion floor here; relative trust ranking and direct-answer authority remain Section 9 work. Distinguish unavailable inputs from measured zero, freeze conservative floors before conformance measurements, return stable inclusion/exclusion reasons, and do not fit coefficients or thresholds on unit, conformance, benchmark, or failure fixtures; empirical calibration remains Section 16 work.
10. [ ] **EGR-710: Integrate bounded EVIDENCE/MISS orchestration.** Preserve ANSWER priority and direct-answer gates; only emit response-less EVIDENCE after EGR-705 and EGR-709 pass, otherwise retain response-candidate EVIDENCE or MISS as appropriate. Fit the final package and complete `ResolutionResult` to Claim-count, evidence-byte, output, diagnostic, working-memory, graph-row, vector-result, and time budgets with deterministic truncation, accurate aggregate consumption, fail-soft dependency behavior, and no response accounting.
11. [ ] **EGR-711: Freeze the transport-neutral handoff and adapter mapping.** Version the unified core result and document how Section 15 must translate its concrete fields, absence values, truncation, errors, and compatibility behavior for Python, MCP, and gRPC. Complete the additive-versus-versioned protocol review required by ADR 0003, but do not implement adapter methods, generated stubs, authorization, or deployment in Section 7.
12. [ ] **EGR-712: Complete the Section 7 conformance gate.** Prove contract codecs and concrete absence, old/new compatibility, strict malformed-row rejection, public and trusted-scope disclosure matrices, current-time boundaries, structured and semantic response-less discovery, support-candidate non-regression, deduplication and projection conflicts, unfitted usefulness reasons, no unregulated answer, exact accounting, count/byte/output/memory/deadline truncation, graph/model/index failure paths, payload-content exclusion, deterministic ordering, and bounded inspection. Run focused and full-suite verification, static and security checks, and representative latency, memory, package-size, and partial-failure benchmarks. Independently labeled usefulness, avoided Tapestry calls/tokens, adapter outage, and release approval remain Section 16 evidence.

**Evidence required:** accepted compatibility decision and versioned full-record/package specifications; deterministic codec, unsupported-version, concrete-absence, malformed-row, and projection-conflict fixtures; disclosure/current-eligibility truth tables; structured and semantic producer tests; support-candidate compatibility tests; canonicalization, policy, accounting, truncation, timeout, dependency-failure, and content-leakage tests; bounded inspection and cardinality review; focused, full-suite, static, and security verification; and reproducible engineering benchmark artifacts. MCP/gRPC parity, generated stubs, authorization, and deployment remain §15 evidence; held-out usefulness, avoided-work, chaos, calibration, and release gates remain §16 evidence.

## §8. Contextual query-frame enrichment and relation-aware graph lookup (0/10)

**Priority:** P2  
**Depends on:** §§4-5, 7.
**Exit:** Engram can interpret representative direct relation questions and bounded elliptical follow-ups, perform parameterized one-hop canonical Claim lookup, and abstain when identity is uncertain.

1. [ ] **EGR-801: Persist a compact previous query frame in user context.** Retain operator, subjects, relation, expected type, qualifiers, source turn, and confidence under existing user isolation and TTL rules.
2. [ ] **EGR-802: Implement bounded follow-up inheritance.** Fill only missing fields, record inherited provenance, honor topic continuity and turn distance, and avoid contaminating self-contained requests.
3. [ ] **EGR-803: Extend operator classification for query frames.** Reuse the §1 operator contract and classifier, adding contextual inheritance and confidence needed by query frames without creating a second incompatible vocabulary; cover who, what, where, when, which, how many, lookup, exists, count, compare, and unknown.
4. [ ] **EGR-804: Resolve canonical subject entities.** Use labels, aliases, edge surface forms, named-entity evidence, and explicit ambiguity results.
5. [ ] **EGR-805: Extract predicate candidates.** Combine dependency structure, verb lemmas, prepositions, canonical Predicate labels, synonyms, and explicit caller identity.
6. [ ] **EGR-806: Infer expected object type.** Support PERSON, PLACE, DATE, NUMBER, BOOLEAN, ENTITY, and UNKNOWN without rejecting valid unknown types prematurely.
7. [ ] **EGR-807: Compile internal one-hop query plans.** Use parameterized, allow-listed templates; do not accept caller-supplied Cypher or procedure names.
8. [ ] **EGR-808: Execute and filter one-hop Claim lookup.** Reuse Section 7's fixed projection and current disclosure-eligibility boundary, then enforce expected type, relation-plan constraints, temporal defaults, row limits, and timeouts without creating a second Claim codec or visibility policy.
9. [ ] **EGR-809: Integrate graph phrasing and evidence.** Phrase only one unambiguous eligible result; otherwise populate the Section 7 evidence record and package with relation and ambiguity features and reasons.
10. [ ] **EGR-810: Build the relation and follow-up benchmark.** Include entity ambiguity, predicate ambiguity, paraphrases, technical relations, explicit references, elliptical replacement, and self-contained context-reset cases.

**Evidence required:** graph query-plan fixtures, read-only enforcement tests, user-isolation and inheritance tests, held-out relation results, and one-hop latency measurements.

## §9. Temporal, trust, and conflict semantics (0/9)

**Priority:** P2  
**Depends on:** §§7-8.  
**Exit:** Engram does not present historical knowledge as current, respects visibility and supplied trust, and turns contradictory eligible Claims into evidence or abstention.

1. [ ] **EGR-901: Define temporal query qualifiers.** Represent current, now, as-of, in-year, before, after, between, and latest separately from lexical terms.
2. [ ] **EGR-902: Implement conservative temporal parsing.** Normalize dates and years, preserve unresolved expressions, and expose parse confidence and source text.
3. [ ] **EGR-903: Extend valid-time and system-time filtering.** Reuse Section 7's current-time eligibility decision, follow the graph schema's half-open intervals, and add requested historical valid-time and system-time interpretation without weakening the baseline disclosure gate.
4. [ ] **EGR-904: Implement current, historical, bounded, and latest query policies.** Define deterministic selection and abstention for missing, overlapping, open, or incomparable bounds.
5. [ ] **EGR-905: Extend visibility and consume trust metadata.** Reuse Section 7's public-or-trusted-scope disclosure decision, add only the visibility semantics needed by relation-aware temporal queries, and treat supplied trust as a policy feature for ranking or direct answers; do not assign, upgrade, or silently default canonical graph values.
6. [ ] **EGR-906: Define contradiction versus multi-valued relation semantics.** Use predicate cardinality or policy metadata where available and fail conservatively when it is unknown.
7. [ ] **EGR-907: Detect incompatible active Claim objects.** Compare canonical subject, predicate, object, temporal, and scope frames while preserving every conflicting Claim ID.
8. [ ] **EGR-908: Suppress arbitrary direct answers on conflict.** Return bounded conflict evidence with stable reason codes and all ranking inputs permitted by policy.
9. [ ] **EGR-909: Add the temporal and conflict adversarial suite.** Cover current/historical swaps, boundary instants, latest ties, system-time differences, missing trust, visibility exclusion, true conflict, and valid multi-value cases.

**Evidence required:** temporal truth tables, graph fixtures, conflict policy review, held-out safety results, and direct-answer suppression tests.

## §10. Bounded graph composition (0/8)

**Priority:** P2  
**Depends on:** §§8-9 and successful one-hop release gate.  
**Exit:** Selected one-hop and two-hop structured questions resolve deterministically within hard limits and always expose the supporting Claim path.

1. [ ] **EGR-1001: Define the graph algebra and plan schema.** Cover LOOKUP, EXISTS, COUNT, AND, OR, NOT, MIN, MAX, ORDER, typed inputs, intermediate bindings, and terminal output.
2. [ ] **EGR-1002: Implement the bounded internal plan compiler.** Compile supported query frames to parameterized allow-listed plans and reject unsupported or underconstrained forms.
3. [ ] **EGR-1003: Enforce composition limits.** Apply `max_hops`, `max_rows`, `max_branches`, per-step candidates, path size, serialized output, and timeout before and during execution.
4. [ ] **EGR-1004: Implement deterministic one-hop and two-hop traversal.** Preserve canonical bindings, temporal and visibility eligibility, deduplication, and no cartesian expansion.
5. [ ] **EGR-1005: Implement safe Boolean and aggregate semantics.** Refuse answers when truncation, unknown completeness, duplicate cardinality, or missing type information could change the result.
6. [ ] **EGR-1006: Populate multi-hop Claim paths as evidence.** Fill and validate the Section 7 package's bounded path field with ordered Claims, bindings, operators, filters, and aggregation inputs without exposing arbitrary graph content; Section 7 itself emits only singleton paths.
7. [ ] **EGR-1007: Add planner rejection and resource tests.** Cover cycles, unconstrained predicates, high fan-out, timeouts, branch explosion, partial failures, invalid paths, and cancellation.
8. [ ] **EGR-1008: Pass the composition value gate.** Demonstrate held-out accuracy, zero unbounded execution, bounded p95 latency, and useful evidence when direct composition abstains.

**Evidence required:** algebra specification, plan fixtures, resource-limit tests, two-hop held-out results, graph-path audit examples, and performance profile.

## §11. Symbolic retrieval rewrite layer (0/7)

**Priority:** P3  
**Depends on:** §§1, 4-5.  
**Exit:** Independently authored, deterministic retrieval rewrites increase exact or downstream recall without becoming executable response patterns or hiding the original query.

1. [ ] **EGR-1101: Define the rewrite rule schema.** Include rule ID, version, input constraints, output template, priority, scope, maximum applications, and provenance.
2. [ ] **EGR-1102: Implement the bounded rewrite engine.** Enforce depth, cycle, expansion, output-size, and time limits with deterministic order.
3. [ ] **EGR-1103: Record rewrite chains in `QueryFrame`.** Preserve original text, every applied rule, intermediate form, and final retrieval representation for diagnostics.
4. [ ] **EGR-1104: Separate rewrites from AIML redirects.** Rewritten requests continue through all eligible resolvers and never select response templates by themselves.
5. [ ] **EGR-1105: Author the initial reduction corpus.** Cover contractions, question normalization, paraphrase reduction, pronoun transformations, synonym classes, conversational repair, and technical phrasing without copying historical ALICE code or response content.
6. [ ] **EGR-1106: Add corpus lint and regression tooling.** Detect cycles, unreachable rules, collisions, overbroad rewrites, duplicate outputs, and unexpected identity loss.
7. [ ] **EGR-1107: Pass the rewrite promotion gate.** Improve held-out exact or downstream recall without increasing semantic-collision or false-direct-answer rates beyond the approved gate.

**Evidence required:** rule corpus provenance, lint output, trace fixtures, licensing review note, held-out comparison, and latency impact.

## §12. Sparse retrieval enhancement (0/8)

**Priority:** P3  
**Depends on:** §§1-5.  
**Exit:** A selected sparse implementation improves technical and long-tail retrieval at acceptable startup, mutation, memory, and latency cost while remaining rebuildable from authoritative state.

1. [ ] **EGR-1201: Define fielded sparse documents.** Specify canonical request, aliases, entities, relation, keywords, technical identifiers, and optional response-text fields with explicit weights.
2. [ ] **EGR-1202: Build a representative sparse benchmark.** Include phrases, proximity, prefixes, character n-grams, symbols, version strings, paths, error codes, and identifier boundary cases.
3. [ ] **EGR-1203: Evaluate candidate engines.** Compare existing IDF overlap, BM25, SQLite FTS5, or another approved local CPU option on relevance, operations, licensing, and portability.
4. [ ] **EGR-1204: Implement the selected rebuildable index.** Keep persisted response artifacts authoritative and support atomic rebuild and compatibility checks.
5. [ ] **EGR-1205: Implement technical tokenization and field scoring.** Preserve meaningful punctuation and components without allowing identifier noise to dominate general language.
6. [ ] **EGR-1206: Integrate sparse candidates and diagnostics.** Expose field contributions, phrase/proximity matches, normalized score, and resolver budget through the common model.
7. [ ] **EGR-1207: Add mutation, recovery, and scale tests.** Cover commit, supersede, lifecycle, eviction, corrupt index, rebuild, large STATIC corpus, and concurrent readers.
8. [ ] **EGR-1208: Pass the sparse promotion gate.** Demonstrate held-out recall or precision value and approved p50, p95, memory, disk, startup, and write-amplification results.

**Evidence required:** engine decision record, benchmark corpus, relevance comparison, operational profile, index recovery tests, and license inventory.

## §13. Standalone semantic retrieval and reranking (0/9)

**Priority:** P3  
**Depends on:** §§5 and 12.  
**Exit:** Optional local semantic retrieval or reranking is promoted only when it improves held-out inference avoidance or evidence usefulness after all resource and safety costs are included.

1. [ ] **EGR-1301: Define standalone embedding records.** Embed canonical requests and aliases, retain model and normalization versions, and do not use accepted response prose as the primary intent vector.
2. [ ] **EGR-1302: Define model artifact policy.** Require approved licensing, pre-provisioned local files, checksums, dimensions, offline startup, and explicit unavailable behavior; prohibit runtime model downloads.
3. [ ] **EGR-1303: Implement optional CPU embedding and index lifecycle.** Cover build, incremental update, supersession, invalidation, retirement, rebuild, dimension mismatch, and fail-soft operation.
4. [ ] **EGR-1304: Integrate standalone semantic candidates.** Run after cheaper resolvers, expose alias provenance and semantic score, obey budget, and never bypass shared eligibility or fusion.
5. [ ] **EGR-1305: Benchmark execution variants.** Compare supported native, ONNX, and quantized paths where available for recall, numerical drift, cold start, p95, throughput, memory, and artifact size.
6. [ ] **EGR-1306: Define the reranker shortlist contract.** Bound input candidates, input length, model time, output features, cancellation, and fallback to pre-rerank order.
7. [ ] **EGR-1307: Evaluate lightweight rerankers.** Compare transparent logistic regression or learning-to-rank with any approved small pairwise encoder using disjoint train, calibration, and release sets.
8. [ ] **EGR-1308: Add model version, health, and rollback controls.** Expose loaded artifact identity, readiness, incompatibility, metrics, and a configuration-only rollback path.
9. [ ] **EGR-1309: Pass independent semantic and reranker gates.** Promote each feature separately only when value exceeds approved false-answer, latency, memory, cold-start, and operating-complexity thresholds.

**Evidence required:** model cards and licenses, checksums, offline-start tests, benchmark artifacts, held-out comparisons, health fixtures, and rollback exercise.

## §14. Utility resolver plugins (0/6)

**Priority:** P3  
**Depends on:** §§4-5.  
**Exit:** Each promoted plugin resolves a declared deterministic input class within hard computational and formatting limits and cannot execute arbitrary code.

1. [ ] **EGR-1401: Define the utility plugin contract.** Declare name, version, accepted frame types, input schema, bounds, deterministic result, evidence, errors, and health.
2. [ ] **EGR-1402: Implement the allow-listed registry and sandbox boundary.** Load only configured built-in plugins, reject arbitrary imports or expressions, and isolate plugin failures.
3. [ ] **EGR-1403: Implement arithmetic, Boolean, and set candidates.** Define numeric domains, precision, overflow, collection bounds, and canonical formatting.
4. [ ] **EGR-1404: Implement date, time, and unit-conversion candidates.** Define timezone, calendar, locale, dimensional-analysis, precision, and ambiguity policies.
5. [ ] **EGR-1405: Implement version and identifier candidates.** Define supported version schemes and identifier grammars without treating arbitrary strings as executable expressions.
6. [ ] **EGR-1406: Gate each plugin independently.** Require conformance, property, fuzz, resource, ambiguity, integration, and held-out value tests before enabling it by default.

**Evidence required:** plugin contract, threat model, per-plugin conformance artifacts, fuzz results, resource-limit tests, and enablement decision.

## §15. Interfaces, migration, security, and operations (1/10)

**Priority:** Continuous  
**Depends on:** Each affected feature.  
**Exit:** Released capabilities have consistent core, Python, MCP, and gRPC behavior; safe persistence migration; bounded observability; and documented deployment, failure, and rollback procedures.

1. [ ] **EGR-1501: Keep the core transport-neutral.** Implement identity, commit, lifecycle, resolution, evidence, index, and feedback behavior once in `EngramCore` or lower layers; adapters only validate and translate.
2. [ ] **EGR-1502: Evolve the Python API compatibly.** Expose §1 identity inputs, the stabilized Section 7 evidence package, and precisely typed operations with concrete falsy absence values and no optional union annotations; preserve documented lower-level calls through wrappers with deprecation and behavior tests.
3. [ ] **EGR-1503: Evolve the MCP tools compatibly.** Validate and translate authoritative identity and the stabilized Section 7 evidence package at the boundary while preserving persistent process ownership, proposal accounting, retry identity, lifecycle, bounded transient state, authorization, redaction, and tool documentation.
4. [ ] **EGR-1504: Evolve and regenerate gRPC contracts.** Translate the stabilized Section 7 evidence package and other authoritative fields additively in v1 only where omission is safe, otherwise use an explicit versioned result or service; regenerate pinned stubs and verify byte-for-byte reproducibility.
5. [ ] **EGR-1505: Implement cross-feature persistence schema management.** Integrate the feature-owned serializers and migrations supplied by §3 and later sections; record schema, normalization, index, policy, and model versions; and provide startup/readiness wiring, explicit migration output or backup behavior, quarantine reporting, and documented downgrade constraints. Artifact and mutation-receipt codecs, v1-to-v2 transformation rules, and startup derivation of response views and indexes remain owned by §3.
6. [ ] **EGR-1506: Preserve cross-adapter concurrency and idempotency.** Verify that Python, MCP, and gRPC preserve the core receipt and mutation semantics supplied by §3; test exact and conflicting retries, deadlines, cancellation, concurrent proposal resolution, and consistent visibility without reimplementing live-state or checkpoint coordination in an adapter.
7. [ ] **EGR-1507: Enforce security and privacy bounds.** Limit request, alias, metadata, support, evidence, diagnostic, and rewrite sizes; keep graph read-only; validate identifiers; separate mutation authority; redact sensitive logs.
8. [x] **EGR-1508: Eliminate runtime dependency acquisition.** Preflight NLTK data, spaCy models, embedding artifacts, graph indexes, and dimensions; make offline or fail-soft behavior explicit and prohibit runtime model downloads.
9. [ ] **EGR-1509: Add bounded operational telemetry.** Measure outcome, resolver contribution, rejection reasons, latency, budget exhaustion, rebuild, durability, and resource use without raw text or high-cardinality identifiers as labels.
10. [ ] **EGR-1510: Document deployment and rollback.** Cover standalone, graph-backed, and Tapestry-backed modes, health/readiness, startup rebuild, degraded dependencies, backups, lifecycle repair, feature flags, policy rollback, and incident response.

**EGR-1508 evidence:** `engram.nltk_data.ensure_resource` is check-only unless the setup caller explicitly supplies `download=True`; every serving path uses that offline default. `Engram.preflight_components` rejects missing NLTK resources before serving and reports NLTK readiness alongside the existing eager spaCy, local-only embedding-model, graph, vector-index, and dimension checks. Focused tests prove that request/runtime checks never call `nltk.download`, the explicit bootstrap still can, missing data blocks startup with a bounded readiness error, and readiness is exposed. The complete repository and static gates were rerun after this cross-cutting change. Dependency acquisition remains available only through the documented setup commands, not through imports, startup, or requests.

**Evidence required:** cross-adapter contract matrix, generated-stub check, persistence compatibility suite, concurrency suite, security review, offline-start test, telemetry cardinality review, and rollback exercise.

## §16. Evaluation, rollout, and release (0/10)

**Priority:** Continuous  
**Depends on:** §0 baseline and every promoted behavior.  
**Exit:** Each release is supported by reproducible held-out accuracy, inference-avoidance, evidence-usefulness, latency, memory, compatibility, failure, and rollback evidence.

1. [ ] **EGR-1601: Create versioned evaluation partitions.** Maintain disjoint tuning, release-gate, and final-test sets with independent labels and provenance. Unit tests, conformance fixtures, and regression cases may verify invariants but must not be used to fit or select policy parameters; prevent hidden leakage into authored rewrite rules or trained policies.
2. [ ] **EGR-1602: Cover the required workload families.** Include exact, alias, paraphrase, multi-turn, technical, graph, temporal, ambiguous, conflicting, stale, scope, policy, unsupported, negative, and dependency-failure cases.
3. [ ] **EGR-1603: Add adversarial near-collision gates.** Make `when`/`where`, current/historical, positive/negative, same terms under different relations, and same language under different scopes mandatory release cases.
4. [ ] **EGR-1604: Measure answer and evidence quality.** Track direct-answer acceptance, false direct answers, useful evidence, proposal acceptance, typed rejection distribution, later correction, and abstention quality. For nondeterministic components, use repeated trials, report dispersion or confidence intervals, and avoid treating individual failures as training examples without an explicit data-governance decision.
5. [ ] **EGR-1605: Measure avoided Tapestry work.** Track Actor bypass, inference calls, input and output tokens, Knowledge Engine retrieval calls, prompt evidence size, and end-to-end latency saved.
6. [ ] **EGR-1606: Measure component and resource performance.** Record p50 and p95 total and resolver latency, cold start, startup rebuild, persistence, graph recall, semantic recall, throughput, memory, disk, and CPU.
7. [ ] **EGR-1607: Add failure and chaos scenarios.** Exercise unavailable graph, missing model, bad index, dimension mismatch, persistence degradation, restart, timeout, cancellation, partial evidence, and adapter outage with correct fallback.
8. [ ] **EGR-1608: Implement staged rollout controls.** Support disabled, shadow, evidence-only, regulated-direct-answer, and rollback modes by namespace and policy version.
9. [ ] **EGR-1609: Define, calibrate, and approve numerical gates.** Record baseline-relative and absolute gates for false answers, value, p95, memory, startup, durability, and evidence size before tuning a feature for release. Select coefficients and thresholds only on the independently labeled tuning partition, freeze them before the release gate, use repeated-run statistics where execution is nondeterministic, and reserve final-test execution for the release decision.
10. [ ] **EGR-1610: Produce a release evidence packet.** Include source revision, configuration, artifacts, tests, benchmarks, evaluation results, known limitations, migrations, security review, rollout decision, and tracker updates.

**Evidence required:** versioned corpus manifest, reproducible evaluation command, machine-readable results, rollout dashboard or report, chaos results, and approved release packet.

## Blocking Issues

No blockers recorded. Add a blocker here only when a named external decision, dependency, or authorization prevents an active item from progressing; mark that item `[-]` in its component at the same time.
