# Response-less Claim evidence contracts v1

**Status:** EGR-702 through EGR-712 strict records, package, projection boundary, current disclosure, producers, normalization, usefulness policy, bounded orchestration, handoff, and conformance implemented  
**Owners:** `engram/resolution.py`, `engram/graph.py`, `engram/evidence.py`, `engram/resolvers.py`, and the transport-neutral projection methods in `engram/core.py`

## Full Claim record

`ClaimEvidenceRecord` is a closed, deterministic `schema_version = 1` transport-neutral record. It contains only:

- a non-empty stable Claim identifier and one primary source resolver;
- a bounded, sorted set of resolver contributions;
- the Section 5 `FeatureSet`, preserving measured zero separately from unavailable input;
- non-empty canonical subject-entity, predicate, and object-entity identifiers;
- the one captured UTC evaluation time, current active/system/valid-time decisions, and optional canonical world-validity bounds;
- supplied trust category, score, and version with explicit availability (an unavailable numeric input is concrete zero; an available measured zero remains distinguishable);
- an allow-listed public-rule or trusted-exact-`ScopeKey` disclosure decision with policy and authority provenance;
- the singleton path containing exactly the record's Claim ID; and
- 1 through 16 unique, sorted, content-free selection reason codes.

Every identifier is limited to 256 UTF-8 bytes and excludes whitespace and control characters. Resolver names and reason codes retain the shared 96-byte bounds. Validity timestamps use the canonical RFC 3339 UTC representation ending in `Z`; present bounds must be ordered and must agree with the recorded current-time decision. Supplied trust is finite and in `[0, 1]`; a supplied score requires its positive version. Public disclosure cannot claim a visibility authority, while company and customer disclosure require explicit trusted-scope authority provenance.

All nested records have closed exact-field dictionaries. The top-level record has deterministic dictionary and JSON codecs, exact-field decoding, immutable nested feature state, and no `None`/JSON `null` values.

## Content boundary

The record has no generic provenance or diagnostic mapping. Raw Claim fields, denormalized subject/predicate/object surfaces, Passage or source content, proof payloads or receipts, credentials, Cypher, embeddings, and arbitrary graph properties have no representable field. An input containing any additional field fails closed. Later projection code must construct this record from an allow-listed row rather than filtering an arbitrary graph mapping after publication.

The path is deliberately singleton in schema version 1. Section 10 owns any future multi-hop record version and cannot populate extra path identifiers through Section 7 codecs.

## Evidence package

`EvidencePackage` is a closed, deterministic `wire_version = 1` envelope. It retains at most 10 records and its complete canonical JSON representation, including the envelope, counts, reasons, nested records, and multibyte UTF-8 identifiers, never exceeds 65,536 bytes.

The builder accepts no more than the shared 1,000-record resolver-output ceiling, groups records by stable Claim ID, rejects incompatible records for the same ID, removes byte-identical duplicates, and sorts the survivors by Claim ID. It then applies the requested count limit (never above 10) and complete serialized-byte limit (never above 64 KiB). Byte fitting removes records from the end of canonical order until the entire envelope fits; it measures every candidate envelope before constructing the strict value, so oversized intermediate values do not bypass deterministic truncation.

`retained_count` must equal the record collection length. `omitted_count` counts identical duplicates and records removed by count or byte limits. `truncated` is true exactly when the omitted count is positive, and one or more sorted closed reasons identify `duplicate_claim_id`, `record_limit`, or `serialized_size_limit`. An available empty package remains a valid concrete value with zero counts, false truncation, and no reasons; EGR-710 owns whether an empty package can support an `EVIDENCE` outcome.

The decoder rejects an oversized JSON string before parsing, rejects more than 10 encoded records before decoding nested records, and applies the same exact-field, nested-version, count, order, uniqueness, reason, and final-byte invariants as direct construction.

## Fixed Claim projection boundary

Full records can be sourced only through three fixed versioned capabilities:

- `structured_entity_claim_projection_v1` matches a bounded entity surface through canonical entity edges;
- `structured_keyword_claim_projection_v1` applies the bounded legacy keyword fallback to canonical Claim subjects; and
- `vector_claim_projection_v1` invokes the configured allow-listed Claim ANN index through the one internal procedure query.

No method accepts Cypher. Structured calls accept only a bounded term, one closed query identifier, and a row limit, and make at most three entity or keyword attempts even when every attempt returns zero rows. Vector calls accept only a finite bounded embedding, a syntactically validated fixed index identifier, a row limit, and a similarity floor. Generic `CALL` remains forbidden by the ordinary graph execution boundary. Legacy surface-returning structured/vector methods remain unchanged for their existing compatibility callers; Section 7 producers consume only the new projection capabilities.

All three fixed queries return the same exact allow-list: stable Claim ID; canonical subject, predicate, and object IDs; invalidation, system-time, and valid-time fields with availability; proof-canonical state; ownership category; supplied trust category, score, and positive score version with availability; and either a structured match or raw semantic similarity with availability. They never return denormalized subject/predicate/object prose, raw Claim nodes, properties maps, Passage/source/proof content, credentials, Cypher, or embeddings.

`ClaimProjection` normalizes external graph nulls immediately to concrete empty strings or numeric zero paired with false availability. It rejects missing/extra fields, invalid identifiers, timestamps and intervals, noncanonical predicates, unknown ownership, trust/version disagreement, nonfinite measurements, source/measurement disagreement, invalid index provenance, excess rows, and incompatible rows sharing one Claim ID. Exact duplicate rows are collapsed and decoded output is Claim-ID ordered. The transport-neutral `Engram` boundary preserves cooperative checks, row and working-memory limits, rejects conflicts across fixed calls, and keeps the semantic projection path fail-soft after strict rejection.

## Current disclosure eligibility and revalidation

`ClaimEligibilityEvaluator` uses only the immutable `QueryFrame.eligibility_context.evaluation_time`; it never reads a second clock or accepts time from ordinary metadata. The current-only policy excludes an invalidated Claim, a Claim without an available system-time lower bound, a Claim outside its half-open system interval, a Claim outside its optional half-open valid-time interval, and a retrieval-only/noncanonical predicate. Canonical subject, predicate, and object identifiers are already mandatory at the projection boundary.

An explicitly `PUBLIC` Claim receives the fixed `claim-disclosure-v1` public decision for the caller's exact frame `ScopeKey`. `COMPANY` and `CUSTOMER` Claims fail closed unless an injected trusted visibility capability returns an allowed decision for both the exact typed `ScopeKey` and exact ownership category. `ExactScopeVisibilityAuthority` supplies a bounded 4,096-grant configuration implementation based only on complete `ScopeKey` equality; it does not parse or derive authority from namespace or context text. Mismatched scopes, mismatched ownership, denials, malformed results, exceptions, and missing authorities are stable exclusions.

Missing Claim trust stays explicitly unavailable with concrete zero; it does not become measured zero and is not inferred from ownership, scope, labels, or resolver source. Current Section 7 disclosure does not rank trust.

Immediately before publication, the evaluator re-reads each Claim through `claim_projection_by_id_v1`, a fourth fixed canonical-only query. The query asks for up to two rows so the one-row decoder can detect duplicate or conflicting canonical matches instead of hiding them behind `LIMIT 1`. Missing rows, query failure, duplicate output, a result carrying any other projection identifier, and changed canonical subject/predicate/object identity exclude the Claim. The current projection—not discovery-time lifecycle, validity, ownership, or trust—is evaluated again. Only an eligible decision marked revalidated can produce `ClaimValidityInputs`; this makes publication-time enforcement an executable precondition for later record construction. Batch revalidation is capped at 1,000 projections and performs cooperative checks before every read.

## Structured response-less producer

`StructuredGraphResolver` is a side-effect-free full-Claim producer. It uses only the fixed structured projection capabilities, evaluates every discovery row, and re-reads every initially eligible Claim through the fixed by-ID projection immediately before constructing a record. It emits no response candidate, graph prose, or minimal evidence by default. The record converter accepts only an eligible revalidated decision from an allow-listed producer; it requires a structured discovery projection for `structured_graph` and a vector discovery projection for `support_semantic`. It derives canonical references, current lifecycle and validity, supplied trust availability, disclosure provenance, singleton path, retrieval availability, and stable content-free reasons from the strict projection and decision.

Output is sorted by Claim ID. Discovery and revalidation share the graph-row lease; record count, complete evidence bytes, output bytes, working memory, and cooperative deadline checks are enforced before publication. Malformed discovery failures become the resolver's existing typed failure through the executor, while current-row unavailability, changed identity, and current ineligibility are bounded stable exclusions. Diagnostics contain only counts and reason codes. The executor independently re-applies evidence-count, evidence-byte, output, diagnostic, graph-row, and memory ceilings while retaining the one current resolver-result schema.

## Semantic response-less producer

`SupportSemanticResolver` retains the existing legacy ANN query and support-to-artifact intersection as the authoritative response-candidate path. This matters because a legacy support Claim can remain valid candidate support even when it lacks the complete canonical entity-edge projection required for publication as full evidence. Candidate ranking, accepted response text, feature values, support references, scope/lifecycle filtering, and response accounting therefore continue through the original raw-hit intersection.

The resolver separately invokes the fixed `vector_claim_projection_v1` capability for full response-less records. The legacy candidate search receives the same candidate/vector limit it had before; the actual raw rows it returns are charged first, and only the remaining vector-result lease is available to the strict projection search. A nonconforming transport that over-returns is clamped or rejected. This permits strict eligible evidence for unlinked Claims without changing which accepted responses can become candidates or double-counting an unbounded pair of ANN result sets.

Every strict vector projection preserves its raw similarity as the available `semantic_similarity` feature; structured match is explicitly unavailable. Initially eligible projections are re-read by ID, and only eligible revalidated records are published in Claim-ID order. Candidate support references consume the shared evidence-count lease first to preserve existing behavior; full records use the remainder. Vector rows, revalidation attempts, evidence bytes, output bytes, live retained memory (including runtime match values), and cooperative deadlines are bounded and measured. Claim-only records create no candidate or accounting observation. Missing model, index, dimension compatibility, graph capability, malformed projection output, or graph availability fails soft to no strict records; deadline and memory exhaustion remain typed resource results.

## Cross-producer normalization

`canonicalize_claim_evidence` accepts at most 1,000 strict records, groups them by stable Claim ID, and emits Claim-ID-ordered records. Exact duplicates collapse. Records for one ID may differ only in producer contributions, complementary feature availability, and stable selection reasons. Canonical references must be identical, and the revalidated validity, trust, disclosure, and singleton-path state must agree exactly; otherwise normalization rejects the group rather than selecting the first resolver's view. Two available values for the same feature must also be equal.

Available measurements override another producer's unavailable marker. Source contributions are unioned, sorted, and capped at eight; the lexically first contribution becomes the deterministic primary source. When at least two distinct sources agree on the same compatible Claim state, `source_agreement` becomes an available measured `1.0` and the stable reason is retained. A single-source record cannot claim source agreement. Reasons are unioned, sorted, and capped at 16. Re-normalizing an already normalized collection is idempotent, reversing producer order produces the same result, and inputs remain immutable.

`ExecutionReport.canonical_claim_evidence()` exposes this normalization over raw outputs from only the two allow-listed Section 7 producers. Records from any other resolver name are ignored and cannot manufacture a candidate, response evidence reference, or accounting observation.

## Initial evidence-usefulness policy

`EvidenceUsefulnessPolicy` is the closed, inspectable `claim-evidence-usefulness-v1` rule. Its non-trust floors were hand-authored and frozen before conformance measurements: canonical completeness `1.0`, structured match `1.0`, semantic similarity `0.60`, and source agreement `1.0`. Version 1 constructors and exact-field codecs reject altered constants, unknown fields, unsupported versions, non-finite inputs, and non-concrete trust-floor configuration. These numbers are engineering defaults, not fitted coefficients or empirical release claims; Section 16 owns independently labeled calibration and any future version.

A Claim must have complete canonical state and at least one available structured or semantic measurement at its respective floor. Unavailable retrieval inputs produce a different reason from available measured values below the floor, including measured zero. Source agreement is a positive stable reason but cannot rescue a Claim whose retrieval measurements do not qualify. The policy does not compare Claims, rank trust, authorize a response, or create direct-answer authority.

Supplied trust is read from the explicit `ClaimTrustInputs` availability state. By default no trust floor is configured, so both unavailable trust and available measured zero remain includable and receive distinct informational reasons. An operator may enable one finite `[0, 1]` inclusion floor; unavailable trust then excludes with `supplied_trust_required_unavailable`, an available value below the floor excludes with `supplied_trust_below_floor`, and the exact boundary qualifies. This is the only current-policy trust effect. `EvidenceUsefulnessDecision` carries only Claim ID, policy version, inclusion boolean, and sorted closed reasons, and rejects externally inconsistent reason/boolean combinations.

## Bounded EVIDENCE/MISS orchestration

`ResolutionOrchestrator` preserves fusion and direct-answer authority unchanged. An `ANSWER` never carries a response-less package. On a non-answer path, raw records from completed allow-listed Claim producers are canonicalized, checked against the frame's exact disclosure scope and evaluation time, evaluated by the frozen usefulness policy, and fitted into one package. A completed producer with no strict raw record leaves the package unavailable, because dependency failure and a genuine no-hit are not distinguishable at the fail-soft projection boundary. Once at least one trusted raw record reaches policy/package evaluation, exclusion may produce an available empty package. At least one retained package record can upgrade a `MISS` to `EVIDENCE`; an available empty package cannot. Existing response-candidate `EVIDENCE` remains `EVIDENCE` when every Claim is excluded.

The final package uses the Claim-count allowance remaining after minimal response-support references and the evidence-byte allowance required by the complete serialized package. It retains at most 10 records. Resolver execution remains authoritative for graph-row and vector-result consumption; orchestration adds measured normalization, policy, package, diagnostic, output, elapsed-time, and working-memory use without exceeding the configured reported ceilings. Deadline, memory, evidence-byte, diagnostic, and output exhaustion produce stable reasons and dimensions. Output fitting removes nested resolver detail before package records, then legacy evidence and response candidates; it downgrades only when no valid useful output can fit. Complete output bytes are solved to a serialization fixed point.

Raw producer `claim_evidence` is removed from nested `resolver_results` before the unified value is constructed. `ResolverResult` requires every raw record's primary and singleton contribution source to match its producing resolver. Records emitted under any non-allow-listed producer are ignored with `claim_evidence_untrusted_producer`; frame-binding or cross-producer conflicts fail closed with `claim_evidence_conflict`. `ResolutionResult` rejects any nested raw full record, so excluded or over-limit Claims cannot bypass the package. Policy and accounting diagnostics contain only bounded counts, versions, booleans, and reason counts. A Claim-only result creates no response candidate, candidacy observation, accepted statement, or accepted-success credit.

## Interface boundary

[ADR 0005](../decisions/0005-section7-evidence-compatibility.md) records this as an in-place evolution of one pre-exposure core result mechanism. `ResolverResult` always contains concrete `claim_evidence`; `ResolutionResult` always contains the concrete package and availability flag; stale development payloads missing those fields fail exact decoding. CLI and MCP remain unchanged and do not expose unified resolution. The committed gRPC v1 proposal-resolution interface also remains unchanged; Section 15 must version a future RPC/message/service if it exposes the package.

## Verification

Focused executable coverage is in `tests/test_evidence.py`, `tests/test_claim_projection.py`, `tests/test_claim_eligibility.py`, `tests/test_resolution_contracts.py`, `tests/test_resolvers.py`, and the legacy vector cases in `tests/test_indexes.py`. It covers deterministic and concrete-absence round trips, unavailable versus measured-zero trust, identifier/reason/source/path bounds, validity consistency, disclosure provenance, unsupported versions, unknown fields, every explicitly excluded payload field, canonical ordering, identical deduplication, projection conflicts, exact retained/omitted accounting, count and complete-byte truncation, hard constructor limits, bounded input, pre-parse rejection, fixed-query/index provenance, strict row decoding, external-null normalization, canonical-only query returns, row/embedding and over-return limits, transport-neutral exposure, semantic fail-soft behavior, active/system/valid-time matrices and boundaries, retrieval-only exclusion, public and exact-scope private disclosure, authority mismatch/failure, trust absence, fixed by-ID revalidation, identity change, current-state replacement, batch bounds, the mandatory revalidated validity handoff, the one exact current resolver/result shape, stale-shape rejection, raw-record containment, structured full-record production, ineligible and changed-Claim exclusion, side-effect freedom, semantic unlinked-Claim production, raw-similarity retention, zero-candidate executor continuation, legacy candidate non-regression, fixed-query-to-record vertical integration, shared vector accounting, producer/executor graph, byte, memory, and deadline clamps, order-independent/idempotent cross-source merging, feature availability and agreement, reference/state/feature conflicts in either order, source/input/cooperative bounds, cross-producer execution without candidacy or accounting, exact usefulness-policy codecs and frozen floors, structured/semantic boundaries, unavailable versus measured-zero reasons, optional trust-floor absence/zero/boundary behavior, source-agreement non-bypass, Claim-only EVIDENCE, excluded-Claim MISS, response-candidate preservation, cross-producer package deduplication, conflict and dependency fail-soft behavior, count/evidence-byte/output/diagnostic/memory/deadline fitting, complete accounting, and decision consistency.
