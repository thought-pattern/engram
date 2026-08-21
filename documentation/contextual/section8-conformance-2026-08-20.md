# Section 8 conformance report — 2026-08-20

Status: **COMPONENT PASS — NOT RELEASE QUALIFIED**

Section 8 provides compact per-user query context, bounded elliptical completion, canonical entity and Predicate resolution, and fixed one-hop Claim lookup in deterministic fixtures and the configured live MemGraph. It preserves the existing Section 7 disclosure and evidence authority: a relation phrase is emitted only for one eligible unambiguous result, and a lone graph result remains `EVIDENCE` rather than becoming an unsupported `ANSWER`.

The original PASS claim did not exercise `config.yml`; the MCP runner omitted `config_path`, and the 15-turn relation benchmark used an in-memory graph fixture. The corrected live run verifies Sarah/`married_to`/Abraham through the fixed capabilities and the complete core. Resolution budget v2 removes uncalibrated time limits from answer policy and reports turn length instead.

## Requirement evidence

| Task | Implementation and verification | Result |
| --- | --- | --- |
| EGR-801 compact previous frame | `engram/contextual.py`, session fields/codecs in `engram/models.py`, and `EngramCore` persistence; `tests/test_contextual.py` | Exact bounded codec, persistence/reload, legacy absence, user isolation, and turn bounds pass. |
| EGR-802 bounded inheritance | Missing-field-only enrichment with source-turn provenance, two-turn distance, confidence, topic, reset, TTL, and idempotent replay tests | Self-contained and cross-user requests do not inherit; elliptical follow-ups do. |
| EGR-803 operator reuse | The Section 1 `QueryOperator` extractor and closed vocabulary are reused | WHO, WHAT, WHERE, WHEN, WHICH, HOW_MANY, LOOKUP, EXISTS, COUNT, COMPARE, and UNKNOWN cases pass. |
| EGR-804 canonical subjects | Fixed label/alias/edge query plus NER surface evidence and caller canonical IDs | Unique, ambiguous, miss, alias, edge-surface, NER, and explicit-identity behavior pass. |
| EGR-805 Predicate candidates | Explicit identity, frame relation, dependency lemma/preposition, lexical fallback, labels, and synonyms | Paraphrase, synonym, type disambiguation, explicit identity, and ambiguity behavior pass. |
| EGR-806 expected type | Closed inference over PERSON, PLACE, DATE, NUMBER, BOOLEAN, ENTITY, and UNKNOWN | Type matches are measured; mismatch suppresses phrasing; UNKNOWN is not rejected prematurely. |
| EGR-807 one-hop plan | Strict `OneHopQueryPlan` with one allow-listed template and bounded rows | Extra fields, ambiguous identities, unsupported templates, arbitrary Cypher/procedure inputs, and obsolete timeout fields cannot compile. |
| EGR-808 execution and filtering | Fixed parameterized graph capability, existing `ClaimProjection`, current Section 7 evaluator, by-ID revalidation, resolver lease | Fixture checks pass; live canonical entity and Predicate lookup select Sarah and `married_to`, and the fixed one-hop query returns Abraham in 53.1550 ms. |
| EGR-809 phrase and evidence | Deterministic single-result phrase, Claim evidence features/reasons, and fusion authority revalidation | The configured live core completes as `EVIDENCE`, retains one Claim and `Sarah — married to: Abraham.`, and reports 8,071.1110 ms without time-based suppression. |
| EGR-810 benchmark | Versioned engineering regression corpus and reproducible runner | All 15 turns pass, covering both ambiguity classes, paraphrases, technical/explicit references, elliptical replacement, reset, and unknown type. The repository-visible split is not independent release evidence. |

The executable contract is [Contextual relation contracts v2](contracts-v2.md). The engineering corpus is [`eval/section8-relation-followup-v1.json`](../../eval/section8-relation-followup-v1.json), and its source-bound machine-readable result is [benchmark-2026-08-20.json](benchmark-2026-08-20.json). The legacy `held_out` key is an engineering regression split only; Section 16 must supply independently held content.

## Verification

- Fresh post-policy-change full unit suite: **1,442 passed in 125.05 seconds**.
- Focused resolution/fusion/contextual/relation/service/evaluation suite: **203 passed in 75.91 seconds**.
- In-memory engineering benchmark: **15/15 turns passed**. Its durations are descriptive, it is not a live MemGraph measurement, and it is not a release gate.
- Pyright: **0 errors, 0 warnings, 0 information messages**.
- Ruff, Black, Bandit high-severity scan, and Vulture: **clean**.
- Unit-test value review: [62 low-value or rigid tests plus seven obsolete latency-budget cases removed](../evaluation/unit-test-value-audit-2026-08-20.md); the remaining pytest suite tests behavior rather than source-shape gates.

The review corrected production defects instead of treating the tests as authoritative: technical standards such as RFC 7231 needed explicit identity extraction; contextual expected-type filtering incorrectly affected inactive candidates; and relation phrasing required a second current-Claim authority check. The final semantic pass also made operator inheritance obey topic/distance bounds, aligned the real graph client's ten-row maximum with the plan contract, rejected out-of-binding graph rows, and stopped falsey serialized frame fields from masquerading as legacy absence. A malformed duplicate-qualifier fixture was corrected because it failed before reaching the intended compact-frame boundary, and an attempted unknown-type phrasing assertion was removed because evidence eligibility does not guarantee fusion-level phrase retention.

## Direct MCP conversation

The fresh [Sarah/sushi artifact](section8-mcp-sarah-sushi-1000-turns-2026-08-20.json) records **1,000 completed and 1,000 evaluated turns** through the official MCP `Client` directly against the repository `MCPServer`, using `user_id` **Sarah**. Sarah states that sushi is good at the start of each ten-turn cycle and asks `What's good?` three times per cycle, producing 300 explicit sushi-recall evaluations.

Every turn received the 16 ordinary protocol/continuity checks plus `sushi_recall_when_asked`. All **17,000 per-turn checks passed**; the ordered pass run is `1 × 1000`, failed turns and failure counts are empty, inspection reports Sarah at turn 1,000, and stop reports 1,000 exchanges. Latency was **3.9773 ms p50** and **10.3146 ms p95**; the first-turn initialization maximum was **448.2917 ms**. The compact artifact stores hashes and summary evidence rather than raw conversation content.

## Live MemGraph comparison

The [comparison report](memgraph-conversation-comparison-2026-08-20.md), [machine-readable artifact](mcp-memgraph-comparison-2026-08-20.json), and [`scripts/compare_mcp_memgraph.py`](../../scripts/compare_mcp_memgraph.py) provide the missing graph-enabled evidence.

In a no-seed five-turn MCP A/B, graph-disabled Engram returned five empty `none` results. Graph-enabled Engram reported `graph_enabled: true` and `graph_ready: true` and returned graph facts on all five turns, with source `graph`. Warm graph turns took 1,393.5619–2,960.6501 ms versus 1.7008–6.4706 ms when disabled. The graph-enabled initialization turn took 3,940.5248 ms.

The two seeded 1,000-turn controls both passed all 17,000 checks and produced identical ordered response and observation hashes. That equality is expected: the seed's `*` pattern satisfies every turn before the legacy graph fallback can run. The earlier Sarah/sushi result was valid continuity evidence but not graph-retrieval evidence.

The direct Section 8 probe found one Sarah entity in 1,740.8917 ms, one `married_to` Predicate in 1,482.5165 ms, and one Abraham one-hop Claim in 53.1550 ms. The complete core request took 8,071.1110 ms, returned `EVIDENCE`, retained one Claim and the Sarah/Abraham response candidate, and reported no exhausted dimensions.

The live review corrected four integration defects: invalid MemGraph `MATCH` placement after `OPTIONAL MATCH`, Predicate compatibility with the existing `label` property, graph replies misreported as source `pattern`, and uncalibrated latency limits suppressing valid evidence. Focused tests cover the retained behavior and prove that elapsed time is reported without changing a completed result. No graph writes were performed.

## Deferred ownership

Sections 9 and 10 subsequently supplied temporal/conflict and bounded multi-hop component behavior. Section 15 owns remaining adapter and operational integration. Section 16 owns independent custody, release-scale calibration, and approval. None of those release outcomes is claimed by this report.
