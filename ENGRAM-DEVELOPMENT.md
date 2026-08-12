# Engram Development Plan

**Audience: Internal | Status: Proposed implementation plan | Baseline: 10 August 2026**

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

The local Tapestry integration design and trackers were used only to preserve the existing process boundary and acceptance contract. They do not assign Engram work status. Tapestry milestone and implementation authority remains with `tapestry-source/PDC-PROJECT-TRACKING.md`.

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
8. **Every expensive path is bounded.** Resolver count, candidate count, graph hops, graph rows, vector results, token output, branches, and wall time have explicit limits.
9. **Dependencies and enabled components are ready at startup.** Runtime packages are hard installation requirements and use normal eager imports. Required NLTK, spaCy, and embedding-model artifacts are provisioned before startup. Configuration may disable a feature, but an enabled graph or semantic component must initialize and pass readiness before serving; runtime downloads, deferred dependency imports, and first-request model loading are not allowed.
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

### 7.4 QueryFrame

`QueryFrame` is the runtime interpretation used by resolvers. It contains:

- original and conversationally resolved request text;
- `QueryIdentity`;
- expected object type when known;
- inherited fields and their source turn;
- rewrite chain;
- caller scope and required metadata;
- resolver cost budget; and
- stable diagnostic identifiers.

The frame is not automatically persisted as user knowledge. A compact previous frame may be kept in user context for bounded follow-up inheritance.

### 7.5 CachedResponseArtifact

The accepted write object should contain at least:

```json
{
  "schema_version": 1,
  "response": "The exact accepted human-facing response.",
  "query_identity": {},
  "retrieval": {"canonical": "...", "aliases": []},
  "tier": "STATIC",
  "lifecycle": "ACTIVE",
  "scope": {"namespace": "...", "context_fingerprint": "..."},
  "support_claim_ids": ["claim-1", "claim-2"],
  "valid_from": "",
  "valid_until": "",
  "knowledge_epoch": "kg-2026-08-10",
  "superseded_by": "",
  "source_label": "tapestry:released",
  "metadata": {}
}
```

`response` is stored once. Canonical and alias representations point to its statement ID. Support Claims remain opaque identifiers in standalone storage and are interpreted only through the graph integration contract.

### 7.6 Candidate

Every resolver should emit the common candidate contract.

```json
{
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

Unavailable features are omitted from `features.values` and named in `features.unavailable`; an empty feature set is represented by empty containers, not `null`. This distinguishes an unavailable measurement from a measured zero without introducing optional value types. Candidate IDs are proposal-local. Statement and Claim identifiers remain stable evidence references where available.

### 7.7 ResolutionResult

The core returns one of three outcomes:

```json
{
  "outcome": "ANSWER",
  "selected_candidate": {},
  "response_candidates": [],
  "evidence": [],
  "confidence": 0.0,
  "reason_codes": [],
  "query_frame": {},
  "resolver_diagnostics": [],
  "budget": {}
}
```

- `ANSWER` contains one selected response whose policy threshold and eligibility checks passed.
- `EVIDENCE` contains bounded useful evidence or response candidates but authorizes no direct response.
- `MISS` contains diagnostics and reason codes but no proposed answer.

The regulated interface may always treat an Engram `ANSWER` as a proposal that still requires current Tapestry validation. Standalone callers may configure their own acceptance policy.

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

The primary exact lookup key should be logically equivalent to:

```text
(scope_key, normalization_version, normalized_retrieval_key)
    -> ordered active statement IDs
```

The common case should contain one active statement. An ordered collection permits detection and repair of legacy collisions instead of hiding them. Eligibility still checks lifecycle, validity, required metadata, and support policy before returning a candidate.

Exact lookup should bypass tokenization, synonym expansion, vector encoding, and graph access when an eligible unambiguous result is found.

## 9. Persistence and secondary indexes

Persisted response artifacts remain authoritative. The following secondary indexes should be built and maintained under the same core mutation lock:

| Index | Purpose |
| --- | --- |
| scoped retrieval key to statement IDs | Constant-time canonical and alias lookup. |
| statement ID to retrieval keys | Efficient update, retirement, supersession, and validation. |
| Claim ID to statement IDs | Support-aware semantic lookup proportional to matching Claims. |
| statement ID to Claim IDs | Efficient lifecycle checks and reverse-index repair. |
| lifecycle and validity eligibility | Avoid ranking ineligible statements. |
| sparse field index | Optional BM25 or FTS retrieval over canonical, alias, entity, relation, identifier, and answer fields. |

Every index implementation must provide:

- add, replace, supersede, invalidate, retire, and restore operations;
- deterministic rebuild from persisted artifacts;
- a consistency checker that reports missing, extra, and conflicting mappings;
- schema and normalization version recording;
- startup behavior for legacy state;
- an atomic swap from a completed rebuilt index; and
- tests that inject interrupted or stale derived state.

Persisting a secondary index is an optimization, not a second source of truth. If an index is missing, incompatible, or corrupt, Engram should rebuild it before declaring the affected resolver ready.

## 10. Accepted response commit and lifecycle

### 10.1 Commit operation

Introduce a transport-neutral operation conceptually equivalent to:

```python
commit_response(
    artifact,
    request_id,
    expected_statement_id="",
)
```

The operation should:

1. validate response, identity, aliases, scope, tier, lifecycle, support, temporal fields, metadata size, and idempotency identity;
2. reject empty and complete normalized `IDK` responses;
3. preserve the response bytes or Unicode scalar sequence accepted by the caller;
4. detect scoped retrieval collisions;
5. create or explicitly supersede one artifact;
6. update all secondary indexes in the same live-state mutation;
7. checkpoint once when persistence is configured; and
8. return created, unchanged, or superseded state with stable identifiers.

`LearnResponse` should remain as a backward-compatible convenience that constructs a DYNAMIC ACTIVE artifact from its existing inputs. Tapestry can migrate to the richer commit method when the wire contract is available.

### 10.2 Lifecycle

The initial lifecycle states are:

| State | Retrieval eligibility | Meaning |
| --- | --- | --- |
| ACTIVE | Eligible when all other checks pass. | Current response artifact. |
| SUPERSEDED | Ineligible. | Replaced by a named newer artifact. |
| INVALIDATED | Ineligible. | Known to be unsupported, false, unsafe, or outside its validity contract. |
| RETIRED | Ineligible. | Administratively removed from service without asserting a replacement or falsehood. |

Lifecycle transitions must be explicit, idempotent, audited, and caller-authorized. Engram enforces supplied lifecycle but does not autonomously decide truth. Eviction of a DYNAMIC artifact is a storage event and should remain distinguishable from an authoritative lifecycle decision.

Temporal eligibility uses half-open bounds when supplied: `valid_from <= evaluation_time < valid_until`. A missing bound is open. Knowledge epoch mismatch follows caller policy and must not be interpreted as a date comparison.

## 11. Resolver framework

Each resolver should implement a small contract:

```python
class Resolver(Protocol):
    name: str
    cost_class: CostClass

    def available(self, frame: QueryFrame) -> bool: ...
    def resolve(self, frame: QueryFrame, budget: ResolutionBudget) -> ResolverResult: ...
```

`ResolverResult` contains candidates, evidence, diagnostics, elapsed time, consumed budget, and a typed failure or skip reason. Resolver exceptions are isolated and do not erase results from successful resolvers.

### 11.1 Initial resolver set

| Resolver | Initial behavior |
| --- | --- |
| `ExactResolver` | Scoped canonical and alias lookup with full eligibility checks. |
| `PatternResolver` | Existing AIML-style match exposed as a candidate without changing accounting during proposal. |
| `RewriteResolver` | Applies bounded retrieval-only normalization rules and records the chain. |
| `LexicalResolver` | Existing keyword, lemma, stem, synonym, phrase, recency, and hit-aware retrieval. |
| `GraphStructuredResolver` | Canonical entity/predicate and bounded structured Claim lookup. |
| `GraphSemanticResolver` | Existing support-aware Claim vector lookup, later extended to evidence-only results. |
| `StandaloneSemanticResolver` | Optional dense retrieval over cached request representations. |
| `UtilityResolver` | Deterministic bounded computations registered by type. |

Pattern accounting must be separated from pattern selection before the regulated pipeline can use pattern candidates safely. A proposal records candidacy. Only an accepted resolution records success.

### 11.2 Resolver ordering and budgets

The default order is:

1. exact;
2. retrieval rewrite plus exact retry;
3. pattern and lexical;
4. optional standalone semantic;
5. structured graph;
6. semantic graph; and
7. utility resolvers when query classification makes one applicable.

Deployments may reorder resolvers by measured latency and value, but exact and bounded eligibility checks remain first. Each resolver has independent limits and the full call has a wall-clock budget. Diagnostics must distinguish unavailable, skipped, exhausted, failed, and completed resolvers.

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

The initial application policy should be:

| Outcome | Effect |
| --- | --- |
| `accepted` | Strengthen the scoped query-to-statement relationship and the statement's reliability history. |
| `rejected_quality` | Weaken the statement broadly, subject to policy/version partitioning. |
| `rejected_context` | Weaken only the observed query, scope, or context relationship. Do not retire globally valid content. |
| `rejected_stale` | Make the candidate ineligible for the applicable validity state and request an authoritative lifecycle decision. |
| `rejected_policy` | Suppress within the relevant namespace or policy version. Do not infer global falsehood. |

Raw counters should be retained for inspection. Derived acceptance features should use minimum sample sizes, aging, and bounded priors so one outcome cannot dominate permanently.

After sufficient labeled traffic exists, an offline evaluation may compare the hand-authored formula with logistic regression or a small learning-to-rank model. A learned model must be versioned, locally available, explainable through feature output, and no less conservative on the false-direct-answer gate.

### 13.2 Negative resolution

A short-lived negative record may memoize that the same scoped request could not be resolved under the same knowledge state:

```json
{
  "query_identity": {},
  "namespace": "...",
  "context_fingerprint": "...",
  "knowledge_epoch": "...",
  "reason": "insufficient_knowledge",
  "expires_at": "..."
}
```

Negative records are not statements, facts, or `IDK` answers. They should be bounded, short-lived, inspectable, and invalidated by relevant knowledge-epoch changes. Policy, safety, transient transport failure, and authorization failure require separate reason handling and should not be generalized into knowledge misses.

## 14. Evidence-only handoff

Graph semantic or structured retrieval may find useful current Claims even when no accepted response is attached. In that case Engram should return EVIDENCE rather than MISS when the evidence policy passes.

Evidence records should include:

- Claim ID;
- source resolver;
- similarity or structured-match features;
- canonical subject, predicate, and object identifiers when the graph contract permits them;
- validity and trust fields used for filtering;
- supporting path for composed results; and
- reason the evidence was selected.

The package is bounded by Claim count, serialized size or token estimate, trust floor, similarity floor, graph rows, and total resolution time. Engram does not turn arbitrary Claim text, Passage text, or proof text into a cached response. Tapestry rechecks current support and decides how to use the evidence.

The existing gRPC contract does not transport Claim records. Adding evidence therefore requires an explicit versioned protocol change or a conservative `Struct` extension where compatibility permits it. The protocol decision must be made before implementation begins.

## 15. Query frames and multi-turn completion

User context should retain a compact previous query frame containing operator, subject entities, relation, expected object type, and qualifiers. Follow-up completion may inherit only missing fields.

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

## 16. Relation-aware graph resolution

Structured graph resolution should progress through the following stages:

1. Classify the operator.
2. Resolve subject and named entities through canonical labels, aliases, and edge surface forms.
3. Generate predicate candidates from dependency structure, verb lemmas, prepositions, Predicate labels, and configured synonyms.
4. Infer an expected object type such as PERSON, PLACE, DATE, NUMBER, ENTITY, or BOOLEAN.
5. Execute a parameterized one-hop lookup against active canonical Claims.
6. Filter by lifecycle, validity, ownership visibility, trust policy, and expected object type.
7. Phrase a response only when one unambiguous eligible result passes the answer policy.
8. Otherwise return bounded Claim evidence.

No caller-supplied Cypher participates in this resolver. Query templates and allowed predicates are internal and parameterized. Graph account permissions remain read-only.

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

### 17.2 Trust and ownership

Engram may consume trust and ownership fields supplied by canonical Claims. It does not assign or upgrade them. Visibility is an eligibility filter. Trust is a ranking and direct-answer policy input. Missing trust is explicit and follows configured conservative behavior.

### 17.3 Conflict handling

A conflict exists when simultaneously eligible canonical Claims map the same subject and predicate to incompatible canonical objects for the requested temporal and scope frame.

Engram should:

1. preserve all conflicting Claim IDs;
2. distinguish an actual contradiction from multi-valued relations;
3. suppress arbitrary direct phrasing;
4. return EVIDENCE with a conflict reason when useful; and
5. expose trust, temporal, and provenance features without declaring which Claim is true.

## 18. Bounded graph composition

Composition begins only after one-hop relation resolution meets its accuracy and latency gate. The initial algebra is:

```text
LOOKUP  EXISTS  COUNT  AND  OR  NOT  MIN  MAX  ORDER
```

Every query plan must declare:

- `max_hops`;
- `max_rows`;
- `max_branches`;
- `max_candidates_per_step`;
- `query_timeout`; and
- maximum evidence-path size.

The first release should favor one and two hops. It should reject cycles, unconstrained predicates, cartesian expansion, unsupported aggregation, and result truncation that could change the answer. Each result carries the Claim path used to derive it.

`Where was Microsoft's founder born?` is a valid target only when Engram can resolve both `founded_by` and `born_in`, enforce scope and temporal validity at each hop, and return the two-Claim path. Otherwise it returns the partial path as evidence or abstains.

## 19. Retrieval expansion after the foundation

### 19.1 Symbolic retrieval rewrites

Retrieval rewrites reduce surface variation before any resolver without selecting a conversational response. They are separate from AIML template redirects.

Rules should be independently authored, versioned, deterministic, depth-limited, cycle-checked, and traced. Initial classes include contraction normalization, question normalization, paraphrase reduction, pronoun transformations, synonym classes, conversational repair, context-dependent reductions, and technical phrasing normalization.

Historical AIML and ALICE behavior may provide a taxonomy of transformations, but historical implementation source or response corpora must not be copied into Engram.

### 19.2 Sparse retrieval

Evaluate fielded BM25 or SQLite FTS5 as a rebuildable secondary index. Candidate fields include canonical request, retrieval aliases, entities, relation, keywords, technical identifiers, and optionally response text. Benchmarks should isolate phrase, proximity, prefix, character n-gram, and technical tokenization improvements.

The existing JSON state remains authoritative unless measured scale, durability, and operational evidence justify a storage redesign.

### 19.3 Optional standalone semantic retrieval

Standalone dense retrieval should embed canonical requests and aliases, not accepted response prose. The feature remains configuration-gated, local, CPU-bound, eagerly initialized and readiness-checked at startup when enabled, and downstream of exact, rewrite, symbolic, and sparse retrieval.

Model artifacts must be provisioned before startup. Benchmarks should compare the current sentence-transformer path with quantized or ONNX execution where licensing, numerical stability, and deployment support are acceptable.

### 19.4 Lightweight reranking

Only a small candidate shortlist may reach reranking. Candidate approaches include logistic regression, a small learning-to-rank model, or a local pairwise encoder. Promotion requires a measured direct-answer precision or useful-evidence gain after accounting for latency, memory, cold start, explainability, and operational complexity.

### 19.5 Utility resolvers

Utility resolvers may cover arithmetic, date arithmetic, unit conversion, Boolean and set operations, version comparison, and identifier parsing. Each plugin declares accepted input types, computational bounds, deterministic formatting, error behavior, and an independent conformance suite. Utility output is not learned as authoritative knowledge unless committed through the ordinary accepted-response path.

## 20. API evolution and compatibility

The transport-neutral core should receive new contracts first. Adapters translate without implementing retrieval, lifecycle, fusion, or accounting logic.

### 20.1 Python API

Add typed `query_identity`, `commit_response`, `resolve_request`, lifecycle, index-rebuild, and index-check operations with concrete falsy absence values and no optional union annotations. Preserve `store`, `query`, `pattern_query`, `learn_from_response`, and existing `EngramCore` methods through compatible wrappers during migration.

### 20.2 MCP

Add fields and tools only after the core contract stabilizes. Existing regulated tools must preserve exact idempotency and proposal accounting. MCP process ownership remains unchanged.

### 20.3 gRPC

Use additive protobuf changes where possible. A new version is required for incompatible field semantics or a new resolution result structure that cannot be represented safely. Regenerate committed stubs with pinned tool versions. Do not edit generated files by hand.

### 20.4 Persistence migration

Legacy statements load with conservative defaults:

- no external canonical identity;
- no retrieval aliases unless safely derived from existing request metadata;
- ACTIVE lifecycle for live statements;
- tier preserved;
- existing opaque support preserved;
- normalization and schema versions recorded during migration; and
- ambiguous exact keys quarantined from direct exact resolution until repaired.

Migration must be idempotent and retain a backup or use an explicit output path. Downgrade behavior must be documented before writing a new state version in place.

## 21. Security, privacy, and failure behavior

- Bind network services to loopback by default. Use TLS or a trusted encrypted proxy for remote access.
- Authentication and authorization remain deployment concerns, but mutation methods must be separable from read methods.
- Scope and metadata are eligibility and provenance, not an authorization system.
- Do not log raw response bodies, request text, context fingerprints, credentials, or customer Claim content by default.
- Enforce size limits on aliases, metadata, evidence, support IDs, rewrites, and diagnostic output.
- Validate configured vector index identifiers and keep the sole internal vector procedure fixed and bounded.
- Preserve the existing conservative Cypher block and read-only database account requirement.
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
| Adapter contract | Python, MCP, and gRPC parity; additive field behavior; typed failures; deadline ambiguity; health; TLS; graceful shutdown; and stub reproducibility. |
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
3. Add exact, alias, and Claim-support reverse indexes with rebuild and consistency checks.
4. Add accepted response commit, STATIC support, validity metadata, and lifecycle.

**Exit:** Distinct questions cannot overwrite each other; accepted aliases resolve one unchanged response; exact lookup is constant-time relative to corpus size; and authoritative callers can commit, supersede, invalidate, and retire accepted outputs.

### Increment B: One resolution contract

1. Add `QueryFrame`, `Candidate`, resolver, budget, and `ResolutionResult` contracts.
2. Adapt exact, pattern, lexical, structured graph, and semantic graph paths.
3. Implement transparent fusion, ambiguity margin, and typed reason codes.
4. Apply statement and query-relationship feedback.
5. Return bounded evidence without an attached response.

**Exit:** Python and service adapters agree on ANSWER, EVIDENCE, and MISS semantics; proposal accounting remains regulated; ambiguity lowers confidence; and relevant Claims can reduce Tapestry work on a response-cache miss.

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

**Exit:** Each promoted addition improves inference avoidance or useful evidence on a held-out set without violating latency, memory, offline operation, or false-direct-answer gates.

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
| Graph composition becomes open-ended | Internal parameterized plans with hard hop, row, branch, candidate, output, and time limits. |
| Semantic retrieval adds startup or deployment cost | Configuration-gated local models, pre-provisioned artifacts, eager startup initialization and readiness checks, quantization benchmarks, and resolver timeouts. |
| Evidence payload exposes too much graph data | Minimal fields, visibility filters, size limits, transport review, and Tapestry revalidation. |
| Protocol evolution breaks existing clients | Core-first contracts, additive fields, versioned protobuf when needed, compatibility tests, and staged migration. |
| Improved cache availability is mistaken for authority | Preserve current support validation and Regulator acceptance in the Tapestry path. |

## 26. Decisions required before implementation

The following decisions should be recorded as short architecture decision records during Increment A:

1. Whether response artifacts remain statement dictionaries or gain a typed persisted record with a compatibility projection.
2. The exact canonical and alias normalization algorithm and versioning policy.
3. Whether exact-index collisions are rejected universally or can coexist in a quarantined legacy state.
4. The compatibility path from `LearnResponse` to `CommitResponse` and whether gRPC uses additive v1 fields or a v2 service.
5. The lifecycle transition authorization and optimistic-concurrency contract.
6. Whether derived indexes are memory-only, optionally persisted, or rebuilt at every startup by corpus size.
7. The evidence-only wire representation and maximum payload.
8. The source of evaluation time and knowledge epoch in standalone and Tapestry deployments.
9. The first numerical latency, memory, evidence-usefulness, and false-direct-answer release gates.

These decisions constrain implementation. They do not delay baseline measurement, adversarial corpus construction, or source reconciliation.

## 27. Immediate next work

The baseline and identity foundation are complete, including the 11 August remediation for symbolic comparison collisions, conservative relation extraction, strict concrete mapping inputs, and transport-neutral enabled-component preflight. The next implementation cycle is Section 2:

1. add scoped exact and statement-to-key indexes with explicit legacy collision reporting;
2. add Claim-to-statement and statement-to-Claim support indexes;
3. make every live index mutation atomic under the existing core mutation lock;
4. implement deterministic rebuild, consistency checking, dry-run repair, and startup compatibility behavior; and
5. prove constant-time exact lookup and support-fan-out scaling with invariant, concurrency, corruption, and interrupted-rebuild tests.

Section 3 commit and lifecycle work remains blocked on those index foundations. Later resolver work must not bypass them.
