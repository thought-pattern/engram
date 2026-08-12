# Engram Project Tracking

**Audience: Internal | Status: Enhancement program In Progress, 29/148 | 12 August 2026**

Last updated: 2026-08-12

Tracks implementation status only. Components and ordering follow [ENGRAM-DEVELOPMENT.md](ENGRAM-DEVELOPMENT.md). Each checkbox is a buildable, checkable artifact. Status is assessed against the active Engram codebase. Commit, review, and merge state are human workflow concerns and do not determine whether implementation work is Done.

This file is the implementation-status authority in this repository for the Engram enhancement program. The Engram project's upstream tracker retains upstream authority, and Tapestry status remains governed by `tapestry-source/PDC-PROJECT-TRACKING.md`.

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
| 3 | Accepted response commit and lifecycle | P0 | §§1-2 | Not Started | 0/9 |
| 4 | Unified resolution pipeline | P0 | §§1-3 | Not Started | 0/10 |
| 5 | Candidate fusion and ambiguity | P1 | §4 | Not Started | 0/8 |
| 6 | Feedback learning and negative resolution | P1 | §§3-5 | Not Started | 0/8 |
| 7 | Evidence-only Tapestry handoff | P1 | §§4-5 | Not Started | 0/7 |
| 8 | Query frames and relation-aware graph lookup | P2 | §§4-5 | Not Started | 0/10 |
| 9 | Temporal, trust, and conflict semantics | P2 | §§7-8 | Not Started | 0/9 |
| 10 | Bounded graph composition | P2 | §§8-9 | Not Started | 0/8 |
| 11 | Symbolic retrieval rewrite layer | P3 | §§1, 4-5 | Not Started | 0/7 |
| 12 | Sparse retrieval enhancement | P3 | §§1-5 | Not Started | 0/8 |
| 13 | Standalone semantic retrieval and reranking | P3 | §§5, 12 | Not Started | 0/9 |
| 14 | Utility resolver plugins | P3 | §§4-5 | Not Started | 0/6 |
| 15 | Interfaces, migration, security, and operations | Continuous | Cross-cutting | Not Started | 0/10 |
| 16 | Evaluation, rollout, and release | Continuous | Cross-cutting | Not Started | 0/10 |
| | **Program total** | | | **In Progress** | **29/148** |

P0 establishes correctness and the shared architecture. P1 uses those contracts to improve regulated recall and evidence handoff. P2 adds structured graph depth after unified resolution is stable. P3 work is optional and advances independently only when held-out evaluation justifies its resource and operating cost. §§15-16 apply throughout.

Sections 0 and 1 are complete. The current build order continues with §2 and then §3. Later components may prepare research and evaluation fixtures, but implementation does not bypass index and lifecycle foundations unless the affected dependency and development plan are revised together.

## Documented Baseline

These behaviors are reported by the source documentation and were reconciled in §0. They are descriptive baseline capabilities rather than additional items in the 148-item enhancement count. The source documentation identifies a `pdc-3` branch with support-aware sentence embeddings and Schema 3.5 graph behavior; EGR-001 records the audited source state and corrections.

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
- [Focused and full verification output](documentation/baseline/test-results-2026-08-11.md): initial 803 passed plus the 12 August remediation addendum; current full suite 927 passed with 1 strict expected failure
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

**Ownership boundary:** §1 owns pure surface identity, representations, validation, and retrieval-key construction. Section 2 owns the index projection boundary, cross-artifact key mappings, collision discovery, generic index mutation, and atomic index state. Section 3 owns artifact projection, lifecycle eligibility, commit-time rejection, and explicit supersession. Section 8 owns contextual and graph-backed canonical entity/predicate resolution. Section 15 owns persistence, startup, operator, and transport integration, and §16 owns held-out release gates.

### Section 1 evidence

- [Version 1 identity contract](documentation/identity/contracts-v1.md)
- [Golden normalization and adversarial fixture](documentation/identity/normalization-v1.json)
- [Focused and full verification results](documentation/identity/test-results-2026-08-11.md): 103 focused tests passed; full suite 927 passed with the legacy replacement defect retained as one strict expected failure
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

**Evidence required:** typed projection/state documentation, focused tests for every task, invariant and concurrency output, rebuild-equivalence artifact, collision and legacy-classification fixture, support-path integration results, complexity and memory benchmark, and transport-neutral check/repair documentation. Persistence/startup, adapter, operational, and release evidence remains assigned to applicable §§15-16 tasks.

**Ownership boundary:** §2 accepts projections and owns derived-index mechanics only. It does not infer lifecycle from tier, manufacture exact identity for legacy records, persist authoritative artifacts, or expose adapter operations. Section 3 creates projections and composes generic mutations into commit and lifecycle transactions; §15 integrates persistence, startup/readiness, adapters, authorization, and operator procedures; §16 evaluates release-scale gates.

### Section 2 evidence

- [Version 1 index contracts and transport-neutral repair behavior](documentation/indexes/contracts-v1.md)
- [Collision, legacy, unsupported-version, and malformed-support fixture](documentation/indexes/classification-v1.json)
- [Focused, full-suite, static-analysis, and benchmark results](documentation/indexes/test-results-2026-08-12.md): 36 focused tests passed; full suite 963 passed with the Section 3 legacy replacement defect retained as one strict expected failure
- Implementation: `engram/indexes.py`; live integration in `engram/core.py`, `engram/eviction.py`, and `engram/persistence.py`
- Invariant and integration suite: `tests/test_indexes.py`, with completed-boundary updates in `tests/test_baseline_gaps.py`
- [100,000-projection and full 5,000-artifact support-proposal benchmark](documentation/indexes/benchmark-2026-08-12.json) and harness at `scripts/benchmark_indexes.py`

All eleven tasks meet the Section 2 exit condition after the 12 August remediation. Exact lookup remained constant-time relative to corpus size with a measured 0.2251x p95 slope from 10,000 to 100,000 projections. The full 5,000-artifact regulated support proposal measured p95 0.6818/1.0115/1.6250 ms at fan-out 1/10/100 and passed both the 30 ms absolute and 125%-of-baseline gates. Scope filtering precedes bounded top-k selection, scan exhaustion abstains without a partial answer, explicit empty projection sets remain meaningful, reproducible diagnostic corruption blocks publication, forward/inverse equivalence holds under rebuild and every generic mutation, and collision or legacy exclusion never selects an arbitrary response. Persistence schema/startup policy and adapter/operator exposure remain assigned to §15 rather than being counted here.

## §3. Accepted response commit and lifecycle (0/9)

**Priority:** P0  
**Depends on:** §§1-2.  
**Exit:** An authoritative caller can commit an unchanged accepted response as STATIC or DYNAMIC, then explicitly supersede, invalidate, or retire it without confusing validity with eviction.

Tasks are listed in dependency order while retaining their stable EGR identifiers.

1. [ ] **EGR-304: Define lifecycle states and base eligibility.** Implement ACTIVE, SUPERSEDED, INVALIDATED, and RETIRED separately from tier and eviction state; publish the legal transition matrix and the lifecycle portion of `direct_answer_eligible` before artifact projection or mutation operations use it.
2. [ ] **EGR-301: Add `CachedResponseArtifact` and its index projection.** Define deterministic concrete codecs for exact response text, query identity, retrieval representations, tier, lifecycle, scope, support, temporal validity, knowledge epoch, supersession, generation, provenance, statistics, and bounded metadata. Project only the fields required by §2 without making the projection authoritative.
3. [ ] **EGR-306: Enforce validity and knowledge-epoch eligibility.** Validate half-open `valid_from`/`valid_until` bounds and caller-configured epoch eligibility without inferring truth; combine these rules with lifecycle into the artifact's §2 projection.
4. [ ] **EGR-309: Add the response persistence schema and migrate legacy state.** Preserve text, tier, scope, support, provenance, and statistics; make migration idempotent, recoverable, and compatible with explicit backup or output behavior. Legacy entries with no recoverable request identity remain unindexed with a concrete reason, while ambiguous recoverable keys are excluded from direct lookup rather than assigned a winner. Execute this feature slice with EGR-1505 before a configured commit path claims durable checkpoint support.
5. [ ] **EGR-302: Implement transport-neutral `commit_response`.** Validate the artifact and authoritative identity, reject empty and normalized complete `IDK`, reject scoped canonical or alias collisions with named existing statement IDs, compose §2 generic index mutations atomically, checkpoint once, and return a stable mutation result.
6. [ ] **EGR-303: Support explicit STATIC accepted outputs.** STATIC commit must protect against ordinary capacity eviction without implying permanent truth, lifecycle, or eligibility.
7. [ ] **EGR-305: Add explicit lifecycle transition operations.** Require typed reason, caller identity or provenance, idempotent request identity, expected generation, legal transition checks, audit fields, one §2 index mutation, and one configured checkpoint.
8. [ ] **EGR-307: Implement concurrency-safe explicit supersession.** Require the expected current statement ID and generation, reject implicit last-writer-wins replacement, create the replacement, update retrieval mappings, and name `superseded_by` atomically.
9. [ ] **EGR-308: Preserve `LearnResponse` compatibility.** Implement it as a DYNAMIC ACTIVE convenience over the new commit path while retaining retry, proposal, user-context, and `IDK` behavior.

**Evidence required:** artifact and projection codec fixtures, lifecycle and eligibility matrices, persistence fixtures across every supported schema, exact-text byte or Unicode equality checks, index-mutation equivalence, checkpoint and idempotency tests, concurrent-transition tests, and migration recovery instructions.

## §4. Unified resolution pipeline (0/10)

**Priority:** P0  
**Depends on:** §§1-3.  
**Exit:** All retrieval paths emit common candidates and the core returns explicit ANSWER, EVIDENCE, or MISS outcomes under a shared budget and accounting policy.

1. [ ] **EGR-401: Add the versioned `QueryFrame`.** Carry original and resolved text, identity, expected object type, inheritance provenance, rewrite chain, scope, required metadata, budget, and diagnostic identity.
2. [ ] **EGR-402: Add the common `Candidate` and evidence-reference types.** Represent response, source, concretely typed feature values and availability, evidence, scope, lifecycle, provenance, and diagnostics consistently without optional unions.
3. [ ] **EGR-403: Add resolver and result protocols.** Define availability, cost class, bounded execution, typed skip/failure reasons, candidates, evidence, elapsed time, and consumed budget.
4. [ ] **EGR-404: Add `ResolutionBudget`.** Enforce total time, resolver time, candidates, graph rows, vector results, evidence size, and configured cost-class limits.
5. [ ] **EGR-405: Adapt exact retrieval.** Return an eligible exact candidate and short-circuit other work when policy permits; expose canonical versus alias provenance.
6. [ ] **EGR-406: Adapt pattern retrieval and separate its accounting.** Pattern selection during proposal records candidacy only; accepted resolution records success exactly once.
7. [ ] **EGR-407: Adapt lexical retrieval.** Preserve existing keyword, lemma, stem, synonym, spelling, phrase, recency, hit-rate, and priority diagnostics in normalized features.
8. [ ] **EGR-408: Adapt structured graph recall.** Stop disguising graph recall as a pattern response and emit explicit graph candidates or evidence.
9. [ ] **EGR-409: Adapt support-aware semantic graph recall.** Preserve fixed internal vector search, exact scope, active Claim filters, support intersection, and separate lexical and semantic features.
10. [ ] **EGR-410: Add `ResolutionResult` and orchestration.** Produce ANSWER, EVIDENCE, or MISS with selected candidate, bounded alternatives, evidence, confidence, reason codes, frame diagnostics, and budget consumption.

**Evidence required:** resolver conformance suite, adapter-independent core tests, accounting tests, optional resolver failure tests, budget-exhaustion tests, and deterministic result fixtures.

## §5. Candidate fusion and ambiguity (0/8)

**Priority:** P1  
**Depends on:** §4.  
**Exit:** Independent evidence can reinforce a response, while close alternatives, mismatched identity, incomplete support, stale state, and conflicts prevent unsafe direct selection.

1. [ ] **EGR-501: Define feature semantics and ranges.** Specify exact, pattern, lexical, semantic, entity, relation, object type, support, history, freshness, authority, agreement, and margin features; unavailable values use concrete falsy containers plus explicit availability rather than `null`.
2. [ ] **EGR-502: Normalize resolver-specific scores.** Calibrate or transform scores onto documented comparable inputs without treating unrelated raw scales as interchangeable.
3. [ ] **EGR-503: Deduplicate candidates and calculate agreement.** Merge evidence for the same statement while retaining every resolver contribution and diagnostic.
4. [ ] **EGR-504: Centralize eligibility filtering.** Apply scope, metadata, lifecycle, validity, ownership visibility, support, and required-source checks before answer scoring.
5. [ ] **EGR-505: Implement transparent first-generation fusion.** Use a versioned configurable formula with inspectable contributions and no hidden generative inference.
6. [ ] **EGR-506: Add top-two margin and ambiguity handling.** Lower confidence or abstain when distinct candidates are too close, even when the leading absolute score is high.
7. [ ] **EGR-507: Add stable policy reason codes.** Explain answer, evidence, and miss decisions without logging sensitive content or relying on free-form prose.
8. [ ] **EGR-508: Calibrate thresholds on disjoint data.** Establish ANSWER and EVIDENCE gates, validate false-direct-answer behavior on a held-out set, and record the released policy version.

**Evidence required:** feature specification, calibration artifact, held-out confusion matrix, before/after latency and acceptance results, and explainability fixtures.

## §6. Feedback learning and negative resolution (0/8)

**Priority:** P1  
**Depends on:** §§3-5.  
**Exit:** Typed Regulator outcomes influence the correct scope, and repeated unresolved requests can avoid redundant work without storing `IDK` as knowledge.

1. [ ] **EGR-601: Add statement-level verdict statistics.** Persist candidate, acceptance, and each typed rejection count partitioned by applicable policy or contract version.
2. [ ] **EGR-602: Add query-relationship statistics.** Track query identity, statement, namespace, context, and relevant version relationships without weakening unrelated scopes.
3. [ ] **EGR-603: Apply typed rejection semantics.** Quality weakens broad statement standing; context weakens only the relationship; stale makes the relevant artifact ineligible pending lifecycle action; policy suppresses the applicable policy scope.
4. [ ] **EGR-604: Add feedback-derived fusion features.** Use minimum sample sizes, bounded priors, and explicit availability markers so sparse observations do not create unjustified certainty.
5. [ ] **EGR-605: Add statistics aging and retention.** Preserve inspectable raw observations, deterministic decay, storage bounds, and protection against permanent standing from old traffic.
6. [ ] **EGR-606: Expose feedback diagnostics.** Add bounded inspection and metrics for statement and relationship history without unbounded identifier labels.
7. [ ] **EGR-607: Add bounded `NegativeResolution` records.** Key by query identity, exact scope, context, knowledge epoch, reason, and expiry; keep them separate from statements and response retrieval.
8. [ ] **EGR-608: Add negative invalidation and safety rules.** Invalidate on relevant epoch change, bound memory and TTL, distinguish knowledge misses from policy or transport failures, and prevent cross-scope reuse.

**Evidence required:** outcome-policy tests, cross-scope isolation tests, decay tests, persisted feedback fixtures, negative-cache expiry and epoch tests, and a repeated-miss cost benchmark.

## §7. Evidence-only Tapestry handoff (0/7)

**Priority:** P1  
**Depends on:** §§4-5.  
**Exit:** A response-cache miss can return a bounded, currently eligible Claim package that measurably reduces Tapestry retrieval work without authorizing an Engram answer.

1. [ ] **EGR-701: Define the evidence record.** Include stable Claim ID, resolver, structured and semantic match features, permissible canonical references, validity/trust inputs, path, and selection reasons.
2. [ ] **EGR-702: Return graph evidence without an attached response.** Extend structured and semantic graph resolvers so relevant active Claims are not discarded solely because no cached response references them.
3. [ ] **EGR-703: Add evidence eligibility checks.** Enforce scope, ownership visibility, active canonical Claim status, temporal validity, trust floor, and retrieval-only exclusions.
4. [ ] **EGR-704: Bound evidence packages.** Enforce Claim count, byte or token estimate, graph rows, similarity, trust, path size, and wall-clock limits with truncation diagnostics.
5. [ ] **EGR-705: Integrate EVIDENCE policy.** Return evidence only when it passes usefulness gates and no direct answer qualifies; preserve MISS for noise.
6. [ ] **EGR-706: Version the service contract.** Add a reviewed MCP/gRPC representation without exposing arbitrary Claim, Passage, proof, or Cypher content.
7. [ ] **EGR-707: Validate the Tapestry handoff.** Demonstrate current support revalidation, no unregulated direct response, fewer initial Knowledge Engine retrieval calls or tokens, and unchanged Actor fallback on Engram failure.

**Evidence required:** wire-contract review, payload security review, end-to-end Tapestry fixture, useful-evidence evaluation, and failure-path tests.

## §8. Query frames and relation-aware graph lookup (0/10)

**Priority:** P2  
**Depends on:** §§4-5.  
**Exit:** Engram can interpret representative direct relation questions and bounded elliptical follow-ups, perform parameterized one-hop canonical Claim lookup, and abstain when identity is uncertain.

1. [ ] **EGR-801: Persist a compact previous query frame in user context.** Retain operator, subjects, relation, expected type, qualifiers, source turn, and confidence under existing user isolation and TTL rules.
2. [ ] **EGR-802: Implement bounded follow-up inheritance.** Fill only missing fields, record inherited provenance, honor topic continuity and turn distance, and avoid contaminating self-contained requests.
3. [ ] **EGR-803: Extend operator classification for query frames.** Reuse the §1 operator contract and classifier, adding contextual inheritance and confidence needed by query frames without creating a second incompatible vocabulary; cover who, what, where, when, which, how many, lookup, exists, count, compare, and unknown.
4. [ ] **EGR-804: Resolve canonical subject entities.** Use labels, aliases, edge surface forms, named-entity evidence, and explicit ambiguity results.
5. [ ] **EGR-805: Extract predicate candidates.** Combine dependency structure, verb lemmas, prepositions, canonical Predicate labels, synonyms, and explicit caller identity.
6. [ ] **EGR-806: Infer expected object type.** Support PERSON, PLACE, DATE, NUMBER, BOOLEAN, ENTITY, and UNKNOWN without rejecting valid unknown types prematurely.
7. [ ] **EGR-807: Compile internal one-hop query plans.** Use parameterized, allow-listed templates; do not accept caller-supplied Cypher or procedure names.
8. [ ] **EGR-808: Execute and filter one-hop Claim lookup.** Enforce active canonical status, exact scope or visibility policy, expected type, temporal defaults, row limits, and timeouts.
9. [ ] **EGR-809: Integrate graph phrasing and evidence.** Phrase only one unambiguous eligible result; otherwise return Claim evidence with relation and ambiguity reasons.
10. [ ] **EGR-810: Build the relation and follow-up benchmark.** Include entity ambiguity, predicate ambiguity, paraphrases, technical relations, explicit references, elliptical replacement, and self-contained context-reset cases.

**Evidence required:** graph query-plan fixtures, read-only enforcement tests, user-isolation and inheritance tests, held-out relation results, and one-hop latency measurements.

## §9. Temporal, trust, and conflict semantics (0/9)

**Priority:** P2  
**Depends on:** §§7-8.  
**Exit:** Engram does not present historical knowledge as current, respects visibility and supplied trust, and turns contradictory eligible Claims into evidence or abstention.

1. [ ] **EGR-901: Define temporal query qualifiers.** Represent current, now, as-of, in-year, before, after, between, and latest separately from lexical terms.
2. [ ] **EGR-902: Implement conservative temporal parsing.** Normalize dates and years, preserve unresolved expressions, and expose parse confidence and source text.
3. [ ] **EGR-903: Apply valid-time and system-time filtering.** Follow the graph schema's half-open intervals and distinguish requested valid time from observation time.
4. [ ] **EGR-904: Implement current, historical, bounded, and latest query policies.** Define deterministic selection and abstention for missing, overlapping, open, or incomparable bounds.
5. [ ] **EGR-905: Consume ownership visibility and trust metadata.** Treat visibility as eligibility and trust as a policy feature; do not assign, upgrade, or silently default canonical graph values.
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
6. [ ] **EGR-1006: Return Claim paths as evidence.** Record ordered Claims, bindings, operators, filters, and aggregation inputs without exposing arbitrary graph content.
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

## §15. Interfaces, migration, security, and operations (0/10)

**Priority:** Continuous  
**Depends on:** Each affected feature.  
**Exit:** Released capabilities have consistent core, Python, MCP, and gRPC behavior; safe persistence migration; bounded observability; and documented deployment, failure, and rollback procedures.

1. [ ] **EGR-1501: Keep the core transport-neutral.** Implement identity, commit, lifecycle, resolution, evidence, index, and feedback behavior once in `EngramCore` or lower layers; adapters only validate and translate.
2. [ ] **EGR-1502: Evolve the Python API compatibly.** Expose §1 identity inputs and precisely typed operations with concrete falsy absence values and no optional union annotations; preserve documented lower-level calls through wrappers with deprecation and behavior tests.
3. [ ] **EGR-1503: Evolve the MCP tools compatibly.** Validate and translate authoritative identity at the boundary while preserving persistent process ownership, proposal accounting, retry identity, lifecycle, bounded transient state, and tool documentation.
4. [ ] **EGR-1504: Evolve and regenerate gRPC contracts.** Represent authoritative identity and other additive fields in v1 where safe or use an explicit versioned service where semantics are incompatible; regenerate pinned stubs and verify byte-for-byte reproducibility.
5. [ ] **EGR-1505: Implement persistence schema management.** Persist response-owned identity and retrieval representations, record schema, normalization, index, policy, and model versions, and provide idempotent migration, explicit output or backup behavior, and documented downgrade constraints.
6. [ ] **EGR-1506: Preserve concurrency and idempotency.** Test live-state and checkpoint atomicity, exact retries, conflicting retries, deadlines, cancellation, concurrent proposal resolution, and index visibility.
7. [ ] **EGR-1507: Enforce security and privacy bounds.** Limit request, alias, metadata, support, evidence, diagnostic, and rewrite sizes; keep graph read-only; validate identifiers; separate mutation authority; redact sensitive logs.
8. [ ] **EGR-1508: Eliminate runtime dependency acquisition.** Preflight NLTK data, spaCy models, embedding artifacts, graph indexes, and dimensions; make offline or fail-soft behavior explicit and prohibit runtime model downloads.
9. [ ] **EGR-1509: Add bounded operational telemetry.** Measure outcome, resolver contribution, rejection reasons, latency, budget exhaustion, rebuild, durability, and resource use without raw text or high-cardinality identifiers as labels.
10. [ ] **EGR-1510: Document deployment and rollback.** Cover standalone, graph-backed, and Tapestry-backed modes, health/readiness, startup rebuild, degraded dependencies, backups, lifecycle repair, feature flags, policy rollback, and incident response.

**Evidence required:** cross-adapter contract matrix, generated-stub check, persistence compatibility suite, concurrency suite, security review, offline-start test, telemetry cardinality review, and rollback exercise.

## §16. Evaluation, rollout, and release (0/10)

**Priority:** Continuous  
**Depends on:** §0 baseline and every promoted behavior.  
**Exit:** Each release is supported by reproducible held-out accuracy, inference-avoidance, evidence-usefulness, latency, memory, compatibility, failure, and rollback evidence.

1. [ ] **EGR-1601: Create versioned evaluation partitions.** Maintain disjoint development, calibration, and release sets with provenance and no hidden leakage into authored rewrite rules or trained policies.
2. [ ] **EGR-1602: Cover the required workload families.** Include exact, alias, paraphrase, multi-turn, technical, graph, temporal, ambiguous, conflicting, stale, scope, policy, unsupported, negative, and dependency-failure cases.
3. [ ] **EGR-1603: Add adversarial near-collision gates.** Make `when`/`where`, current/historical, positive/negative, same terms under different relations, and same language under different scopes mandatory release cases.
4. [ ] **EGR-1604: Measure answer and evidence quality.** Track direct-answer acceptance, false direct answers, useful evidence, proposal acceptance, typed rejection distribution, later correction, and abstention quality.
5. [ ] **EGR-1605: Measure avoided Tapestry work.** Track Actor bypass, inference calls, input and output tokens, Knowledge Engine retrieval calls, prompt evidence size, and end-to-end latency saved.
6. [ ] **EGR-1606: Measure component and resource performance.** Record p50 and p95 total and resolver latency, cold start, startup rebuild, persistence, graph recall, semantic recall, throughput, memory, disk, and CPU.
7. [ ] **EGR-1607: Add failure and chaos scenarios.** Exercise unavailable graph, missing model, bad index, dimension mismatch, persistence degradation, restart, timeout, cancellation, partial evidence, and adapter outage with correct fallback.
8. [ ] **EGR-1608: Implement staged rollout controls.** Support disabled, shadow, evidence-only, regulated-direct-answer, and rollback modes by namespace and policy version.
9. [ ] **EGR-1609: Define and approve numerical gates.** Record baseline-relative and absolute gates for false answers, value, p95, memory, startup, durability, and evidence size before tuning a feature for release.
10. [ ] **EGR-1610: Produce a release evidence packet.** Include source revision, configuration, artifacts, tests, benchmarks, evaluation results, known limitations, migrations, security review, rollout decision, and tracker updates.

**Evidence required:** versioned corpus manifest, reproducible evaluation command, machine-readable results, rollout dashboard or report, chaos results, and approved release packet.

## Blocking Issues

No blockers recorded. Add a blocker here only when a named external decision, dependency, or authorization prevents an active item from progressing; mark that item `[-]` in its component at the same time.
