# Engram Development Plan

**Audience: Internal | Status: Sections 0-13 component-implemented; Sections 15-16 active; release not approved | Baseline: 10 August 2026 | Updated: 22 August 2026**

Implementation status for this plan is maintained in [ENGRAM-PROJECT-TRACKING.md](ENGRAM-PROJECT-TRACKING.md). This document defines the intended architecture, sequencing, contracts, and acceptance gates. It does not mark proposed work as implemented.

## 1. Purpose

Engram should reduce the expensive work Tapestry must perform by resolving requests through bounded, predictable CPU computation. It should return an accepted response directly when the available evidence is sufficient, return a useful evidence package when it can narrow Tapestry's work, and abstain when neither outcome is safe.

The documented Engram implementation already contains symbolic matching, lexical retrieval, graph recall, support-aware vector retrieval, conversation state, regulated proposal handling, persistence, and several interfaces. The immediate development problem is to give these capabilities a sufficiently precise shared query identity, write contract, candidate model, and resolution policy.

This plan develops Engram in four increments:

1. Make cached response identity and lifecycle correct.
2. Unify existing retrieval paths and learn from regulated outcomes.
3. Add relation-aware, temporal, and bounded graph resolution.
4. Expand retrieval breadth only after the common resolution contract is measured and stable.

## 2. Sources and status basis

This plan originated by reconciling a historical inference-avoidance development study with the active Engram implementation. The source study is not retained in this repository; this plan and its accepted ADRs are the repository-local authority for the resulting target direction.

Current implementation context comes from the active [README](README.md), [MCP](documentation/mcp-integration.md), [gRPC](documentation/grpc-integration.md), [narrative](documentation/narrative.md), and [code-style](documentation/code-style.md) guides plus the Python modules and tests in this repository.

Section 0 reconciled those claims against the active source and recorded the audited revision, discrepancies, tests, and measurements in [the source audit](documentation/baseline/source-audit-2026-08-11.md). Current-state statements below remain baseline descriptions; implementation completion is governed by the tracker and its linked evidence.

Sections 0 through 13 have since been component-completed under that authority. The executable Section 4 substrate and its current resource/timing boundary are published in the [unified resolution contract v2](documentation/artifacts/resolution-contract-v2.md); v2 removes latency limits from answer policy while retaining elapsed reporting and non-time resource bounds. Section 5 feature semantics, authoritative revalidation, fusion, ambiguity, and policy boundaries are published in the [candidate-fusion contract v1](documentation/fusion/contracts-v1.md), with verification and benchmark evidence in the [Section 5 conformance report](documentation/fusion/section5-conformance-2026-08-15.md). Section 6 targeting, receipt, aggregation, aging, feedback-history, lifecycle-handoff, persistence, recovery, and negative-resolution boundaries are published in the [feedback and negative-resolution contract v1](documentation/feedback/contracts-v1.md), with verification and scale evidence in the [Section 6 conformance report](documentation/feedback/section6-conformance-2026-08-16.md). Section 7's in-place core evidence evolution, unchanged CLI/MCP/current-gRPC boundaries, strict contracts, disclosure gate, response-less producers, bounded orchestration, conformance results, and engineering benchmark are published in the [accepted decision](documentation/decisions/0005-section7-evidence-compatibility.md), [core handoff](documentation/evidence/core-handoff.md), and [Section 7 conformance report](documentation/evidence/section7-conformance-2026-08-16.md). Section 11's configuration-gated retrieval rewrites are published in [the symbolic rewrite contract](documentation/rewrite/contracts-v1.md). Section 12's fielded BM25 implementation, engine comparison, source-bound gate, and live MCP evidence are published in the [sparse contract](documentation/sparse/contracts-v1.md), [ADR 0006](documentation/decisions/0006-section12-fielded-bm25.md), and [conformance report](documentation/sparse/section12-conformance-2026-08-21.md). Section 13's checksum-gated offline embeddings, exact-cosine lifecycle, transparent reranker, independent promotion decisions, and live MCP evidence are published in the [semantic contract](documentation/semantic/contracts-v1.md), [ADR 0007](documentation/decisions/0007-section13-local-semantic-and-transparent-reranking.md), and [conformance report](documentation/semantic/section13-conformance-2026-08-22.md). A future gRPC evidence RPC/message is versioned only when Section 15 changes that actual external interface. Component completion does not imply that Section 16's protected evaluation or release gate has passed.

The 20 August 2026 execution update completes EGR-1502's stable [Python API v1](documentation/python-api.md), including authoritative identity input, the Section 7 evidence package, keyed feedback examples, strict mapping boundaries, concrete falsy absence, and a transient cooperative-cancellation hook. EGR-1510 adds the [deployment and rollback runbook](documentation/operations/deployment-and-rollback-v1.md) and its isolated rollback exercise. The [Section 16 evaluation foundation](documentation/evaluation/foundation-v1.md) now contains only public tuning cases and contrasts. Release-gate and final-test custody slots are unprovisioned and unauthorized; their previously visible examples were compromised for release use and removed. Proposed numerical sample floors are unmet, and no release approval is claimed. Engineering benchmark artifacts bind to governed source state and report turn-length distributions without a latency pass/fail threshold.

The EGR-702 through EGR-712 [full Claim evidence and package contracts v1](documentation/evidence/contracts-v1.md) supply the strict canonical-reference, validity, trust, disclosure-provenance, singleton-path, excluded-content, canonical-deduplication, exact-omission, 10-record/64-KiB, fixed canonical-only graph-projection, current disclosure, exact-scope authority, publication-revalidation, structured and semantic full-record production, legacy semantic-candidate preservation, order-independent fail-closed cross-producer merging, the frozen unfitted usefulness policy, raw-record containment, bounded Claim-only EVIDENCE/MISS orchestration, the [actual core/adapter handoff](documentation/evidence/core-handoff.md), and the completed conformance gate.

The local Tapestry integration design and trackers were used only to preserve the existing process boundary and acceptance contract. They do not assign Engram work status. In an integration workspace, Tapestry milestone and implementation authority remains with `tapestry-source/PDC-PROJECT-TRACKING.md`; that external source tree is not vendored in this repository.

## 3. Documented baseline

The following capabilities are treated as the starting point.

| Area | Documented behavior |
| --- | --- |
| Application core | `EngramCore` owns one shared `Engram`, isolated user runtimes, persistence, and regulated-cache proposal state. CLI, MCP, and gRPC are adapters over the core. |
| Storage | Statements are plain dictionaries in STATIC and DYNAMIC tiers. STATIC is protected from capacity eviction. DYNAMIC supports FIFO, LRU, LFU, and hit-rate eviction. |
| Lexical retrieval | Stopword-filtered keywords, IDF-weighted overlap, recency, statement and keyword hit statistics, stemming, lemmatization, WordNet synonyms, spelling correction, and optional phrase keywords. |
| Symbolic retrieval | Regex-based AIML-style patterns, wildcard specificity, previous-response and topic context, predicates, conditions, maps, sets, redirects, random choices, and dynamic learning. |
| Conversation state | User-isolated histories, predicates, active topics, canonical referenced entities, dialogue acts, repetition handling, fact admissions, and conservative pronoun expansion. |
| Regulated cache | `Propose` returns exactly scoped, patternless response candidates without a hit. `Resolve` records one typed Regulator verdict. `LearnResponse` stores a non-empty, non-`IDK` Actor response. `RetireResponse` removes globally stale DYNAMIC patternless responses. |
| Scope and provenance | Exact namespace and context-fingerprint filters, required top-level Tapestry metadata, source labels, user attribution, and opaque support Claim identifiers. |
| Graph access | Optional, fail-soft, read-only Memgraph access using canonical Entity, Predicate, and Claim structures. Fixed triple lookup and guarded read-only Cypher are available. |
| Semantic graph recall | A local CPU sentence transformer can search an externally managed Claim-premise vector index. Results are intersected with cached-response support Claims and merged with keyword scores. |
| Persistence | JSON state, atomic checkpoints for durable core mutations, degraded-durability reporting, final flush, backward-compatible configuration loading, and non-persistence of secrets and transient proposals. |
| Interfaces | Lower-level Python APIs, CLI, persistent MCP tools, and a versioned unary gRPC service with health, typed errors, TLS support, and graceful shutdown. |
| Verification | The documentation reports unit, concurrency, adapter, persistence, regulated-cache, network, health, TLS, restart, and generated-stub coverage, plus a pattern evaluation corpus. |

### 3.1 Known constraints in the baseline

The development study and source documentation identify the following concrete constraints:

- Response equivalence is based on a request keyword set plus exact scope. Removing grammatical operators can collapse distinct questions such as `when` and `where` into the same identity.
- Matcher `pattern_aliases` exist, but Tapestry-generated retrieval aliases do not have a separate, non-executable representation.
- There is no documented exact reverse index from a scoped canonical request or alias to one cached response.
- Support-aware vector proposal lookup scans statements to find responses attached to returned Claim IDs instead of using a documented `claim_id -> statement_ids` reverse index.
- Regulated learn-back produces a DYNAMIC response and does not provide the general accepted STATIC commit and lifecycle contract required by the development study.
- STATIC describes eviction protection. It does not independently express ACTIVE, SUPERSEDED, INVALIDATED, or RETIRED validity state.
- Pattern, lexical, conversational graph, and support-aware vector paths do not expose one common candidate and outcome model.
- Keyword and vector evidence are merged, but the documented policy does not yet expose agreement, ambiguity margin, entity, relation, support coverage, freshness, or authority as first-class fusion features.
- Regulator outcomes are counted, but no documented statement-level and query-relationship learning model applies the different rejection meanings.
- Vector recall can help select a cached response, but relevant validated Claims without an attached response are not returned as an evidence-only result.
- Graph recall resolves entities and related facts, but it does not yet document deterministic relation extraction, temporal query interpretation, contradiction handling, or bounded multi-hop composition.
- Session expansion handles explicit references conservatively. It does not yet carry a structured previous query frame for elliptical follow-ups.

These constraints define the first work. Later search and model additions must not bypass them.

## 4. Objectives and non-goals

### 4.1 Objectives

Engram development should:

1. Prevent semantically distinct requests from overwriting or retrieving each other's responses.
2. Move reusable semantic work to the write path through canonical requests, retrieval aliases, scope, support, provenance, validity, and lifecycle metadata.
3. Make exact repeats and accepted aliases the least expensive resolution path.
4. Represent every resolver result through one inspectable candidate contract.
5. Distinguish a direct answer, evidence-only handoff, and miss.
6. Combine independent retrieval evidence and abstain on ambiguity, conflict, invalidity, or insufficient support.
7. Use typed Regulator outcomes as appropriately scoped learning signals.
8. Preserve standalone utility while making graph-backed and Tapestry-backed deployments progressively more capable.
9. Keep graph access read-only, bounded, and compatible with the shared canonical Claim schema.
10. Measure avoided or reduced Tapestry work, with raw cache hits retained as a diagnostic metric.

### 4.2 Non-goals

This plan does not turn Engram into:

- a generative language model;
- an authority for truth, trust, ownership, scope, policy, or freshness in a Tapestry deployment;
- a general Cypher execution service;
- an unbounded graph reasoner or proof constructor;
- a replacement for Tapestry's Knowledge Engine, Actor, or Regulator;
- a distributed cache or replicated database in the initial work; or
- a runtime model downloader.

Open-ended reasoning, proof construction, broad tool use, and generative work remain outside Engram's boundary.

## 5. Engineering invariants

The following invariants apply across all phases.

1. **Accepted text is immutable.** Engram returns the exact human-facing response committed by the authoritative caller. Canonicalization and aliases change retrieval metadata, not response prose.
2. **Acceptance remains a separate authority.** Retrieval scores rank candidates. Tapestry's current support validation and Regulator remain the release boundary for Tapestry-coupled direct answers.
3. **Identity differs from search terms.** Stopwords may be omitted from lexical search while operators and qualifiers remain part of semantic identity.
4. **STATIC differs from validity.** Tier controls eviction. Lifecycle and validity control eligibility.
5. **Aliases are data, not programs.** Retrieval aliases must never become executable AIML patterns unless an author deliberately creates a separate pattern.
6. **Secondary indexes are disposable.** Persisted response artifacts are authoritative. Exact, alias, support, sparse, and vector indexes must be rebuildable and consistency-checkable.
7. **Graph access is read-only.** Engram does not create or mutate canonical Claims, Entities, Predicates, trust, ownership, or temporal bounds at runtime.
8. **Every expensive path is bounded.** Resolver count, candidate count, graph hops, graph rows, vector results, evidence, serialized output and diagnostics, bounded working-memory estimates, and branches have explicit resource limits. Callers can cancel cooperatively. Elapsed time is observed but never used as a knowledge-quality or fusion threshold.
9. **Required local dependencies are ready at startup; graph tooling is optional.** Runtime packages are hard installation requirements and use normal eager imports. Required NLTK and explicitly selected local language resources are provisioned before startup. A configured but unavailable graph or graph-vector capability is reported as not ready and skipped without preventing Engram from serving local cache, matcher, conversation, or regulated-response paths. Optional graph I/O runs outside the core-wide state lock, so an outstanding graph call does not block status, mutations, or local-only resolution for another user; retry identity and per-user contextual order remain serialized. Runtime downloads, deferred dependency imports, and first-request model loading are not allowed.
10. **Conflicts lower authority.** Contradictory or near-tied evidence normally produces EVIDENCE or MISS, not an arbitrary ANSWER.
11. **Retries are safe.** Durable regulated mutations retain logical request identity and cannot double-credit, duplicate, or silently replace different content.
12. **Observability has bounded cardinality.** User text, raw context fingerprints, statement IDs, and Claim IDs do not become unbounded metric labels.
13. **Absence uses concrete values.** Core, persisted, and transport contracts do not generate `None` or JSON `null`. Each field has one precise concrete type and a documented falsy empty value such as `""`, `[]`, `{}`, `0`, or `false`. When an empty value is also a valid observation, a separate presence or availability field preserves the distinction. Boundary adapters normalize omitted external input immediately, and new core annotations do not use optional union types.

## 6. Target architecture

Engram should expose a single progressive resolution operation while preserving focused lower-level APIs where compatibility requires them.

```text
request + caller context
          |
          v
  QueryFrame construction
  identity, scope, entities,
  relation, qualifiers, history
          |
          v
  bounded retrieval rewrites
          |
          v
  +-------+--------+---------+---------+----------+
  |       |        |         |         |          |
 exact  pattern  lexical  semantic  structured  utility
                               graph      graph
  |       |        |         |         |          |
  +-------+--------+---------+---------+----------+
          |
          v
 normalized Candidates
          |
          v
 validity, scope, support,
 conflict and feature checks
          |
          v
 candidate fusion and policy
          |
     +----+----+
     |         |
     v         v
   ANSWER   EVIDENCE
     |         |
     +----+----+
          |
          v
         MISS when neither threshold is met
```

The pipeline progresses by cost under explicit policy. An eligible exact result can terminate immediately. Other resolvers may run until the configured cost budget is exhausted or sufficient independent evidence has accumulated. Optional resolvers advertise availability and fail without disabling cheaper resolvers.

Section 4 establishes the transport-neutral execution substrate and a deliberately conservative baseline: one unique eligible exact artifact may produce ANSWER, any bounded non-answer candidates or already available graph references produce EVIDENCE without authorizing a response, and no usable output produces MISS. Section 5 replaces that baseline with feature normalization, fusion, ambiguity, calibrated thresholds, and stable policy reasons. Section 7 owns response-less Claim retrieval and packaging, Section 8 owns contextual relation-aware graph interpretation, and Section 15 owns wire exposure. The target diagram includes those later stages, but their behavior is not silently counted as Section 4 completion.

## 7. Core data contracts

Data contracts should be typed internally, transport-neutral in `EngramCore`, serializable, versioned, and additive at adapter boundaries.

### 7.1 ScopeKey

`ScopeKey` defines exact eligibility boundaries. It must not depend on a hash whose inputs are unknown to Engram.

```json
{
  "schema_version": 1,
  "namespace": "support",
  "context_fingerprint": "account-tier:pro"
}
```

Version and provenance filters remain explicit metadata constraints. If a future deployment needs structured scope dimensions, introduce a versioned schema rather than parsing opaque fingerprint text.

### 7.2 QueryIdentity

`QueryIdentity` represents what the request asks. It is separate from the terms used to find candidates.

```json
{
  "schema_version": 1,
  "normalization_version": 1,
  "canonical_form": "birth date of Alan Turing",
  "operator": "when",
  "entities": [
    {"surface": "Alan Turing", "canonical_id": "entity:alan-turing"}
  ],
  "relation": {"surface": "born", "canonical_id": "predicate:date_of_birth"},
  "qualifiers": [],
  "lexical_terms": ["alan", "turing", "born"],
  "scope": {
    "namespace": "biography",
    "context_fingerprint": ""
  }
}
```

Standalone Engram may leave canonical IDs empty and use conservative normalized surfaces. An authoritative writer such as Tapestry may supply richer canonical identity. Engram validates structure and normalization version but does not reinterpret a valid authoritative identity during commit.

The initial operator vocabulary should include `who`, `what`, `where`, `when`, `which`, `why`, `how`, `how_many`, `lookup`, `exists`, `count`, `compare`, and `unknown`. Unknown is preferable to an unjustified inference.

### 7.3 RetrievalRepresentation

A response may have one canonical representation and multiple aliases. Each representation is non-executable.

```json
{
  "schema_version": 1,
  "canonical": "default PostgreSQL port",
  "aliases": [
    "What port does PostgreSQL use?",
    "What is the default PostgreSQL port?",
    "Postgres default port"
  ],
  "normalization_version": 1
}
```

On commit, Engram normalizes each representation into a retrieval key, removes duplicates within the artifact, checks collisions within exact scope, and records the originating representation for diagnostics.

### 7.4 ResolutionBudget and BudgetConsumption

One resolution captures a versioned `ResolutionBudget` before resolver planning. It specifies configured cost-class allowances, candidate count, graph rows, vector results, evidence count and bytes, serialized output and diagnostics, and bounded working-memory estimates. Version 2 contains no elapsed-time answer limits or knowledge deadlines. The injected monotonic clock records resolver and complete-turn duration in `BudgetConsumption.elapsed_ns`; elapsed time never changes candidacy, evidence, outcome, or exhaustion. Section 15 supplies transient caller cancellation and isolates optional graph I/O from the core-wide state lock. Cancellation stops work at cooperative boundaries; a driver call already in progress may leave only that request outstanding and does not reinterpret a slow valid result as a knowledge miss.

The executor owns the mutable consumption ledger rather than placing mutable counters in `QueryFrame`. It reserves and consumes allowances deterministically, issues each resolver a read-only bounded lease, and records completed, truncated, exhausted, skipped, unavailable, or failed consumption without allowing a resolver to increase its own limits. Reservations, releases, remaining allowance, and final `BudgetConsumption` are bounded versioned records suitable for deterministic fixtures. Section-specific resolvers may define tighter limits, but they cannot weaken the shared envelope.

### 7.5 QueryFrame

`QueryFrame` is the immutable runtime interpretation used by resolvers. It contains:

- original and conversationally resolved request text;
- `QueryIdentity`;
- expected object type when known;
- inherited fields and their source turn;
- rewrite chain;
- caller scope and required metadata;
- immutable resolution budget limits; and
- stable diagnostic identifiers.

The trusted core boundary builds and validates one base frame per request. It accepts authoritative or standalone Section 1 identity, verifies scope and representation consistency, captures the eligibility and budget contexts once, and performs shared baseline request preprocessing once so pattern, lexical, and graph adapters do not independently invent different resolved requests. Base construction performs no graph lookup, follow-up inheritance, retrieval rewrite, runtime dependency loading, or transport-specific translation.

Section 4 defines the expected-object-type vocabulary and concrete empty inheritance and rewrite trace containers so the version 1 frame is stable. Section 8 owns contextual population, previous-frame retention, entity and relation resolution, and expected-type inference; Section 11 owns rewrite rule semantics and trace population. The frame is not automatically persisted as user knowledge. A compact previous frame may be kept in user context only through the bounded Section 8 contract.

### 7.6 IndexProjection and IndexState

Section 2 indexes consume a bounded internal projection rather than depending on the Section 3 artifact or lifecycle implementation. An `IndexProjection` contains only the fields required to construct and validate derived indexes:

```json
{
  "schema_version": 1,
  "statement_id": "stmt-...",
  "generation": 1,
  "retrieval_keys": [
    {
      "key": {},
      "provenance": "canonical",
      "representation": "Who acquired GitHub?"
    }
  ],
  "support_claim_ids": ["claim-1"],
  "direct_answer_eligible": true,
  "exclusion_reason": "",
  "normalization_version": 1
}
```

All fields use concrete values. Section 3 projects validated `CachedResponseArtifact` records into this contract and owns the lifecycle, validity, and epoch rules used to calculate `direct_answer_eligible` for a named `EligibilityContext`. Because time and namespace epoch can change without an artifact mutation, this Boolean is a context-stamped derived snapshot rather than a permanent property of the artifact. Exact lookup revalidates it against the request context or atomically refreshes the projection before returning `FOUND`; an expired or epoch-stale artifact cannot remain directly answerable through an old index state. A legacy record with no recoverable identity produces a projection with empty retrieval keys and an explicit exclusion reason; index construction must not manufacture an exact key from lexical keywords or response text.

`IndexState` owns the complete retrieval-key and support-index pairs, their schema and normalization versions, and the associated build and collision reports. Readers observe one completed state. Builders and repair operations construct a candidate state off-live, check it, and swap it atomically rather than mutating individual maps into visibility.

### 7.7 CachedResponseArtifact

The accepted write object should contain at least:

```json
{
  "schema_version": 1,
  "statement_id": "stmt-...",
  "generation": 1,
  "response": "The exact accepted human-facing response.",
  "query_identity": {},
  "retrieval": {"canonical": "...", "aliases": []},
  "tier": "STATIC",
  "lifecycle": "ACTIVE",
  "scope": {"namespace": "...", "context_fingerprint": "..."},
  "support_claim_ids": ["claim-1", "claim-2"],
  "valid_from": "",
  "valid_from_available": false,
  "valid_until": "",
  "valid_until_available": false,
  "knowledge_epoch": 0,
  "knowledge_epoch_available": false,
  "superseded_by": "",
  "provenance": {
    "schema_version": 1,
    "source_label": "tapestry:released",
    "caller_id": "regulator-a",
    "accepted_at": "2026-08-12T16:00:00Z"
  },
  "statistics": {
    "schema_version": 1,
    "hit_count": 0,
    "query_count": 0,
    "last_hit": "",
    "last_hit_available": false
  },
  "metadata": {}
}
```

`response` is stored once and its Unicode scalar sequence survives every codec and migration unchanged. Canonical and alias representations point to its statement ID. Temporal bounds use canonical RFC 3339 UTC text plus explicit presence flags; knowledge epoch is a nonnegative integer plus explicit availability. Provenance and statistics are versioned concrete records, and metadata is bounded immutable JSON without null. Support Claims remain opaque identifiers in standalone storage and are interpreted only through the graph integration contract. The executable field and codec limits are published in [the version 1 artifact contract](documentation/artifacts/artifact-contract-v1.md).

The authoritative live repository is keyed by statement ID. The legacy statement dictionary and statistics interfaces are compatibility projections from that repository rather than competing stores. Every valid artifact can supply an `IndexProjection` for an explicit `EligibilityContext`; the projection is disposable derived input and is not a second authoritative record.

### 7.8 EligibilityContext and EligibilityDecision

One request captures one immutable eligibility context at the trusted core boundary:

```json
{
  "schema_version": 1,
  "evaluation_time": "2026-08-12T16:00:00Z",
  "evaluation_time_available": true,
  "namespace": "tenant-a",
  "knowledge_epoch": 42,
  "knowledge_epoch_available": true,
  "artifact_repository_available": true,
  "epoch_source": "trusted_integration"
}
```

The UTC clock and namespace epoch provider are injected dependencies. Tests supply both directly; core logic does not read ambient time or manufacture an unavailable epoch. Standalone initialization and every monotonic namespace-epoch increment are explicit operations captured in a deterministic snapshot for later coordinated persistence. Stale expected epochs conflict and concurrent increments have one winner. Adapters may transmit candidate values, but only a configured `TrustedEligibilityInput` boundary can establish them. The exact codec, capture paths, increment reasons, and availability rules are published in [the version 1 eligibility-context contract](documentation/artifacts/eligibility-context-v1.md).

Pure eligibility evaluation returns a stable `EligibilityDecision` containing lifecycle base eligibility, request-time direct-answer eligibility, one closed exclusion reason, the captured evaluation time, and the epoch value, availability, and policy used. The fixed evaluation order is repository availability, time availability, namespace, lifecycle, interval ordering and position, then epoch requirements. `REQUIRE_MATCH` requires artifact and context epochs; `MATCH_WHEN_ARTIFACT_AVAILABLE` leaves an epoch-less artifact unconstrained but still requires a matching context for an epoch-bearing artifact. Availability failure abstains; it does not infer truth. These context fields and the deterministic context signature also let exact lookup detect and refresh a projection calculated for an older time or epoch. The executable truth tables and reason vocabulary are published in [the version 1 eligibility-decision contract](documentation/artifacts/eligibility-decision-v1.md).

### 7.9 Candidate, FeatureSet, and EvidenceReference

Every response resolver emits the common versioned `Candidate` contract. Graph-only paths emit minimal versioned `EvidenceReference` records through `ResolverResult` rather than manufacturing a response candidate.

```json
{
  "schema_version": 1,
  "candidate_id": "candidate-...",
  "statement_id": "stmt-...",
  "response": "...",
  "source": "exact",
  "features": {
    "values": {
      "exact_match": 1.0,
      "pattern_specificity": 0.0,
      "lexical_score": 0.0,
      "semantic_score": 0.0,
      "entity_match": 1.0,
      "relation_match": 1.0,
      "support_coverage": 1.0,
      "freshness": 1.0,
      "top_two_margin": 1.0
    },
    "unavailable": ["acceptance_rate", "authority"]
  },
  "evidence": [],
  "scope": {},
  "lifecycle": "ACTIVE",
  "provenance": {},
  "diagnostics": {}
}
```

Unavailable features are omitted from `features.values` and named in `features.unavailable`; an empty feature set is represented by empty containers, not `null`. This distinguishes an unavailable measurement from a measured zero without introducing optional value types. Candidate IDs are proposal-local. Statement and Claim identifiers remain stable evidence references where available. Contracts have deterministic codecs, closed versions, explicit byte and item bounds, deeply concrete diagnostics, and exact accepted-response text preservation.

Section 4 owns only the generic feature container and the minimal evidence-reference shape: stable evidence identifier, source resolver, evidence kind, scope, and bounded provenance or selection diagnostics. Section 5 owns feature names, ranges, normalization, deduplication, agreement, and policy interpretation. Section 7 owns the full response-less Claim record, fixed read projections, current-time disclosure eligibility, initial unfitted usefulness, bounded Tapestry package, and wire-safe core content. Section 9 extends that baseline with requested historical-time, trust-ranking, multi-value, and conflict semantics. The Section 7 path field carries only a singleton Claim; Section 10 owns multi-hop path population and validation.

### 7.10 ResolutionResult

The core returns one of three outcomes:

```json
{
  "outcome": "ANSWER",
  "selected_candidate": {},
  "selected_candidate_available": false,
  "response_candidates": [],
  "evidence": [],
  "confidence": 0.0,
  "confidence_available": false,
  "reason_codes": [],
  "frame_diagnostics": {},
  "resolver_diagnostics": [],
  "budget": {}
}
```

- Section 4 baseline `ANSWER` contains exactly one unique eligible exact artifact. The selected-candidate presence flag is true, its unchanged response is available, and non-exact resolvers were not invoked after the permitted short circuit.
- Section 4 baseline `EVIDENCE` contains bounded non-answer response candidates or already available graph references but authorizes no direct response. It does not claim that the Section 7 Tapestry usefulness or packaging gate has passed.
- `MISS` contains bounded execution diagnostics and reason codes but no selected candidate, response candidates, or evidence references.

Section 4 reason codes describe execution and its conservative baseline decision. Fused confidence, ambiguity, thresholds, and general answer-policy reasons remain unavailable until Section 5 supplies them; availability fields preserve that distinction from numeric zero. Results expose bounded frame diagnostics rather than automatically returning or logging raw original and resolved text. The regulated interface may always treat an Engram `ANSWER` as a proposal that still requires current Tapestry validation. Standalone acceptance policy remains explicit and versioned.

## 8. Identity and exact retrieval

### 8.1 Identity construction

The first identity implementation should be conservative and deterministic:

1. Preserve interrogative and computational operators before stopword filtering.
2. Normalize contractions, Unicode, case, punctuation, symbolic comparisons, technical operators, and whitespace through a versioned function.
3. Extract explicit named entities and technical identifiers without forcing uncertain canonical IDs.
4. Extract a relation surface from the main predicate when confidence is adequate.
5. Preserve negation, comparison, quantity, temporal, and location qualifiers.
6. Produce lexical terms separately using the existing keyword pipeline.
7. Include exact scope in lookup and collision checks.

Externally supplied identity should take precedence at write time. Engram should reject malformed or internally inconsistent identity rather than silently rewriting it.

### 8.2 Collision policy

A retrieval key collision occurs when the same scoped, normalized canonical or alias key points to different active response artifacts.

The default policy should fail the commit with a conflict that names the existing statement IDs. Explicit supersession may replace the active mapping when the caller supplies the expected current statement or generation. Silent last-writer-wins behavior is not acceptable.

The test corpus must include:

- `when` versus `where`;
- `who` versus `what`;
- current versus historical qualifiers;
- positive versus negated requests;
- same terms under different namespaces and context fingerprints;
- singular versus count questions; and
- one alias accidentally assigned to two active answers.

### 8.3 Exact index

The exact ownership and direct-lookup views should be logically equivalent to:

```text
(scope_key, normalization_version, normalized_retrieval_key)
    -> ordered indexable owner IDs
    -> one eligible statement ID, or a typed miss/collision result
```

The derived state keeps two distinct views. An ownership view exposes every indexable artifact carrying a key to collision validation and audit. A direct-lookup view exposes only an eligible, unambiguous mapping. The common case contains one statement; zero is a miss, and more than one is an explicit collision that cannot produce a direct answer. Deterministic ordering supports reproducible reports and repair but must never select a winner.

Exact lookup returns a concrete typed result such as `FOUND`, `MISS`, or `COLLISION` with bounded statement IDs and canonical-versus-alias provenance. Section 3 supplies a context-stamped eligibility decision through `IndexProjection` and revalidates or refreshes it before a direct result; Section 4 adapts a current `FOUND` result into the common candidate contract.

Exact lookup should bypass tokenization, synonym expansion, vector encoding, and graph access when an eligible unambiguous result is found.

## 9. Persistence and secondary indexes

Persisted response artifacts remain authoritative. Section 2 consumes validated `IndexProjection` values so it does not depend on the later artifact, lifecycle, persistence, or transport implementations. The following secondary index pairs are built and maintained together under one re-entrant core mutation boundary:

| Index | Purpose |
| --- | --- |
| scoped retrieval key to statement IDs | Constant-time ownership, canonical and alias lookup, and explicit collision discovery. |
| statement ID to retrieval keys | Efficient generic replacement, removal, audit, and exact-index validation. |
| Claim ID to statement IDs | Support-aware semantic lookup proportional to matching Claims and fan-out. |
| statement ID to Claim IDs | Efficient support replacement, removal, audit, and reverse-index validation. |
| sparse field index | Optional BM25 or FTS retrieval over canonical, alias, entity, relation, identifier, and answer fields. |

The exact forward and inverse maps form one invariant, as do the support forward and inverse maps. Neither half is independently mutable or considered complete. Section 2 provides:

- an explicit lookup operation for exact keys and matched Claim IDs;
- deterministic off-live construction from validated projections;
- a consistency checker that reports missing, extra, asymmetric, ineligible, unindexable, and conflicting mappings;
- distinct self-check and explicit authoritative-comparison operations so an empty authoritative projection set is not confused
  with omitted input, plus validation of every build diagnostic reproducible from retained projections;
- schema and normalization version recording in `IndexState`;
- one atomic swap from a completed and checked candidate state;
- generic add, replace, remove, and support-update mutations whose result equals a clean rebuild; and
- tests that abandon candidate builds or inject stale live state without exposing partial maps to readers.

Index construction classifies legacy records rather than guessing. Missing identity, unsupported versions, malformed support, lifecycle exclusion, within-artifact duplicate representations, and cross-artifact collisions have distinct bounded reason codes. Duplicate canonical or alias representations inside one artifact are deduplicated; a key owned by multiple eligible artifacts remains a collision group and is omitted from direct lookup.

The existing support-aware semantic path must consume the Claim-to-statement index after vector Claim matching. Its work after graph lookup must scale with matched Claim IDs and their response fan-out rather than iterating the statement corpus. Scope and other caller filters are applied before bounded top-k selection. A configured Claim-edge scan bound applies to the complete matched fan-out; exhaustion returns no partial candidate set and abstains with a stable diagnostic.

Section 2 owns the transport-neutral check, rebuild, dry-run diff, explicit repair, and live-state mutation primitives. Section 3 owns the artifact repository, projection and request-time revalidation, commit-time rejection, lifecycle composition, tier admission and eviction, feature serializers and migration, durable mutation receipts, and rebuilding compatibility views and indexes from authoritative artifacts. Section 15 integrates those feature contracts into cross-feature schema management and startup/readiness, and owns adapter exposure, authorization, backups, downgrade, and operator procedures. Section 16 owns release-scale performance and failure gates; Section 2 still supplies engineering complexity and resource benchmarks.

Section 2 was completed and independently remediated on 12 August 2026. The implementation is in `engram/indexes.py`, with the core mutation boundary and support-aware lookup integration in `engram/core.py`, eviction synchronization in `engram/eviction.py`, and current persistence compatibility rebuilding support-only legacy projections in `engram/persistence.py`. The remediation prevents pre-filter fan-out truncation, abstains on scan exhaustion, gives explicit empty authoritative sets precise check and repair semantics, and rejects reproducible diagnostic-report corruption before publication. The exact contract, bounds, lock order, checker semantics, and transport-neutral repair behavior are documented in [the version 1 index contract](documentation/indexes/contracts-v1.md). Its [classification fixture](documentation/indexes/classification-v1.json), [verification report](documentation/indexes/test-results-2026-08-12.md), and [100,000-projection and full-proposal benchmark](documentation/indexes/benchmark-2026-08-12.json) provide the required invariant, concurrency, compatibility, complexity, and memory evidence. All applicable ADR 0004 Section 2 engineering gates passed, including the full 5,000-artifact support-aware proposal's absolute and baseline-relative gates; Section 16 still owns held-out release evaluation.

Persisting a secondary index is an optimization, not a second source of truth. If a future index snapshot is missing, incompatible, or corrupt, Engram rebuilds from authoritative artifacts before declaring the affected resolver ready. The initial implementation keeps `IndexState` rebuildable in memory as required by ADR 0003.

## 10. Accepted response commit and lifecycle

### 10.1 Lifecycle domain and transition policy

The lifecycle vocabulary and legal transition matrix land before artifact projection or mutation operations. The initial lifecycle states are:

| State | Base eligibility | Legal lifecycle transition | Meaning |
| --- | --- | --- | --- |
| ACTIVE | Eligible when request-time checks also pass. | SUPERSEDED, INVALIDATED, or RETIRED through their named operations. | Current response artifact. |
| SUPERSEDED | Ineligible. | None in version 1. | Replaced by a named newer artifact. |
| INVALIDATED | Ineligible. | None in version 1. | Known to be unsupported, false, unsafe, or outside its validity contract. |
| RETIRED | Ineligible. | None in version 1. | Administratively removed from service without asserting a replacement or falsehood. |

Tier controls admission and residency, not truth. Capacity eviction is not a lifecycle transition and cannot mark an artifact invalid. Only the explicit supersession operation may create replacement lineage or set ACTIVE to SUPERSEDED; invalidation and retirement cannot be used as generic replacement verbs. The contract must also specify whether and how historical retrieval keys may be reused, including expected owner and generation checks, so base commit never silently reactivates or overwrites history.

### 10.2 Artifact, context, and eligibility

The deterministic artifact codec implements the concrete fields in Section 7, including exact response text, explicit temporal presence, integer epoch availability, generation, provenance, and statistics. Lifecycle base eligibility is artifact state. Temporal and epoch eligibility are pure functions of the artifact and one injected `EligibilityContext` captured for the request.

Temporal eligibility uses half-open bounds when supplied: `valid_from <= evaluation_time < valid_until`. Bounds must be well ordered. Knowledge epoch mismatch follows configured policy and is never interpreted as a date comparison; unavailable required time, repository, or epoch state produces a stable abstention reason. Boundary truth tables cover the exact start, exact end, missing bounds, unavailable epoch, mismatch, and every lifecycle state.

An `IndexProjection` records the decision for its context, but passage of time and a namespace epoch increment do not mutate the artifact. `ContextualExactLookup` snapshots the index, retrieves every bounded owner, requires each authoritative artifact, reevaluates them under the captured context, and invokes Section 2's generic atomic refresh against the exact state generation. Refresh may alter only eligibility and its reason; missing authority, owner-set changes, retry exhaustion, and malformed refresh data abstain by error. Only the post-refresh lookup may return `FOUND`. The executable cross-section boundary is published in [the version 1 projection-refresh contract](documentation/artifacts/projection-refresh-v1.md).

### 10.3 Authoritative repository and compatibility views

The live `ArtifactRepository` is the sole in-memory authority and supports statement-ID lookup plus construction and atomic publication of a complete immutable `RepositoryState`. The existing statement dictionary, statistics access, and other compatibility shapes are deeply frozen derived views returned to legacy callers as detached copies. A conservative rebuild carries complete identity and support ownership but requires contextual eligibility refresh before a direct result. The repository checker proves equal artifact/view/projection ID sets, exact view derivation, identity, generation, retrieval, support, and Section 2 index consistency. The executable state and compatibility contract is published in [the version 1 repository contract](documentation/artifacts/repository-v1.md).

Repository ownership defines one lock order shared with Section 2: repository re-entrant lock, then the private index-owner lock. Readers using repository APIs see one completed repository/view/index state. Optimistic swaps require the exact repository generation and a candidate exactly one generation later. DYNAMIC admission is strictly bounded: when protected entries leave too few victims, it returns `REJECTED_CAPACITY` instead of growing over capacity or dropping the incoming artifact. FIFO, LRU, LFU, and hit-rate ordering use authoritative statistics with statement-ID tie breaks; untouched entries are not protected by their default hit-rate value. Migrated over-capacity state removes enough victims. STATIC does not consume DYNAMIC capacity and cannot be an ordinary victim. Every plan rebuilds repository views and indexes, preserves lifecycle, and reports the namespaces whose accepted-artifact availability changed for later coordinated epoch increments. The policy is published in [the version 1 tier-admission contract](documentation/artifacts/tier-admission-v1.md).

### 10.4 Durable receipts and feature persistence

Every mutating request has a deterministic bounded `MutationReceipt` containing request identity, closed operation, lowercase SHA-256 signature of the complete canonical payload, result code and concrete result, ordered affected statement generations, completion state, sequence, and creation time. An exact completed retry returns the recorded result without a second mutation or checkpoint; prepared state returns `IN_PROGRESS`. Reuse with another operation or payload returns `CONFLICT` without result disclosure. Live receipts and recently pruned tombstones have separate retention bounds: a matching tombstone returns `EXPIRED` and cannot be reapplied; after tombstone aging the request is outside the advertised retry horizon. The complete ledger snapshot preserves replay, conflict, in-progress, expiry, and next-sequence behavior across restart. The contract and configuration obligations are published in [the version 1 mutation-receipt contract](documentation/artifacts/mutation-receipts-v1.md).

Persistence version 2 embeds an exact `response_state` containing authoritative artifacts, namespace epoch snapshot, complete receipt/tombstone ledger, and bounded quarantine. Compatibility views and every response index are omitted and rebuilt at startup; an artifact-derived view replaces a matching legacy view, while a pattern/artifact ID conflict blocks readiness. Version 1 remains readable. Migration preserves exact response text, tier, scope, aliases, support, provenance, statistics, and non-contract metadata; initializes each recovered namespace epoch to zero; and never fabricates receipts. Missing or malformed identity retains the legacy statement but leaves it exact-unindexed with a concrete quarantine reason. Ambiguous recovered keys retain all owners, mark each record ambiguous, and return COLLISION rather than assigning a winner. `migrate_persistence_state` is non-mutating and idempotent. The schema, fixtures, classification, failure behavior, and recovery procedure are published in [the persistence v2 contract](documentation/artifacts/persistence-v2.md).

Section 3 owns these codecs, the v1-to-v2 feature transformation, quarantine classification, and startup derivation of response views and indexes. EGR-1505 owns cross-feature schema orchestration, readiness, explicit backup or migration-output experience, downgrade constraints, and operator reporting.

### 10.5 Atomic mutation coordinator

All Section 3 mutations use one coordinator. It validates a request and receipt, builds an off-live candidate containing artifact repository state, compatibility views, Section 2 indexes, namespace epoch effects, and the completed receipt, checks their equivalence, and performs exactly one configured checkpoint.

The default durable sequence checkpoints the complete candidate before publishing it atomically to readers. A definite checkpoint failure leaves live state unchanged and returns no success. An indeterminate storage outcome is resolved by reloading durable state and its receipt before responding or retrying. A crash after durable checkpoint but before live publication converges on restart, and no second post-publication checkpoint is attempted. In-memory mode uses the same state machine without claiming durability. Fault-injection tests cover failures before write, during atomic replacement, after durable replacement, and during publication or recovery.

The executable ownership, lock-order, failure-classification, recovery, and visibility rules are published in [the atomic mutation coordinator contract v1](documentation/artifacts/mutation-coordinator-v1.md).

### 10.6 Base commit

Introduce a transport-neutral operation conceptually equivalent to:

```python
commit_response(
    artifact,
    request_id,
)
```

The operation should:

1. validate response, identity, aliases, scope, tier, lifecycle, support, temporal fields, epoch fields, metadata bounds, and request identity;
2. reject empty and complete normalized `IDK` responses;
3. preserve the Unicode scalar sequence accepted by the caller exactly;
4. reject scoped canonical or alias collisions with stable named owner IDs;
5. create one new artifact or return the receipt for an exact retry;
6. use the atomic coordinator to update the repository, compatibility views, and all Section 2 indexes; and
7. return stable created, unchanged-retry, conflict, or failure results and identifiers.

Base commit never supersedes or replaces an existing artifact. Any historical-key reuse permitted by the lifecycle policy must use the explicit replacement operation with expected identity and generation.

The executable validation, collision, idempotency, capacity, and durability rules are published in [the accepted-response base commit contract v1](documentation/artifacts/base-commit-v1.md).

### 10.7 Lifecycle and supersession operations

Invalidation and retirement are separate transport-neutral operations. Each requires typed reason, caller identity or provenance, request identity, expected generation, authorization at the applicable boundary, and audit fields. Engram enforces the requested legal transition but does not autonomously decide truth. There is no generic public setter that can assign SUPERSEDED.

The executable terminal-transition, audit, retry, and concurrency rules are published in [the audited lifecycle mutation contract v1](documentation/artifacts/lifecycle-mutations-v1.md).

Supersession is the only replacement path. It requires the expected current statement ID and generation, validates and creates the replacement artifact, links lineage, changes the visible retrieval mappings, and records both generations and the result in one coordinated mutation. Stale or competing writers receive stable conflicts; it never falls back to implicit last-writer-wins behavior.

The executable identity-reuse, lineage, capacity, audit, concurrency, and restart rules are published in [the explicit supersession contract v1](documentation/artifacts/supersession-v1.md).

### 10.8 Compatibility and conformance

`LearnResponse` remains a backward-compatible convenience that constructs a DYNAMIC ACTIVE artifact and calls base commit while preserving proposal accounting, retry identity, user-context, and complete normalized `IDK` rejection. It does not acquire implicit supersession semantics. Tapestry can migrate to richer transport operations when Section 15 wire contracts are available.

The executable wrapper, durable retry, exact proposal, support, accounting, and failure rules are published in [the LearnResponse compatibility contract v1](documentation/artifacts/learn-response-compatibility-v1.md).

Section 3 closed on 2026-08-12 after fault-injected concurrency, restart, persistence failure, migration, clock-boundary, epoch-change, eviction, STATIC-retention, receipt-retry, collision, and supersession tests proved repository/view/index/receipt equivalence. Applicable Section 2 complexity and resource gates passed on a fresh rerun, and a 1,000-turn official MCP protocol conversation passed. The [Section 3 conformance report](documentation/artifacts/section3-conformance-2026-08-12.md) and [recovery runbook](documentation/artifacts/section3-recovery-runbook.md) record the evidence and operator response. Section 16 retains held-out release policy and value gates.

## 11. Resolver framework

The resolver framework separates contracts, retrieval discovery, execution, accounting, and decision policy. A deterministic registry builds one bounded execution plan from configured resolvers and the validated base frame. The budgeted executor runs that plan; the accounting finalizer applies observations only after aggregation; and orchestration produces the conservative Section 4 `ResolutionResult`. A resolver does not own any of those outer concerns.

Each resolver implements a small side-effect-free contract:

```python
class Resolver(Protocol):
    name: str
    cost_class: CostClass

    def available(self, frame: QueryFrame) -> bool: ...
    def resolve(self, frame: QueryFrame, budget: ResolverBudget) -> ResolverResult: ...
```

`available` and `resolve` do not increment query, candidacy, hit, success, feedback, or durable statistics; mutate conversation state; checkpoint; or reinterpret the frame. `ResolverResult` contains candidates, minimal evidence references, strict full-Claim producer records, accounting observations, diagnostics, elapsed time, consumed budget, and one typed completed, unavailable, skipped, exhausted, or failed state. Full producer records are internal to orchestration and appear in the unified result only through its bounded package. Resolver exceptions are isolated and translated without erasing results from successful resolvers. Legacy lower-level APIs remain compatibility wrappers around the pure discovery primitives and their explicit legacy side effects.

The initial adapters must extract retrieval from existing mutation-heavy paths before claiming conformance. In particular, the existing pattern fallback calls graph recall and returns a statementless pattern-shaped result. Structured graph extraction therefore precedes completion of the pure pattern adapter even though graph execution remains later in the runtime cost order.

### 11.1 Initial resolver set

| Resolver | Initial behavior |
| --- | --- |
| `ExactResolver` | Scoped canonical and alias lookup with current repository, metadata, source, lifecycle, validity, and epoch checks. |
| `PatternResolver` | Existing AIML-style statement match exposed as a candidate without graph fallback or discovery-time accounting. |
| `RewriteResolver` | Applies bounded retrieval-only normalization rules and records the chain. |
| `LexicalResolver` | Existing keyword, lemma, stem, synonym, spelling, phrase, recency, and hit-aware retrieval without discovery-time accounting. |
| `GraphStructuredResolver` | Existing read-only bounded graph recall exposed as evidence references; canonical relation-aware plans arrive in Section 8. |
| `GraphSemanticResolver` | Existing support-aware Claim vector lookup returning support-linked accepted-response candidates; response-less evidence arrives in Section 7. |
| `StandaloneSemanticResolver` | Optional dense retrieval over cached request representations. |
| `UtilityResolver` | Deterministic bounded computations registered by type. |

Section 4 conformance covers the currently implemented exact, pattern, lexical, existing structured graph, and support-semantic paths. The registry and protocols are extensible, but rewrite, standalone semantic, relation-aware graph, response-less evidence, and utility implementations are not prerequisites for Section 4 completion; they remain in Sections 11, 13, 8, 7, and 14.

### 11.2 Centralized accounting

Every pure resolver returns accounting observations rather than applying them. After aggregation and complete-result output-budget fitting, the accounting finalizer records each unique proposed statement once even when multiple resolvers emitted it, records no speculative success, and records an accepted success exactly once through the authoritative Section 3 artifact boundary or the explicit legacy compatibility boundary. An exact candidate downgraded to MISS because its result cannot fit is counted as candidacy, never as accepted success. Regulated retries retain their existing identity and cannot double-credit a proposal or result. Section 6 later adds typed feedback observations; it does not replace Section 4 candidacy and success accounting.

### 11.3 Resolver ordering, plans, and execution budgets

The default order is:

1. exact;
2. retrieval rewrite plus exact retry;
3. pattern and lexical;
4. optional standalone semantic;
5. structured graph;
6. semantic graph; and
7. utility resolvers when query classification makes one applicable.

Deployments may reorder resolvers by measured latency and value, but exact and bounded hard-eligibility checks remain first. The registry records configuration, availability, skips, order, and cost-class decisions in one inspectable bounded plan. Section 4 permits a unique eligible exact result to short-circuit; until Section 5 is implemented, no non-exact response candidate is promoted directly to ANSWER.

The executor owns the mutable budget ledger and enforces candidates, graph rows, vector results, evidence and output sizes, diagnostics, bounded working-memory estimates, and cost-class allowances. The injected monotonic clock measures each resolver and the complete resolution but does not stop execution or reject a returned result. Deterministic truncation is explicit. An unavailable, skipped, resource-exhausted, or failed resolver produces typed diagnostics and cannot erase earlier completed results or prevent permitted later work while non-time resource allowance remains.

## 12. Candidate fusion and answer policy

The first fusion implementation should be transparent and hand-authored. It should normalize resolver-specific scores, expose every feature, and produce stable reason codes.

Candidate features should include, when available:

- exact canonical or alias match;
- pattern specificity;
- lexical overlap and field contributions;
- semantic similarity;
- entity and relation agreement;
- expected object-type agreement;
- support similarity and support coverage;
- current support validity;
- statement and query-relationship acceptance history;
- freshness and lifecycle eligibility;
- Claim authority and trust inputs supplied by the graph;
- independent resolver agreement; and
- the top-one versus top-two margin.

Fusion must not simply select the largest score from unrelated scales. A candidate supported by independent exact, lexical, and graph signals should gain confidence. A close competing candidate, relation mismatch, incomplete support, contradiction, stale state, or scope uncertainty should lower the result.

Policy proceeds in this order:

1. Remove ineligible lifecycle, scope, metadata, temporal, and support candidates.
2. Group duplicate statement candidates emitted by multiple resolvers.
3. Combine features and resolver agreement.
4. Detect contradictions and near-ties.
5. Apply an ANSWER threshold and minimum margin.
6. If no answer qualifies, apply the EVIDENCE usefulness policy.
7. Otherwise return MISS.

Section 4 exact short-circuiting still performs every hard repository, scope, metadata, source, lifecycle, validity, and epoch check before producing its conservative baseline ANSWER. Section 5 centralizes those non-bypassable checks for every candidate source before fusion and adds support, ownership visibility, agreement, ambiguity, and configurable policy gates; it does not weaken or duplicate the Section 3 authoritative eligibility decision.

Thresholds and coefficients belong in versioned configuration and evaluation artifacts. They must not be tuned on the release test set.

## 13. Feedback and negative resolution

### 13.1 Typed feedback

Track both statement-level and query-relationship observations:

```text
candidate_count
accept_count
rejected_quality
rejected_context
rejected_stale
rejected_policy
```

One versioned transport-neutral feedback operation should bind each observation to an immutable target: the authoritative request or proposal reference, typed query identity and normalization version, exact `ScopeKey`, canonical bounded constraint fingerprint, statement ID and generation when applicable, observation kind, external Regulator outcome, relevant contract and policy fingerprints, and injected observation time. Candidate observations should consume Section 4's already deduplicated accounting set. Verdict receipts must be durable, idempotent, and conflict-detecting so transport retries, restarts, and checkpoint recovery cannot double-credit an observation. Engram's own ANSWER selection is not an external acceptance label.

Statement history is keyed by statement generation rather than response text. Query-relationship history additionally includes the exact identity, scope, a canonical bounded fingerprint of relevant request constraints, and the version partition. `ScopeKey` already contains namespace and context fingerprint; implementations should not create a looser parallel scope representation. Existing artifact hit and query counters remain accepted-use compatibility statistics unless an explicit migration places them in a named legacy partition; they are not silently reinterpreted as typed Regulator feedback.

The initial application policy should be:

| Outcome | Effect |
| --- | --- |
| `accepted` | Strengthen the scoped query-to-statement relationship and the statement's reliability history. |
| `rejected_quality` | Weaken the statement broadly, subject to policy/version partitioning. |
| `rejected_context` | Weaken only the observed query, scope, or context relationship. Do not retire globally valid content. |
| `rejected_stale` | Exclude the targeted generation through feedback state and request an authoritative expected-generation lifecycle decision through Section 3. |
| `rejected_policy` | Suppress within the relevant namespace or policy version. Do not infer global falsehood. |

Raw aggregate counters and bounded time buckets should be retained for inspection rather than an unbounded observation event log. Derived acceptance features should use an injected clock, deterministic aging and compaction, minimum sample sizes, explicit availability, and bounded priors so one outcome cannot dominate permanently. Operational metrics must not use raw query, statement, namespace, or context identifiers as unbounded labels.

After sufficient labeled traffic exists, an offline evaluation may compare the hand-authored formula with logistic regression or a small learning-to-rank model. A learned model must be versioned, locally available, explainable through feature output, and no less conservative on the false-direct-answer gate.

### 13.2 Negative resolution

A short-lived negative record may memoize that the same scoped request could not be resolved under the same knowledge and resolution-policy state:

```json
{
  "schema_version": 1,
  "query_identity": {},
  "scope": {
    "schema_version": 1,
    "namespace": "...",
    "context_fingerprint": "..."
  },
  "constraint_fingerprint": "sha256:...",
  "knowledge_epoch": 42,
  "knowledge_epoch_available": true,
  "resolver_plan_fingerprint": "...",
  "capability_readiness_fingerprint": "...",
  "policy_fingerprint": "...",
  "reason": "insufficient_knowledge",
  "expires_at": "..."
}
```

The lookup key includes the typed query identity, exact `ScopeKey`, a canonical bounded constraint fingerprint, an available knowledge epoch, and the normalization, resolver-plan, capability-readiness, and policy fingerprints; `reason` and expiry are record values. The first implementation is memory-only, fixed-TTL, capacity-bounded, deterministically evicted, and non-sliding so repeated hits cannot preserve a miss indefinitely. An unavailable knowledge epoch cannot admit or reuse a negative record. Because that epoch currently versions accepted-response mutations rather than every lexical, pattern, or graph source, version 1 admits and reuses only exact-only plan misses; non-exact plans run normally and invalidate related records. Persistence requires separate benchmark and migration justification.

Negative records are not statements, facts, evidence, or `IDK` answers. Admission is limited to `insufficient_knowledge` after the configured resolver plan completed sufficiently to establish a knowledge miss with required authoritative dependencies available. Policy, safety, authorization, transport, dependency-unavailable, timeout, exhaustion, truncation, and indeterminate failures are not reusable knowledge misses. Lookup occurs only after trusted frame construction and knowledge-state capture; a hit may bypass expensive resolvers but must return a typed bounded MISS with accurate consumption, no candidacy or success accounting, and fail-open behavior that never blocks the ordinary Actor or Tapestry path. Relevant epoch, normalization, resolver-plan, readiness, or policy changes invalidate the record.

The version-1 implementation and conformance evidence are complete. The executable targeting, 1,000-observation receipt signing, aggregation, aging, fusion-history, lifecycle-handoff, negative-admission, budget enforcement, persistence/recovery, and inspection rules are specified in [the Section 6 contract](documentation/feedback/contracts-v1.md), with final verification and 5,000-record scale results in [the Section 6 conformance record](documentation/feedback/section6-conformance-2026-08-16.md). Adapter exposure and cross-feature configuration remain Section 15 work, and empirical formula calibration remains Section 16 work.

## 14. Evidence-only handoff

Section 4 can carry minimal references produced by the existing graph path so graph recall is no longer disguised as a pattern response. Section 7 first reconciles ADR 0003 with the already strict exact-field minimal-reference and result codecs, then introduces strict full-Claim and package contracts before changing a resolver. Fixed read-only structured and vector projections supply only allow-listed fields. Response-less producers may retain a Claim only after the current disclosure and usefulness policies pass; the final bounded package produces EVIDENCE rather than MISS without authorizing an Engram answer.

Evidence records should include:

- Claim ID;
- source resolver;
- similarity or structured-match features;
- canonical subject, predicate, and object identifiers when the graph contract permits them;
- current validity and supplied trust fields plus explicit availability;
- the exact disclosure scope or trusted visibility-decision provenance;
- one singleton Claim path, with multi-hop population deferred to Section 10; and
- reason the evidence was selected.

The package follows ADR 0003's fixed first-generation envelope: at most ten evidence records, at most 64 KiB complete serialized size, 256-byte identifiers, at most sixteen selection reasons per record, canonical ordering, Claim-ID deduplication, and explicit retained, omitted, and truncation fields. New discovery also consumes Section 4's graph-row, vector-result, output, diagnostic, and working-memory budgets rather than redefining them as Section 7 limits. Similarity and supplied trust participate only through one versioned conservative hand-authored evidence policy; Section 7 may apply a trust inclusion floor but does not rank Claims by trust or grant direct-answer authority. Relative trust policy arrives in Section 9 and empirical calibration remains Section 16 work.

Current disclosure eligibility uses the frame's injected evaluation time and requires active/system-current state, inclusion in the current valid-time interval, proof-canonical identity, and retrieval-only exclusion. Explicitly public Claims follow the documented public rule. Company/customer Claims require a configured trusted visibility authority that maps the caller's exact `ScopeKey`; namespace or context text alone never establishes authorization. Missing trust remains unavailable rather than being silently defaulted. Section 9 later adds requested historical-time selection, trust ranking, multi-value relations, and conflict policy without weakening this disclosure boundary.

The existing CLI, MCP, and gRPC contracts do not transport the unified Claim package. Section 7 evolves the pre-exposure core shape in place. CLI and MCP remain unchanged. Section 15 may stabilize a Python adapter and must introduce an explicitly reviewed versioned gRPC RPC/message/service before external Claim-package exposure; it also owns authorization, redaction, cancellation, generated stubs, and deployment. Section 16 independently measures useful evidence and avoided Tapestry retrieval or token work; Section 7 conformance fixtures establish invariants and engineering bounds only.

## 15. Contextual query-frame enrichment and multi-turn completion

User context retains a compact previous query frame containing operator, subject entities, relation, expected object type, and qualifiers. Follow-up completion inherits only missing fields.

Examples:

```text
What was Microsoft's 2025 revenue?
2024?
```

The second frame inherits subject, relation, and operator, replaces the temporal qualifier, and records each inherited field.

```text
Tell me about PostgreSQL replication.
And MySQL?
```

The second frame inherits the relation or topic and replaces the subject.

Inheritance must be bounded by turn distance, topic continuity, and confidence. A self-contained request does not inherit. Ambiguous completion lowers confidence or returns MISS. User-isolated frames remain part of conversation state and follow existing TTL and persistence rules.

The current boundary is documented in [Contextual relation contracts v2](documentation/contextual/contracts-v2.md). The retained frame is an exact, compact session codec rather than learned knowledge; it uses a two-turn maximum distance and existing session isolation, persistence, and expiry. `EngramCore.resolve_request` accepts an additive keyword-only `user_id`, while Section 15 continues to own any stable external evidence/context adapter.

## 16. Relation-aware graph resolution

Structured graph resolution progresses through the following stages:

1. Classify the operator.
2. Resolve subject and named entities through canonical labels, aliases, and edge surface forms.
3. Generate predicate candidates from dependency structure, verb lemmas, prepositions, Predicate labels, and configured synonyms.
4. Infer an expected object type such as PERSON, PLACE, DATE, NUMBER, ENTITY, or BOOLEAN.
5. Execute a parameterized one-hop lookup against active canonical Claims.
6. Filter by lifecycle, validity, ownership visibility, trust policy, and expected object type.
7. Phrase a response only when one unambiguous eligible result passes the answer policy.
8. Otherwise return bounded Claim evidence.

No caller-supplied Cypher participates in this resolver. Query templates and allowed predicates are internal and parameterized. Graph account permissions remain read-only.

Version 1 exposes three fixed graph capabilities: canonical entity surface lookup, canonical Predicate surface lookup, and `one_hop_claim_v1`. Entity and Predicate resolution return explicit selected, ambiguous, or miss states. The one-hop result reuses the Section 7 `ClaimProjection`, current disclosure evaluator, by-ID revalidation, `ClaimEvidenceRecord`, and package. A unique compatible result may add a deterministic response candidate, but the Section 5 support and independent-source policy still prevents a lone graph result from authorizing `ANSWER`.

The versioned [relation and follow-up corpus](eval/section8-relation-followup-v1.json) separates development and engineering-regression cases. Its legacy `held_out` key does not imply independent custody: all content is visible in this repository and is ineligible as Section 16 release evidence. Its reproducible [benchmark runner](scripts/benchmark_contextual_graph.py) measures every turn and binds the generated report to governed source state while recording direct relations, ambiguity, paraphrases, technical references, bounded elliptical inheritance, context reset, unknown types, plan bounds, and end-to-end duration.

## 17. Temporal, trust, and conflict semantics

### 17.1 Temporal interpretation

The first temporal parser should cover:

- `currently` and `now`;
- `as of <date or year>`;
- `in <year>`;
- `before` and `after`;
- bounded `between`; and
- `latest`.

The query frame should distinguish requested valid time from system observation time. Graph filtering must use the schema's half-open valid-time and system-time semantics. If the query cannot be mapped safely, Engram returns evidence or a miss.

Implemented by the [Temporal, Trust, and Conflict Contracts v1](documentation/temporal/contracts-v1.md): exact supported grammar reports deterministic recognition confidence `1.0`, unresolved text reports `0.0`, and both the source expression and normalized bounds remain inspectable. Current, historical, bounded, and latest requests reuse the same canonical proof, visibility, and by-ID revalidation path.

### 17.2 Trust and ownership

Engram may consume trust and ownership fields supplied by canonical Claims. It does not assign or upgrade them. Visibility is an eligibility filter. Trust is a ranking and direct-answer policy input. Missing trust is explicit and follows configured conservative behavior.

The initial policy has no invented trust threshold. It compares only values supplied with the same available source trust-score version. Missing or version-incomparable trust suppresses a direct relation phrase while retaining bounded Claim evidence.

### 17.3 Conflict handling

A conflict exists when simultaneously eligible canonical Claims map the same subject and predicate to incompatible canonical objects for the requested temporal and scope frame.

Engram should:

1. preserve all conflicting Claim IDs;
2. distinguish an actual contradiction from multi-valued relations;
3. suppress arbitrary direct phrasing;
4. return EVIDENCE with a conflict reason when useful; and
5. expose trust, temporal, and provenance features without declaring which Claim is true.

The optional canonical Predicate `cardinality` property supplies `SINGLE` or `MULTI`; missing metadata is `UNKNOWN` and fails conservatively. Overlapping incompatible `SINGLE` objects preserve all conflict Claim IDs and suppress phrasing. `MULTI` is retained as valid multi-value evidence, while successive non-overlapping historical objects are not mislabeled as a simultaneous contradiction. Stable reasons and the full policy are documented in the [Section 9 conformance report](documentation/temporal/section9-conformance-2026-08-20.md).

The final MCP evaluation uses a no-catch-all test seed so five prompts in Sarah's 1,000-turn sushi conversation demonstrably reach the configured MemGraph. All turns pass; turn lengths are reported as observations only.

## 18. Bounded graph composition

Composition begins only after one-hop relation resolution meets its accuracy requirements and its observed turn lengths have been reported. The initial algebra is:

```text
LOOKUP  EXISTS  COUNT  AND  OR  NOT  MIN  MAX  ORDER
```

Every query plan must declare:

- `max_hops`;
- `max_rows`;
- `max_branches`;
- `max_candidates_per_step`;
- maximum evidence-path size.

There is no composition latency answer budget and Engram does not require a MemGraph timeout. Cooperative cancellation remains required and complete-turn durations are reported as observations. A blocked database driver call cannot be interrupted by an Engram traversal check, so the current request remains outstanding; optional graph I/O releases the core-wide state lock so unrelated local work continues, and an operator may disable new graph work or stop the process through its supervisor. The first release favors one and two hops. It rejects cycles, unconstrained predicates, cartesian expansion, unsupported aggregation, and result truncation that could change the answer. Each result carries the Claim path used to derive it.

`Where was Microsoft's founder born?` is a valid target only when Engram can resolve both `founded_by` and `born_in`, enforce scope and temporal validity at each hop, and return the two-Claim path. Otherwise it returns the partial path as evidence or abstains.

The version-1 implementation is documented in [the composition contract](documentation/composition/contracts-v1.md). It emits only fixed canonical one-hop capabilities, supports repeated Predicate identities at distinct query positions, revalidates every Claim before publication, and advances composed evidence to Claim record schema 2/package wire 2. The [frozen evaluation](documentation/composition/benchmark-2026-08-20.json) reports 15/15 passing cases and observed turn lengths without a timing gate. The [configured live MemGraph probe](documentation/composition/live-memgraph-2026-08-20.json) retains Sarah → Abraham → Keturah as an ordered two-Claim evidence path while rejecting the competing Sarah cycle.

## 19. Retrieval expansion after the foundation

### 19.1 Symbolic retrieval rewrites

Retrieval rewrites reduce surface variation before any resolver without selecting a conversational response. They are separate from AIML template redirects.

Rules should be independently authored, versioned, deterministic, depth-limited, cycle-checked, and traced. Initial classes include contraction normalization, question normalization, paraphrase reduction, pronoun transformations, synonym classes, conversational repair, context-dependent reductions, and technical phrasing normalization.

Historical AIML and ALICE behavior may provide a taxonomy of transformations, but historical implementation source or response corpora must not be copied into Engram.

The version-1 implementation is documented in [the symbolic rewrite contract](documentation/rewrite/contracts-v1.md). It is disabled by default, eagerly validates a 23-rule packaged corpus when enabled, runs after contextual enrichment, accepts only a fixed point into resolver planning, retains the complete `QueryFrame` chain, and keeps AIML matching on the pre-rewrite representation. The [repository-visible engineering comparison](documentation/rewrite/benchmark-2026-08-20.json) improves exact recall from 3/23 to 23/23 with zero semantic collisions and zero false direct answers; the data is component conformance evidence, not protected Section 16 release evidence. The final official-MCP run evaluates and passes all 1,000 Sarah preference turns with five live MemGraph replies.

### 19.2 Sparse retrieval

Section 12 selects a configuration-gated, project-owned fielded BM25 implementation after comparing the existing IDF overlap, unfielded BM25, SQLite FTS5, and the selected engine. Its versioned document projects canonical request, retrieval aliases, entities, relation, keywords, and punctuation-preserving technical identifiers; response text is excluded by default and, when explicitly selected, carries the lowest field weight. Bounded phrase, proximity, prefix, character-trigram, and exact-identifier signals feed the common candidate and diagnostic model without bypassing shared eligibility or fusion.

The accepted-response JSON state remains authoritative. The sparse index is an immutable in-memory projection with atomic clean rebuild, incremental statement updates, statistics-only posting reuse, explicit consistency checks, and no persisted index bytes. The [version-1 contract](documentation/sparse/contracts-v1.md), [engine decision](documentation/decisions/0006-section12-fielded-bm25.md), [synthetic corpus](eval/section12-sparse-v1.json), and [promotion artifact](documentation/sparse/benchmark-2026-08-21.json) record the component boundary and measured gate. Section 16 remains the release authority.

### 19.3 Optional standalone semantic retrieval

The version-1 standalone dense resolver embeds canonical requests and aliases,
never accepted-response prose. It is configuration-gated, local, CPU-bound,
eagerly initialized and readiness-checked when enabled, and registered downstream
of exact, rewrite, symbolic, and sparse retrieval. Its immutable exact-cosine
index supports incremental changed-statement replacement, retirement and
supersession exclusion, consistency checks, and clean repository-derived rebuild.
Every candidate is generation-revalidated and enters the shared eligibility and
fusion policy.

The model must be explicitly provisioned before startup. The pinned native
`all-MiniLM-L6-v2` artifact is Apache-2.0, checksum and dimension gated, and loaded
with CPU/offline/remote-code-disabled settings. Native, ONNX, and quantized
variants are reported separately; an uninstalled runtime or unprovisioned
artifact is recorded unavailable rather than downloaded or simulated. The
[contract](documentation/semantic/contracts-v1.md), [ADR 0007](documentation/decisions/0007-section13-local-semantic-and-transparent-reranking.md),
[frozen corpus](eval/section13-semantic-v1.json), and [benchmark](documentation/semantic/benchmark-2026-08-22.json)
record the implementation and independent component gate.

### 19.4 Lightweight reranking

Only a bounded fused-candidate shortlist reaches reranking. Version 1 supplies a
fixed, transparent logistic scorer over the published normalized fusion features
and base score. Candidate count, serialized input bytes, measured model time, and
output features are bounded; cancellation propagates, while input/time/scorer
failure preserves baseline order. Loaded version, readiness, completions,
fallbacks, and cancellations are observable without raw text. The reranker has an
independent feature flag and configuration-only rollback.

The frozen evaluation uses disjoint train, calibration, and release labels. No
approved local pairwise artifact exists, so pairwise comparison is explicitly
unavailable. The transparent scorer passes its safety, non-regression, and
resource checks independently of semantic retrieval, but the release partition
shows no positive ranking gain, so it is not promoted. These are
repository-visible engineering results, not Section 16 protected release
approval.

### 19.5 Utility resolvers

Section 14 supplies seven fixed built-in plugins: arithmetic, Boolean, set,
date/time, unit conversion, SemVer comparison, and UUID/slug validation. The
configuration is an allow-list of those names, not a module or entry-point loader.
The implementation contains no general expression evaluator and performs no file,
network, graph, subprocess, shell, or current-time operation.

Each plugin publishes name/version, accepted direct-request grammar, hard input and
work bounds, canonical output, evidence policy, stable errors, and health. Numeric
work uses bounded Decimal precision and magnitude; set work has bounded items;
date/time work requires ISO input and an explicit source offset; units must share a
fixed dimension; versions are SemVer 2.0.0 only; identifiers are UUID or lowercase
slug only. The [utility contract](documentation/utilities/contracts-v1.md) and
[threat model](documentation/utilities/threat-model-v1.md) define the exact rules.

A successful candidate records `learnable: false` and no accounting or Claim
evidence. Fusion re-executes the named built-in and checks its canonical input,
versions, statement digest, and response before granting deterministic answer
authority. The result is never learned, persisted, or treated as accepted-response
knowledge. Every plugin passed its own conformance, held-out, property, fuzz,
resource, ambiguity, security, and integration gate and is promoted for opt-in
component use. All remain disabled by default, and a plugin can be rolled back by
removing its name without migrating data.

## 20. API evolution and compatibility

The transport-neutral core should receive new contracts first. Adapters translate without implementing retrieval, lifecycle, fusion, or accounting logic.

### 20.1 Python API

Add typed `query_identity`, `commit_response`, `resolve_request`, lifecycle, index-rebuild, and index-check operations with concrete falsy absence values and no optional union annotations. `resolve_request` accepts a transient callable cancellation check that raises `ResolutionCancelledError`; cancellation is not serialized, included in retry identity, cached as a knowledge miss, or published as partial resolver success. Preserve `store`, `query`, `pattern_query`, `learn_from_response`, and existing `EngramCore` methods through compatible wrappers during migration.

### 20.2 MCP

Keep the existing conversation and regulated proposal tools unchanged. The Section 7 unified evidence package is not added to MCP inputs or outputs. MCP process ownership, idempotency, and proposal accounting remain unchanged.

### 20.3 gRPC

Use additive protobuf changes where possible. A new version is required for incompatible field semantics or a new resolution result structure that cannot be represented safely. Regenerate committed stubs with pinned tool versions. Do not edit generated files by hand.

### 20.4 Persistence migration operations

Section 3 owns the response artifact and receipt codecs, feature migration, quarantine classifications, and derivation of compatibility views and indexes. This cross-cutting interface and operations layer invokes those contracts during startup, reports their result, and supplies explicit backup or migration-output and downgrade procedures. Legacy statements receive only the conservative defaults permitted by the Section 3 migration contract:

- no external canonical identity;
- no retrieval aliases unless safely derived from existing request metadata;
- ACTIVE lifecycle for live statements;
- tier preserved;
- existing opaque support preserved;
- normalization and schema versions recorded during migration; and
- ambiguous exact keys quarantined from direct exact resolution until repaired.

Migration orchestration must be idempotent, surface quarantine without making the affected exact resolver ready, and retain a backup or use an explicit output path. Downgrade behavior must be documented before writing a new state version in place.

## 21. Security, privacy, and failure behavior

- Bind network services to loopback by default. Use TLS or a trusted encrypted proxy for remote access.
- Authentication and authorization remain deployment concerns, but mutation methods must be separable from read methods.
- Scope and metadata are eligibility and provenance, not an authorization system.
- Do not log raw response bodies, request text, context fingerprints, credentials, or customer Claim content by default.
- Enforce size limits on aliases, metadata, evidence, support IDs, rewrites, and diagnostic output.
- Validate configured vector index identifiers and keep the sole internal vector procedure fixed and bounded.
- Preserve the existing conservative Cypher block and read-only database account requirement.
- Treat graph and graph-vector capabilities as optional even when configured: unavailable capabilities report `ready: false`, are skipped by resolver planning, and do not change overall Engram readiness. Execute optional graph I/O outside the core-wide state critical section while retaining per-user ordering and atomic accounting/publication after the call returns.
- A failed optional resolver returns typed diagnostics and permits cheaper paths to continue.
- An unavailable Engram never blocks Tapestry's Actor path.
- A persistence error continues to distinguish live-state mutation from durable checkpoint state.

## 22. Verification strategy

### 22.1 Test layers

| Layer | Required coverage |
| --- | --- |
| Unit | Normalization, identity, collision checks, aliases, lifecycle, eligibility, feature calculation, feedback, temporal parsing, query-frame inheritance, and utility functions. |
| Property and invariant | Index add/remove/rebuild equivalence, idempotency, no cross-scope retrieval, lifecycle transition rules, bounded planners, and deterministic serialization. |
| Migration | Every supported persisted schema, repeated migration, corrupted derived indexes, rollback/output behavior, and legacy collision quarantine. |
| Core integration | Progressive resolver order, budgets, optional dependency failure, accounting, fusion, ANSWER/EVIDENCE/MISS, checkpoint degradation, and recovery. |
| Graph contract | Canonical entity/predicate lookup, active Claim filtering, temporal bounds, ownership visibility, trust, conflicts, vector support intersection, and composed paths. |
| Adapter contract | Python, MCP, and gRPC parity; additive field behavior; typed failures; cancellation and externally raised backend-failure mapping; health; TLS; graceful shutdown; and stub reproducibility. |
| End to end | Tapestry proposal, support validation, regulation, exact response serving, learn-back, supersession, stale handling, evidence handoff, and Actor fallback. |
| Performance | Exact, lexical, semantic, graph, persistence, startup rebuild, and memory benchmarks at representative corpus sizes. |

### 22.2 Evaluation corpus

The release corpus should include:

- exact repeats and accepted aliases;
- paraphrases with and without shared keywords;
- adversarial `when`/`where`, current/historical, positive/negative, and count/lookup collisions;
- short and elliptical follow-ups;
- technical identifiers, versions, symbols, paths, and error codes;
- graph one-hop and two-hop questions;
- temporal and latest-value questions;
- ambiguous entity and predicate questions;
- contradictory and stale knowledge;
- response-less but relevant Claim evidence;
- policy, namespace, context, ownership, and version mismatches;
- unsupported requests and repeated unresolved requests; and
- unavailable graph, model, index, and persistence dependencies.

Training, threshold tuning, and release-gate sets must be disjoint where a learned or calibrated policy is involved.

### 22.3 Metrics

The principal measures are:

```text
direct-answer acceptance rate
false-direct-answer rate
inference-avoidance rate
useful-evidence rate
proposal acceptance rate
rejection reason distribution
exact, symbolic, lexical, semantic, and graph contribution
p50 and p95 total Engram latency
p50 and p95 latency by resolver
startup and index-rebuild time
memory footprint
Actor tokens, retrieval calls, and latency avoided
```

The north-star measure is the fraction of requests that Engram resolves correctly or makes materially cheaper for Tapestry, subject to the false-direct-answer gate.

No fixed production threshold is asserted in this plan. The baseline work package must record current distributions and the release owner must approve numerical gates before feature tuning begins.

## 23. Delivery sequence

### Increment A: Correct identity and accepted knowledge

1. Reconcile source and capture baseline benchmarks.
2. Implement `QueryIdentity`, retrieval representations, and collision tests.
3. Define the index projection boundary; add paired exact/alias and Claim-support indexes, typed lookups, off-live rebuild, consistency checking, atomic state ownership, and repair operations.
4. Replace the support-aware statement scan with Claim fan-out lookup and prove exact, support, rebuild, mutation, concurrency, and memory bounds.
5. Define lifecycle legality and the deterministic authoritative accepted-response artifact codec.
6. Add injected eligibility context, pure time and epoch decisions, and projection revalidation that cannot return a stale direct result.
7. Add the authoritative live repository, compatibility views, and bounded STATIC/DYNAMIC admission and eviction.
8. Add durable mutation receipts, response persistence v2, legacy migration and quarantine, and startup derivation of views and indexes.
9. Add the atomic mutation coordinator and transport-neutral base commit with no implicit replacement.
10. Add audited invalidation, retirement, and concurrency-safe explicit supersession.
11. Preserve `LearnResponse` through a compatibility wrapper and close the concurrency, restart, failure, migration, time, epoch, eviction, idempotency, and performance conformance gate.

**Exit:** Distinct questions cannot overwrite each other; accepted aliases resolve one unchanged response; exact lookup is constant-time relative to corpus size; and authoritative callers can commit, supersede, invalidate, and retire accepted outputs.

### Increment B: One resolution contract

1. Define resolution budget and consumption, base `QueryFrame`, `Candidate`, `FeatureSet`, minimal `EvidenceReference`, resolver/result, and `ResolutionResult` contracts with deterministic concrete codecs and bounds.
2. Build one trusted base frame and extract side-effect-free retrieval primitives; disentangle existing graph fallback before completing the pure pattern adapter.
3. Adapt exact, existing structured graph, pattern, lexical, and support-semantic paths while retaining the hard eligibility and fixed graph security boundaries.
4. Add the deterministic registry and plan, budgeted fail-soft executor, centralized exactly-once accounting finalizer, conservative exact-only baseline orchestration, and Section 4 conformance gate.
5. Implement transparent fusion, ambiguity margin, calibrated thresholds, and typed policy reason codes in Section 5.
6. Apply statement and query-relationship feedback in Section 6.
7. Reconcile evidence contract versions, add strict full-Claim and package codecs, fixed projections, current disclosure eligibility, separate structured and semantic response-less producers, deterministic evidence normalization, an initial unfitted usefulness policy, bounded orchestration, and the transport-neutral Section 7 handoff.

**Exit:** The transport-neutral core produces bounded ANSWER, EVIDENCE, and MISS results; compatibility wrappers preserve documented behavior; proposal and accepted-success accounting remain regulated and exactly once; ambiguity lowers confidence after fusion; and a response-cache miss can carry a versioned, currently disclosure-eligible Claim package without authorizing an Engram answer. Python, MCP, and gRPC parity remains Section 15 work, while independently measured Tapestry value remains Section 16 work.

### Increment C: Structured graph resolution

1. Add previous query frames and bounded follow-up inheritance.
2. Implement operator, entity, relation, and expected-type extraction.
3. Add parameterized one-hop Claim lookup.
4. Add temporal filtering, trust inputs, and contradiction handling.
5. Add the bounded graph algebra and evidence paths after the one-hop gate passes.

**Exit:** Representative one-hop and two-hop questions resolve or abstain deterministically, historical facts are not presented as current, and conflicts never produce arbitrary direct answers.

### Increment D: Retrieval breadth and optimization

1. Add retrieval-only rewrite rules.
2. Benchmark and select sparse enhancements.
3. Add optional standalone request embeddings.
4. Evaluate lightweight reranking.
5. Add bounded utility resolvers.

**Exit:** Each promoted addition improves inference avoidance or useful evidence on a held-out set without violating memory, offline operation, or false-direct-answer gates. Turn lengths are reported separately.

### Increment E: Continuous evaluation and operations

Evaluation, observability, migration, security, adapter parity, and documentation run through all increments. Production rollout begins in shadow mode, then evidence-only mode, then regulated direct-answer mode by namespace. Rollback disables new resolvers or policy versions without making persisted accepted responses unreadable.

## 24. Release gates

A work item is complete when its API and the following evidence are present:

1. implementation complete with no unresolved high-severity correctness or security finding;
2. focused, full, migration, concurrency, and adapter tests passing;
3. measured latency and memory at declared corpus sizes;
4. invariant and index consistency checks passing;
5. wire and persistence compatibility documented;
6. failure and rollback behavior exercised;
7. operator and integration documentation updated;
8. evaluation results stored as reproducible artifacts; and
9. [ENGRAM-PROJECT-TRACKING.md](ENGRAM-PROJECT-TRACKING.md) updated with evidence links.

Any direct-answer change also requires a false-direct-answer comparison against the previous released policy. A regression blocks promotion even when raw hit rate increases.

## 25. Principal risks and controls

| Risk | Control |
| --- | --- |
| Canonicalization creates new semantic collisions | Preserve operator and qualifiers, version normalization, fail on alias collisions, and maintain adversarial tests. |
| More resolvers increase false direct answers | Common eligibility checks, independent-evidence fusion, top-two margin, conflict handling, and a held-out false-answer gate. |
| Secondary indexes diverge from JSON state | Authoritative artifacts, transactional live updates, deterministic rebuild, startup checks, and repair tooling. |
| Lifecycle is confused with tier or eviction | Separate fields and APIs, explicit transition table, and lifecycle-specific tests. |
| Feedback overfits one user or context | Separate statement and relationship statistics, scope every observation, age counts, and require sample floors. |
| Graph composition becomes open-ended | Internal parameterized plans with hard hop, row, branch, candidate, path, and output limits plus cooperative cancellation. |
| Semantic retrieval adds startup or deployment cost | Configuration-gated local models, pre-provisioned artifacts, eager startup initialization and readiness checks, quantization benchmarks, cooperative cancellation, optional-backend isolation, and supervisor-controlled shutdown. |
| Utility parsing becomes an execution surface | Fixed built-in names, dedicated narrow grammars, hard work/output bounds, no dynamic execution or I/O, authority re-execution, independent fuzz/security gates, and per-plugin rollback. |
| Evidence payload exposes too much graph data | Minimal fields, visibility filters, size limits, transport review, and Tapestry revalidation. |
| Protocol evolution breaks existing clients | Core-first contracts, additive fields, versioned protobuf when needed, compatibility tests, and staged migration. |
| Improved cache availability is mistaken for authority | Preserve current support validation and Regulator acceptance in the Tapestry path. |

## 26. Decisions governing implementation

Section 0 recorded the Increment A decisions in [ADR 0001](documentation/decisions/0001-authoritative-response-artifact-and-absence.md), [ADR 0002](documentation/decisions/0002-normalization-collisions-and-lifecycle.md), [ADR 0003](documentation/decisions/0003-derived-indexes-and-evidence-wire.md), and [ADR 0004](documentation/decisions/0004-evaluation-time-epoch-and-release-gates.md):

1. Accepted responses gain a typed authoritative persisted record with a compatibility projection; general statement dictionaries are not extended indefinitely.
2. Canonical and alias normalization is explicitly versioned, with the remediated normalization v1 defining the first releasable keyspace.
3. Conflicting active exact keys are rejected; legacy ambiguity may coexist only as an excluded collision group and never selects a direct answer.
4. `LearnResponse` becomes a compatibility wrapper over transport-neutral base commit, which creates or returns an exact retry but never implicitly supersedes; adapter wire evolution is additive only where semantics remain compatible.
5. Lifecycle mutations are authorized, idempotent, audited, and guarded by expected statement identity and generation; explicit supersession is the only replacement path and no generic lifecycle setter can assign SUPERSEDED.
6. The first exact and support `IndexState` is memory-only, deterministically rebuilt, consistency-checked, and atomically swapped; snapshots require later benchmark justification.
7. Evidence-only output uses a bounded versioned wire record with explicit truncation and no unrestricted graph content.
8. Each request captures one injected UTC evaluation time and a nonnegative namespace knowledge epoch plus availability under the documented standalone and trusted-integration rules; exact retrieval revalidates time- and epoch-dependent eligibility.
9. Initial numerical memory, evidence-usefulness, and false-direct-answer gates are fixed before feature tuning and evaluated through Section 16; turn-length distributions are reported without a timing gate.

These accepted decisions constrain Sections 2 and 3. A change requires a superseding ADR rather than an implementation-local reinterpretation.

## 27. Immediate next work

The baseline, identity, exact/alias/support index, accepted-response lifecycle, unified resolution substrate, candidate-fusion policy, and feedback/negative-resolution substrate are complete. Section 4 supplies deterministic transport-neutral frames and result contracts, pure adapters for every existing retrieval path, a cost-aware resolver plan, bounded-working-set enforcement, elapsed-time reporting, output-budget fitting before success accounting, and bounded retry-safe centralized accounting. Section 5 adds source-specific normalization, authoritative revalidation, deduplication, independent agreement, transparent scoring, ambiguity abstention, and a conservative unfitted policy. Section 6 adds strictly targeted external observations, durable exact-once receipts, bounded aged aggregates, feedback-derived history, authorized stale handoff, and conservative exact-only negative reuse. Its findings gate proves the declared 1,000-observation batch, complete negative-hit resource accounting, accurate checkpoint/replay durability, lexical knowledge safety, and representative 5,000-record preparation and recovery bounds.

Section 7, response-less Claim evidence and Tapestry packaging, is complete. ADR 0005 establishes that the strict pre-exposure core result evolves in place as one current mechanism. Full-Claim and package records establish concrete absence, allow-listed content, canonical order, explicit truncation, ten-record and 64-KiB limits, and singleton paths. The fixed graph projection and current disclosure gate require Claims to be active, system-current, currently valid, proof-canonical, non-retrieval-only, and either explicitly public or authorized through a trusted exact-`ScopeKey` decision. Structured and semantic response-less discovery, deterministic merge, the unfitted usefulness policy, complete budget fitting, transport-neutral orchestration, and the conformance gate are implemented. Noise remains MISS and Engram never promotes response-less Claim content to an unregulated answer. The [Section 7 conformance report](documentation/evidence/section7-conformance-2026-08-16.md) records 232 focused and 1,422 full passing tests, clean static/security gates, bounded inspection review, the reproducible engineering benchmark, and a fresh 1,000-turn MCP conversation with all 16 checks passing on every turn.

Section 7 closes with an explicit core handoff and conformance gate, not an adapter or release claim. Section 8 consumes the package for relation-aware fallback; Section 9 adds requested historical-time, trust-ranking, multi-value, and conflict semantics; Section 10 populates multi-hop paths. Section 15 now supplies the stable Python mapping, transient cooperative cancellation, offline preflight, and the deployment/rollback runbook; it still owns the future versioned gRPC evidence interface, authorization, redaction, cross-adapter cancellation, and telemetry. CLI and MCP remain unchanged. Section 16 calibrates numerical policy and independently measures evidence usefulness, avoided Knowledge Engine calls or tokens, failure behavior, and release value.

Section 8's deterministic contracts, engineering fixtures, and configured live MemGraph path are implemented. The configured graph resolves Sarah and `married_to`, the fixed one-hop query returns Abraham, and the complete core request retains the graph evidence in about eight seconds. Elapsed time is reported rather than used as answer authority. The [live comparison](documentation/contextual/memgraph-conversation-comparison-2026-08-20.md) is the controlling evidence for the current turn lengths. When MemGraph is unavailable, Engram reports the optional capability as not ready, skips graph resolvers, and continues serving non-graph paths. When an already-started graph call is slow, that request remains outstanding but graph I/O releases the core-wide state lock for both unified resolution and legacy chat; another user's local-only request and status inspection continue, while the same user's contextual requests remain ordered.

Section 10's closed algebra, strict bounded plans, fixed-capability one/two-hop traversal, safe Boolean and aggregate behavior, and schema-2 Claim paths are implemented. The [composition conformance report](documentation/composition/section10-conformance-2026-08-20.md) records the focused and complete-suite results available at its capture time, 15/15 engineering regression cases, the configured Sarah → Abraham → Keturah MemGraph evidence path, and a passing 1,000-turn Sarah/sushi MCP conversation. All durations are observations; no composition latency answer gate exists. The repository-visible regression splits are component evidence, not independent release evidence.

Section 12's default-off fielded BM25 resolver is implemented. It projects seven weighted fields from authoritative accepted-response artifacts, preserves bounded technical identifiers, exposes phrase/proximity/prefix/ngram/field diagnostics, shares the lexical fusion family, atomically rebuilds immutable generations, synchronizes only affected postings, reuses postings for statistics-only mutations, and persists no secondary state. The [Section 12 conformance report](documentation/sparse/section12-conformance-2026-08-21.md) records 128 focused and 1,535 full passing tests, all 11 engineering promotion verdicts, optional graph execution-isolation and sparse-readiness regressions, and a source-bound 1,000-turn official-MCP Sarah preference run with sparse enabled and five live MemGraph results.

Section 13's default-off standalone semantic resolver and transparent reranker are implemented. The semantic index embeds only canonical requests and aliases from authoritative accepted-response artifacts, verifies a pinned local model license, checksum, revision, backend, and dimension before CPU-only offline loading, publishes immutable rebuildable generations, and enters the common eligibility and fusion path after cheaper resolvers. The independent engineering gate promotes semantic for opt-in component use after 0.8889 release recall@1, an 0.1111 gain over sparse, and zero false answers. The reranker passes its safety and resource checks but remains unpromoted because it adds no release recall. The [Section 13 conformance report](documentation/semantic/section13-conformance-2026-08-22.md) records the artifact policy, disjoint benchmark, 1,562-test full suite, and source-bound 1,000-turn official-MCP Sarah preference run with semantic, reranker, and live MemGraph ready.

Section 3 retains ownership of artifact generations, lifecycle, epoch mutation, accepted-response and mutation-receipt persistence, migration, and startup derivation. Section 4 retains unique candidacy and accepted-success finalization. Section 5 owns the common feature vocabulary, fusion, ambiguity, calibrated confidence, thresholds, and policy reasons; Section 6 owns feedback targets and receipts, scoped aggregates, their feature-owned codecs and migration contract, aging, the history producer, negative resolution, and bounded core inspection. Section 7 owns full response-less Claim contracts, fixed projections, current disclosure eligibility, initial unfitted usefulness, packaging, and the transport-neutral core handoff; Section 8 owns contextual frame enrichment and canonical relation-aware graph plans; Section 9 owns requested historical-time, trust-ranking, multi-value, and conflict semantics; Section 10 owns multi-hop evidence paths; Section 12 owns the optional sparse document, tokenizer, index, and resolver contracts; and Section 13 owns standalone request embeddings, local model artifact compatibility, semantic-index lifecycle, semantic candidate production, and bounded transparent shortlist reranking. Section 15 owns cross-feature schema/startup orchestration, adapter exposure and authorization, migration and backup operator experience, downgrade and rollback, operational telemetry, and deployment; stable Python mapping, offline serving preflight, deployment/rollback documentation, optional graph execution isolation, and independent sparse and semantic readiness reporting are complete. Section 16 owns independent custodianship, calibration, usefulness and avoided-work measurement, chaos, release-scale sample floors, and release approval. Its public tuning foundation passes integrity checks, while EGR-1601 is blocked on an independent evaluation custodian and EGR-1609 is blocked on an independent release owner; release-gate and final-test content remains unprovisioned and unauthorized.
