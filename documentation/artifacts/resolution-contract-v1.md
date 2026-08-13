# Unified resolution contract v1

**Status:** Implemented and verified on 12 August 2026; independently remediated on 13 August 2026  
**Owners:** `engram/resolution.py`, `engram/resolvers.py`, and the transport-neutral `EngramCore.resolve_request` boundary

## Purpose and ownership

Version 1 supplies the bounded execution substrate for the existing Engram retrieval paths. It does not define candidate fusion, ambiguity policy, response-less Claim packaging, contextual relation planning, or a wire representation. Those remain assigned to Sections 5, 7, 8, and 15 respectively.

The baseline policy is deliberately conservative:

- one unique currently eligible exact accepted-response artifact may return `ANSWER`;
- non-exact response candidates or already available minimal graph references return `EVIDENCE` without authorizing a response; and
- no usable output returns `MISS`.

## Versioned records

The following records use closed `schema_version = 1` codecs, exact-field decoding, finite numeric validation, bounded strings and collections, immutable nested mappings, and concrete absence values rather than `None` or JSON `null`:

| Record | Purpose | Concrete absence |
| --- | --- | --- |
| `ResolutionBudget` | Configured total/per-resolver time, cost classes, candidate, graph, vector, evidence, output, diagnostic, and working-memory limits | limits remain numeric; disabled resource classes use zero where permitted |
| `BudgetConsumption` | Measured aggregate or per-resolver use and sorted exhaustion dimensions | zero counters and `[]` |
| `QueryFrame` | Original/resolved text, identity, object type, traces, scope, filters, captured budget/eligibility, diagnostic ID | `[]`, `{}`, and `""` |
| `FeatureSet` | Generic numeric features with explicit unavailable feature names | `{}` and `[]` |
| `EvidenceReference` | Stable minimal graph/support reference with resolver, kind, scope, provenance, and diagnostics | `{}` |
| `Candidate` | Proposal-local ID, exact response text or non-answer response candidate, source, features, evidence, scope, lifecycle, and provenance | empty evidence/provenance/diagnostics containers |
| `AccountingObservation` | Pure statement candidacy and keyword observation | `[]` |
| `ResolverResult` | Completed, unavailable, skipped, exhausted, or failed invocation output | empty candidates/evidence/accounting/diagnostics |
| `ResolverBudget` | Read-only per-invocation lease calculated from remaining allowance | zero remaining dimensions |
| `ResolverReservation` | Inspectable lease and resulting consumption for one invoked resolver | concrete lease and consumption records |
| `ResolutionResult` | `ANSWER`, `EVIDENCE`, or `MISS`, resolver diagnostics, and final consumption | explicit selected-candidate availability and empty collections |

Every decoder rejects unknown fields and unsupported versions. `QueryFrame`, candidates, evidence, resolver diagnostics, and result diagnostics freeze nested values. Generic nested values are limited to eight levels, 4,096 items, 16,384 UTF-8 bytes per string, and 65,536 encoded UTF-8 bytes per mapping. Resolver/result collections are independently bounded by the configured execution envelope and contract maxima.

## Frame construction

`QueryFrameBuilder` is the trusted construction boundary. It:

1. validates the request and exact `ScopeKey`;
2. accepts a structurally valid authoritative Section 1 identity or builds the standalone identity;
3. validates authoritative identity/retrieval normalization consistency and exact scope agreement;
4. performs contraction expansion once;
5. recaptures caller-supplied budget limits against the injected monotonic clock so stale deadlines cannot cross the boundary;
6. captures one immutable eligibility context against the injected UTC clock and namespace epoch state; and
7. derives a bounded non-disclosing diagnostic ID from the supplied logical seed.

Base construction performs no graph call, contextual inheritance, retrieval rewrite, dependency loading, session mutation, persistence mutation, or transport translation. The expected object type is `UNKNOWN`, and inheritance/rewrite traces are empty until their owning later sections populate them.

## Resolver order and adapter behavior

The default allow-listed order is exact, pattern, lexical, structured graph, then support semantic. The registry retains a bounded plan entry for every registered resolver and records `not_configured`, `cost_class_disabled`, `dependency_unavailable`, or `availability_check_failed` without aborting later work.

- Exact lookup uses the authoritative Section 3 repository, scoped canonical/alias index, captured eligibility context, and required metadata/source filters. The accepted response is not rewritten.
- Pattern discovery returns statement-backed captures, specificity, `that`, and topic diagnostics without graph fallback or accounting mutation.
- Lexical discovery exposes aggregate score, IDF overlap, exact/synonym ratios, recency, keyword hit rate, priority, spelling correction, and phrase-mode signals. Lemma/stem contributions are explicitly unavailable because the current lexical scorer does not calculate separate numeric contributions.
- Structured graph recall returns bounded minimal references. It preserves a graph Claim ID when present and derives a deterministic `legacy-graph:sha256:` reference only for legacy rows without an ID.
- Support semantic recall pushes the vector result limit into the fixed Claim-vector query, intersects matches through the support reverse index, reapplies artifact scope/lifecycle/metadata/source filters, and returns only support-linked accepted responses. Its candidate features keep raw semantic similarity, configured vector weight, statement priority, and the legacy combined retrieval score separate so Section 5 does not calibrate a blended value as semantic similarity.

`Engram.query`, `Engram.pattern_query`, and existing proposal behavior remain compatibility wrappers with their original accounting and result shapes. New discovery methods do not mutate sessions, query/hit statistics, persistence, or graph phrasing state.

## Execution and truncation

`ResolverExecutor` processes the deterministic plan sequentially with one injected monotonic clock. It calculates each lease from the remaining aggregate allowance, uses the earlier of the total and per-resolver deadlines, records a `ResolverReservation`, rejects a result that returns at or after that deadline, translates other resolver exceptions to `FAILED`, and retains earlier successful results. Executor-observed elapsed time replaces untrusted resolver-reported elapsed time. Concrete adapters invoke the lease deadline before and after expensive calls and inside local loops. Lexical, pattern, structured-graph, and support-semantic discovery bound their retained working-set estimates and return typed exhaustion without partial ranking. An external database or model call already in progress is not preempted by this cooperative executor; its server/driver timeout and cross-adapter cancellation remain required operational controls under Section 15. Availability failures, disabled cost classes, total deadline exhaustion, resolver-count exhaustion, per-resolver time exhaustion, and empty resource dimensions use typed states/reasons.

The executor distrusts resolver reports. It deterministically truncates candidates, nested and top-level evidence, serialized output, diagnostics, graph rows, vector results, and working-memory estimates to the lease. An eligible exact result short-circuits only when it is the single exact candidate returned by the exact resolver.

The orchestrator then measures the complete UTF-8 `ResolutionResult`, including its mandatory envelope and diagnostics, before it authorizes accepted-success accounting. Budgets reserve at least 4,096 bytes for a representable result. If the full result exceeds the configured bound, resolver diagnostics, evidence, and response candidates are removed in deterministic order; an exact response that cannot fit becomes `MISS` rather than an incomplete `ANSWER`. Such a candidate receives candidacy accounting but never accepted-success credit. A pessimistically sized accounting preview ensures the subsequent atomic finalization cannot make the selected outcome exceed the budget. Final `BudgetConsumption.output_bytes` equals the actual encoded result size.

## Accounting and retry semantics

Resolvers return observations but never apply statistics. `ResolutionAccountingFinalizer` deduplicates by statement ID after aggregation. Accepted-response artifacts use one atomic `FINALIZE_RESOLUTION_ACCOUNTING` mutation receipt that increments every unique candidate query count and an optional accepted hit in one repository/checkpoint publication. Internal receipt IDs are fixed-size SHA-256 derivations of the external request ID.

Receipt replay survives persistence reload and does not double-credit artifact, aggregate, keyword, or hit counters. Legacy dictionary statements use the existing locked compatibility counters and a bounded in-process logical request cache whose stable signature excludes elapsed time. `EngramCore.resolve_request` retains a matching bounded 1,000-entry immutable result cache, discards the corresponding transient accounting entry on eviction, and rejects changed input while the logical request remains within that advertised in-process horizon. Durable artifact receipts continue to own restart and longer-lived mutation replay semantics.

## Outcome invariants

- `ANSWER` requires exactly one selected exact candidate, `selected_candidate_available = true`, that candidate as the sole response candidate, no top-level evidence, and confidence `1.0` marked available. Later non-exact resolvers have not run.
- `EVIDENCE` uses the one canonical empty candidate, has no selected candidate, and must contain at least one response candidate or evidence reference. Confidence is exactly zero and unavailable rather than inferred.
- `MISS` uses the one canonical empty candidate and has no selected candidate, response candidates, or evidence references. Confidence is exactly zero and unavailable; execution diagnostics and budget consumption remain available.

These invariants are constructor-enforced as well as decoder-enforced, so every valid in-memory value round-trips through its deterministic codec. Executor normalization also preserves `measurement_available = false`; it never converts an unavailable observation into a measured zero.

Normal outcomes include the diagnostic ID, inspectable plan, invoked reservations, bounded resolver results, accounting disposition, stable baseline reason codes, and aggregate consumption. Output-constrained results preserve the diagnostic ID, a bounded accounting summary, truncation reasons, and exact aggregate consumption while omitting details that do not fit.

## Verification

Executable coverage is in `tests/test_resolution_contracts.py` and `tests/test_resolvers.py`. The complete evidence matrix, static gates, performance measurements, compatibility review, and MCP soak are recorded in [the Section 4 conformance report](section4-conformance-2026-08-12.md).
