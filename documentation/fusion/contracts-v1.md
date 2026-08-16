# Candidate fusion and ambiguity contract v1

**Status:** Implemented with an unfitted conservative initial policy on 15 August 2026  
**Owners:** `engram/fusion.py` and the fusion boundary in `engram/resolvers.py`  
**Initial policy:** `fusion-v1.0.0`, formula version 1

## Scope and safety boundary

The Section 5 layer consumes the bounded resolver outputs defined by Section 4. It does not generate text, call a model, mutate authoritative state, add graph facts, or infer missing trust and identity values. Raw `ResolverResult` records remain the complete contribution and diagnostic audit trail. The bounded fusion report links each merged statement to its candidate IDs, resolver sources, raw feature records, normalized inputs, diagnostic field names, evidence IDs, weighted contributions, eligibility flags, and stable reason codes without copying response text or diagnostic values.

A unique exact result can still answer as a single source after current-state revalidation. A non-exact result can answer only when at least two distinct candidate-producing resolver families agree on the same statement and an actual retained `SUPPORT` evidence reference is present. Missing or explicitly incomplete support, response conflicts, stale authoritative state, scope or visibility mismatch, lifecycle exclusion, identity mismatch, object-type mismatch, and a close runner-up all prevent direct selection. If no current-state authority is supplied, the engine safely abstains; production and default orchestration bind authority to the same Engram used for accounting.

## Canonical feature contract

Every canonical input has range `[0.0, 1.0]`, where larger is better. `NormalizedFeatureSet` always contains a concrete numeric value for every feature and separately lists which values are available. An unavailable observation therefore has numeric zero plus absent availability; it is never represented by `None` or JSON `null` and is not confused with a measured zero.

| Feature | Meaning | Unavailable when |
| --- | --- | --- |
| `exact` | Scoped normalized request equality | exact lookup did not measure equality |
| `pattern` | Bounded pattern specificity | no pattern resolver contribution exists |
| `lexical` | Calibrated lexical relevance | no lexical score or overlap was measured |
| `semantic` | Bounded semantic similarity | no semantic model observation exists |
| `entity` | Query/candidate entity identity agreement | entity identity was not measured |
| `relation` | Query/candidate relation agreement | relation identity was not measured |
| `object_type` | Expected/candidate object-type agreement | object type was not measured |
| `support` | Presence of an actually retained linked support reference | support was inspected and absent; the measured value is zero |
| `history` | Bounded accepted-use history | no authoritative accepted-use observation exists |
| `freshness` | Bounded time-recency signal | recency was not measured |
| `authority` | Explicit source trust or authority input | no authority value was supplied; source labels are not silently ranked |
| `agreement` | Distinct resolver agreement on one statement | deduplication has not run |
| `margin` | Leading score minus runner-up score | fewer than two score-eligible candidates exist |

The executable definitions, including range and absence text, are the closed `FEATURE_DEFINITIONS` mapping. Tests assert exact coverage of the 13-member `FusionFeature` vocabulary.

## Resolver-specific normalization

Raw resolver scales are never pooled under a generic score:

- `exact_match` is clamped to `[0, 1]` and mapped only to `exact`.
- `pattern_specificity = s` maps to `s / (s + 4)` after negative values are floored at zero.
- the lexical scorer's documented `[0, 1]` `lexical_score` is used directly; `lexical_overlap` is its fallback. The legacy test-only `score` alias is accepted only for a lexical source.
- `semantic_score` is the raw bounded similarity. `legacy_retrieval_score` and `vector_weight` are deliberately ignored because they are different or already blended scales.
- `entity_match`, `relation_match`, and `object_type_match` map only to their named canonical features; their trusted producers remain owned by §8.
- `support_coverage` is accepted only from support-semantic candidates, lexical `recency` only from lexical candidates, and `authority_score` only from semantic candidates. Raw resolver `hit_rate` is ignored; history enters only through current authoritative artifact statistics until §6 defines a versioned feedback producer.
- `priority`, `legacy_retrieval_score`, and `vector_weight` are deliberately ignored. They remain separate raw provenance and are not reinterpreted as lexical relevance, semantic similarity, authority, or accepted-use history.
- an authoritative artifact can replace support and history with its current support links and `hit_count / query_count`. Authority is available only from an explicit finite `metadata.authority` value.

Clamping is a normalization boundary, not permission to select: central eligibility and the threshold/margin policy remain mandatory.

## Deduplication and agreement

Candidates group by stable `statement_id`, then sort by the fixed source order exact, support semantic, pattern, lexical, standalone semantic, and utility. A response, scope, or lifecycle disagreement within one statement group makes that group unselectable; no resolver is allowed to win the conflict by order. Evidence IDs are deduplicated while every original candidate remains in its owning resolver result.

For compatible duplicates, positive relevance features take the maximum normalized observation. Entity, relation, object type, support completeness, freshness, and authority take the minimum so one favorable resolver cannot hide an explicit mismatch or weak trust signal. Agreement is `min(1, (distinct_resolver_families - 1) / 2)`: one family is a measured zero, two families produce `0.5`, and three or more produce `1.0`. Duplicate members of one configured family do not inflate agreement. The merged candidate gets a deterministic policy/request/statement-derived ID and preserves exact response bytes from the strongest compatible contribution.

The top-level report is bounded to 16,384 encoded bytes, eight candidates, and eight inline contribution summaries per candidate. Omitted counts are explicit. This report bound does not discard raw resolver results; normal result-output budgeting can independently truncate those records under the Section 4 rules.

`FusionDecision.working_memory_bytes` is a deterministic conservative serialized working-set estimate covering input candidates and evidence, normalized contributions, fused groups, and the retained report. The orchestrator passes only the allowance remaining after resolver execution. Fusion returns typed `fusion_memory_exhausted` without selecting a response when that allowance is insufficient, and complete `BudgetConsumption.working_memory_bytes` is the resolver consumption plus the bounded fusion estimate. Direct engine callers can either consume the frame limit or supply a concrete available allowance; a nonzero override without its availability marker is rejected.

## Central eligibility order

Eligibility runs before a candidate can influence an answer decision:

1. require exact scope and `ACTIVE` lifecycle and reject same-statement response conflicts;
2. for artifacts, re-run Section 3 lifecycle, validity-time, namespace-epoch, and repository availability policy against the captured request context;
3. require the authoritative response bytes, requested metadata, and requested source label to still match;
4. enforce ownership visibility: absent, `public`, and `scope` use the exact `ScopeKey`; `context` additionally requires the exact non-empty owner context fingerprint; unknown or private modes abstain because the base frame has no authoritative private-owner identity;
5. treat explicit `support_complete: false` as answer-ineligible;
6. for non-exact answers, require at least two distinct resolver sources and an actually retained `SUPPORT` reference, regardless of an artifact's unexpanded support count;
7. treat an available zero entity or relation match as identity mismatch; when an expected object type is known, require an available nonzero object-type match.

Hard scope, lifecycle, current-state, source, metadata, visibility, and content failures remove a group from scoring and evidence. Missing independent support and identity/type mismatches allow a useful candidate to remain evidence but prevent `ANSWER`.

## Formula and decision policy

For every available non-exact, non-margin feature `i`, formula version 1 calculates:

```text
weighted_average = sum(weight[i] * value[i]) / sum(weight[i])
score = exact + (1 - exact) * weighted_average
```

Unavailable inputs do not enter either sum. `margin` has weight zero because it is calculated only after ranking. The exact term transparently dominates at `1.0`; there is no hidden inference or learned model. The initial weights and gates are hand-authored conservative defaults, are serialized in every fusion report, and have fingerprint `1f9d19acaedc277b4916bc366e74dc8c03d921e335acd21ac7fc2f1b449d963c`.

The released gates are:

- `ANSWER >= 0.78`, provided answer eligibility passes;
- `EVIDENCE >= 0.35`, or an existing graph evidence reference is available; and
- top-two margin `>= 0.12` for an answer when a runner-up exists.

A close distinct runner-up always causes `EVIDENCE`, even if the leading score is high. Non-answer confidence remains concrete zero and unavailable in the unified result; `ANSWER` exposes the selected score as available confidence.

## Stable policy reasons

`FusionPolicyReason` is the closed non-content vocabulary. Outcome reasons include `answer_exact_eligible`, `answer_fusion_threshold`, `answer_margin_clear`, `ambiguous_top_candidates`, `answer_threshold_not_met`, `answer_eligibility_prevented`, `evidence_threshold_met`, `graph_evidence_available`, `evidence_threshold_not_met`, and `no_eligible_candidates`. Eligibility reasons cover scope, lifecycle, same-statement conflict, missing authoritative state, changed response, artifact ineligibility, required metadata/source, ownership visibility, incomplete support, missing independent sources, identity mismatch, and object-type mismatch.

The orchestrator adds the pre-existing stable accounting and output-budget reasons. Free-form response text and diagnostic values never become policy reason codes.

## Policy provenance and performance evidence

Section 5 does not fit policy parameters on unit tests, conformance fixtures, or observed regression failures. Policy `fusion-v1.0.0` freezes an auditable initial configuration: equal primary lexical and semantic weights, lower bounded support and agreement reinforcement, small contextual and current-state modifiers, two independent resolver families, mandatory retained support for non-exact answers, an `0.78` answer gate, an `0.35` response-candidate evidence gate, and an `0.12` ambiguity margin. The settings favor abstention and remain provisional until §16 evaluates independently labeled tuning, release-gate, and final-test partitions. Nondeterministic producers require repeated-run dispersion or confidence reporting there; deterministic Section 5 fixtures prove only invariants and must not become training data.

The refreshed warmed offline benchmark is [benchmark-2026-08-15.json](benchmark-2026-08-15.json), version `section5-fusion-benchmark-v1.1`. On its recorded Windows/Python environment, the reinforced-pair fusion p50/p95 was 2.7806/4.8659 ms versus 0.0016/0.0019 ms for the former selection-only baseline; this deliberately compares policy and deterministic resource-accounting overhead, not resolver execution. All six deterministic acceptance fixtures passed. Fusion p50/p95 was 91.0377/99.4548 ms for 100 distinct candidates and 914.1076/1,161.5992 ms for 1,000, with 5,808,561 peak traced bytes and a 3,580,777-byte conservative fusion estimate at 1,000. The estimate remained within the 16 MiB frame budget and all six engineering gates passed. The synthetic harness uses an explicit permissive fixture authority and therefore measures formula/resource behavior, not authoritative-state accuracy. These are Section 5 engineering regression bounds, not Section 16 release SLOs.

## Verification fixtures

`tests/test_fusion.py` covers the feature vocabulary, concrete absence, frozen policy values and fingerprint, resolver-specific transforms, contribution retention, evidence deduplication, transparent scoring, agreement/support gating, ambiguity, scope/lifecycle/content conflicts, identity/type mismatch, exact selection, authoritative expiration/visibility/content revalidation, current history/authority inputs, order invariance, ID conflicts, decision memory codecs, explicit remaining-memory enforcement, typed resource exhaustion, and content-free reasons. Orchestration tests separately prove that resolver memory is reserved first and the fusion estimate appears in complete consumption. These are deterministic conformance checks, not calibration observations.

The complete verification record, including full-suite/static results and the scope-limited MCP regression, is [section5-conformance-2026-08-15.md](section5-conformance-2026-08-15.md).
