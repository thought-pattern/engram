# Candidate fusion and ambiguity contract v1

**Status:** Current implemented and qualified contract
**Owners:** `engram/fusion.py` and the fusion boundary in `engram/resolvers.py`
**Current policy:** `fusion-v1.0.0`, formula version 1

## Scope and selection boundary

The fusion layer ranks bounded resolver outputs. Text generation and
authoritative mutation remain with their owning components. Raw `ResolverResult` records retain each
contribution; the bounded fusion report links merged statements to sources,
features, evidence, weighted contributions, eligibility, and reason codes.

A unique exact result may answer after current-state revalidation. A non-exact
result requires agreement by two resolver families and a retained `SUPPORT`
reference. The eligibility order below handles conflicts, stale state, scope,
visibility, lifecycle, identity, type, and ambiguity.

## Canonical feature contract

Every canonical input has range `[0.0, 1.0]`, where larger is better.
`NormalizedFeatureSet` contains a concrete numeric value for every feature and a
separate availability set, distinguishing absent observations from measured zero.

| Feature | Meaning | Available from |
| --- | --- | --- |
| `exact` | Scoped normalized request equality | exact lookup |
| `pattern` | Bounded pattern specificity | pattern resolver |
| `lexical` | Calibrated lexical relevance | lexical score or overlap |
| `semantic` | Bounded semantic similarity | semantic model |
| `entity` | Query/candidate entity identity agreement | identity comparison |
| `relation` | Query/candidate relation agreement | relation comparison |
| `object_type` | Expected/candidate object-type agreement | object-type comparison |
| `support` | Presence of a retained linked support reference | support inspection |
| `history` | Bounded accepted-use history | authoritative history |
| `freshness` | Bounded time-recency signal | recency measurement |
| `authority` | Explicit source trust or authority input | supplied authority value |
| `agreement` | Distinct resolver agreement on one statement | deduplication |
| `margin` | Leading score minus runner-up score | two score-eligible candidates |

The executable definitions, including range and absence text, are the closed `FEATURE_DEFINITIONS` mapping. Tests assert exact coverage of the 13-member `FusionFeature` vocabulary.

## Resolver-specific normalization

Each raw resolver scale maps to named canonical features:

- `exact_match` is clamped to `[0, 1]` and mapped only to `exact`.
- `pattern_specificity = s` maps to `s / (s + 4)` after negative values are floored at zero.
- the lexical scorer's documented `[0, 1]` `lexical_score` is used directly; `lexical_overlap` is its fallback. The legacy test-only `score` alias is accepted only for a lexical source.
- `semantic_score` is the raw bounded similarity. `legacy_retrieval_score` and `vector_weight` are ignored because they are different or already blended scales.
- `entity_match`, `relation_match`, and `object_type_match` map only to their named canonical features; contextual resolution owns their trusted producers.
- `support_coverage` is accepted only from support-semantic candidates, lexical `recency` only from lexical candidates, and `authority_score` only from semantic candidates. Raw resolver `hit_rate` is ignored; history enters through current authoritative artifact and versioned feedback statistics.
- `priority`, `legacy_retrieval_score`, and `vector_weight` remain raw provenance and stay outside canonical features.
- an authoritative artifact can replace support and history with its current support links and `hit_count / query_count`. Authority is available only from an explicit finite `metadata.authority` value.

Central eligibility and the threshold/margin policy run after clamping.

## Deduplication and agreement

Candidates group by stable `statement_id`, then sort by the fixed source order exact, support semantic, pattern, lexical, standalone semantic, and utility. A response, scope, or lifecycle disagreement makes the group unselectable. Evidence IDs are deduplicated while every original candidate remains in its owning resolver result.

For compatible duplicates, positive relevance features take the maximum normalized observation. Entity, relation, object type, support completeness, freshness, and authority take the minimum, preserving mismatches and weak trust signals. Agreement is `min(1, (distinct_resolver_families - 1) / 2)`: one family is a measured zero, two families produce `0.5`, and three or more produce `1.0`. Distinct configured families determine agreement. The merged candidate gets a deterministic policy/request/statement-derived ID and preserves exact response bytes from the strongest compatible contribution.

The report is bounded to 16,384 encoded bytes, eight candidates, and eight inline
contribution summaries per candidate, with explicit omitted counts. Raw resolver
results follow the separate result-output budget.

`FusionDecision.working_memory_bytes` estimates the serialized working set for
inputs, evidence, normalized contributions, fused groups, and the report. The
orchestrator supplies the allowance remaining after resolver execution;
insufficient allowance returns `fusion_memory_exhausted` with an empty selection.

## Central eligibility order

Eligibility runs before a candidate can influence an answer decision:

1. require exact scope and `ACTIVE` lifecycle and reject same-statement response conflicts;
2. for artifacts, re-run lifecycle, validity-time, namespace-epoch, and repository availability policy against the captured request context;
3. require the authoritative response bytes, requested metadata, and requested source label to still match;
4. enforce ownership visibility: absent, `public`, and `scope` use the exact `ScopeKey`; `context` additionally requires the exact non-empty owner context fingerprint; unknown or private modes abstain when authoritative private-owner identity is unavailable;
5. treat explicit `support_complete: false` as answer-ineligible;
6. for non-exact answers, require at least two distinct resolver sources and an actually retained `SUPPORT` reference, regardless of an artifact's unexpanded support count;
7. treat an available zero entity or relation match as identity mismatch; when an expected object type is known, require an available nonzero object-type match.

Hard scope, lifecycle, current-state, source, metadata, visibility, and content
failures remove a group from scoring and evidence. Source-agreement and
identity/type mismatches keep a useful candidate at `EVIDENCE`.

## Formula and decision policy

For every available non-exact, non-margin feature `i`, formula version 1 calculates:

```text
weighted_average = sum(weight[i] * value[i]) / sum(weight[i])
score = exact + (1 - exact) * weighted_average
```

The sums include available inputs. `margin` has weight zero because it is
calculated after ranking. Exact match dominates at `1.0`. The versioned
weights and gates are serialized in every fusion report and have fingerprint
`1f9d19acaedc277b4916bc366e74dc8c03d921e335acd21ac7fc2f1b449d963c`.

The released gates are:

- `ANSWER >= 0.78`, provided answer eligibility passes;
- `EVIDENCE >= 0.35`, or an existing graph evidence reference is available; and
- top-two margin `>= 0.12` for an answer when a runner-up exists.

A close distinct runner-up always causes `EVIDENCE`, even if the leading score is high. Non-answer confidence remains concrete zero and unavailable in the unified result; `ANSWER` exposes the selected score as available confidence.

## Stable policy reasons

`FusionPolicyReason` is the closed non-content vocabulary for outcomes, ambiguity,
authority, scope, lifecycle, support, identity, type, and eligibility.

The orchestrator adds stable accounting and output-budget reasons. Policy reason
codes come from the closed vocabulary; response text and diagnostics remain data.

## Verification

`tests/test_fusion.py` verifies feature normalization, scoring, eligibility,
ambiguity, authoritative revalidation, budgets, and stable reasons.
