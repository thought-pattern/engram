# Section 4 unified resolution conformance — 12 August 2026, independently remediated 13 August 2026

**Historical timing note:** This report describes the v1 deadline-bearing contract. [Resolution contract v2](resolution-contract-v2.md) supersedes that timing policy; current execution reports elapsed time without a latency gate.

## Exit assessment

Section 4 meets its exit condition. Every currently implemented response-retrieval path has a pure adapter and emits a bounded common record. The deterministic executor is fail-soft, cooperatively lease-bounded, and exact-short-circuiting. Accepted-response candidacy and optional success are finalized once, after the final output outcome is known, through one durable atomic receipt. The transport-neutral core returns the conservative version 1 `ANSWER`, `EVIDENCE`, and `MISS` outcomes.

Fusion, ambiguity, general confidence policy, response-less Claim expansion/packaging, contextual relation-aware frames, and wire exposure were not pulled forward; they remain assigned to Sections 5, 7, 8, and 15.

## Requirement matrix

| Task | Implemented evidence | Verification evidence |
| --- | --- | --- |
| EGR-404 | `ResolutionBudget`, `BudgetConsumption`, `ResolverBudget`, `ResolverReservation`, and `BudgetLedger` | deterministic codecs, captured/recaptured deadlines, boundary, exhaustion, reservation, and truncation tests |
| EGR-401 | immutable versioned `QueryFrame`, object-type vocabulary, inheritance and rewrite trace records | codec, scope, trace, concrete absence, immutability, and unsupported-version tests |
| EGR-411 | `QueryFrameBuilder` with injected clocks, identity/scope validation, single preprocessing, eligibility capture, diagnostic hashing | standalone/authoritative, stale-deadline recapture, single-clock, and no-context-side-effect tests |
| EGR-402 | `Candidate`, `FeatureSet`, `EvidenceReference`, and `AccountingObservation` | proposal-local identity, exact Unicode, availability-versus-zero, nested bounds, null rejection, and codec fixtures |
| EGR-403 | `Resolver` protocol and typed `ResolverResult` states | fake-resolver failure, availability failure, skipped/unavailable/exhausted/completed state tests |
| EGR-410 | versioned `ResolutionResult` and closed outcome invariants | complete selected-candidate, confidence, top-level evidence, round-trip, and invalid-state tests for ANSWER/EVIDENCE/MISS |
| EGR-412 | pure lexical, pattern, structured graph, repository exact, and support-vector primitives; unchanged wrappers | pre/post statistic snapshots, graph-fallback trap, wrapper accounting, exact legacy result-shape tests |
| EGR-405 | `ExactResolver` over contextual repository lookup | canonical/alias provenance, exact response preservation, filters, scope, lifecycle, short-circuit tests |
| EGR-408 | `structured_graph_evidence` plus `StructuredGraphResolver` | graph-only reference tests, legacy ID derivation, pattern/graph separation |
| EGR-406 | `pattern_candidates` plus `PatternResolver` | captures, specificity, topic/that, miss, no graph fallback, no accounting mutation |
| EGR-407 | `query_candidates`, score-component extraction, and `LexicalResolver` | overlap, synonym, recency, hit-rate, priority, spelling/phrase diagnostics, unavailable lemma/stem markers, cooperative deadline, and working-set exhaustion tests |
| EGR-409 | bounded vector Claim query and support-index intersection in `SupportSemanticResolver` | linked/unlinked Claim fixture, scope/lifecycle filters, vector limit tests, and separate similarity/weight/priority/legacy-score features |
| EGR-413 | deterministic allow-listed `ResolverRegistry` and inspectable plan | order, configuration, cost, availability, duplicate and unknown resolver tests |
| EGR-414 | `ResolverExecutor`, adversarial truncation, cooperative deadline checks, and typed count/memory exhaustion | nested evidence/output/diagnostic/memory bounds, unavailable measurements, failure isolation, exact short-circuit, reservation tests |
| EGR-415 | bounded `ResolutionAccountingFinalizer` and atomic `FINALIZE_RESOLUTION_ACCOUNTING` receipt | output-downgrade safety, cross-resolver deduplication, invalid-acceptance atomicity, elapsed-stable retry, bounded eviction, persistence-reload replay |
| EGR-416 | `ResolutionOrchestrator` and `EngramCore.resolve_request` | exact ANSWER, non-exact EVIDENCE, MISS, deterministic core retry tests |
| EGR-417 | this report, contract, focused/full verification, benchmark, and MCP evidence | all gates below |

## Comprehensive review remediations

The completion review found and remediated the following issues before closure:

- caller-supplied budget deadlines are recaptured at the trusted boundary instead of trusting a possibly stale absolute clock value;
- availability-check exceptions become typed unavailable plan entries and cannot abort planning;
- executor bounds include nested candidate evidence and distrust over-reported graph, vector, diagnostic, output, and memory consumption;
- complete result envelopes are UTF-8 measured, deterministically truncated, and reported through exact final `output_bytes` consumption;
- exact artifact candidacy and success use one atomic durable mutation rather than two receipts with a crash window;
- internal receipt IDs use fixed-size hashes rather than unbounded caller text;
- artifact receipt replay after persistence reload does not double-credit repository, aggregate, keyword, or hit counters;
- vector result limits are pushed into the fixed graph-vector call;
- shared contraction preprocessing occurs once in frame construction and is not repeated by pure pattern discovery;
- the legacy `query()` wrapper again returns exactly `matches`, `keywords`, and `resolved_query` after a review caught leaked discovery diagnostics;
- PatternResolver uses the matcher’s public length contract instead of private storage;
- generic nested diagnostics have explicit item, string, and encoded-byte limits;
- all new dynamic mappings and test protocols were narrowed until the pinned Pyright gate reported no errors or warnings;
- final output-budget fitting now precedes accounting, so an exact candidate downgraded to `MISS` cannot receive accepted-success credit;
- resolver-owned local loops and retained working sets now perform cooperative deadline and memory checks, and the executor rejects results returned after the earlier aggregate/per-resolver deadline while the contract explicitly assigns in-flight external-call cancellation to backend and Section 15 controls;
- finalizer retry signatures exclude nondeterministic elapsed time, and its bounded retention is synchronized with the bounded core result cache;
- `ResolutionResult` construction now enforces every state needed for lossless codec round trips, including selected-candidate, confidence, and top-level-evidence rules;
- unavailable resource measurements remain unavailable instead of being rewritten as measured values; and
- support-semantic candidates expose raw similarity, vector weight, priority, and the legacy combined score as distinct features for Section 5 normalization.

## Verification results

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_resolution_contracts.py tests/test_resolvers.py -q` | 60 passed |
| `python -m pytest -q` | 1,231 passed in 75.83 seconds; no expected failures |
| `python -m ruff check engram scripts eval tests` | all checks passed |
| `python -m black --check -q -W 1 -l 132 -t py311 <Section 4 files>` with an isolated cache | all files unchanged |
| `npx --yes pyright@1.1.411` | 0 errors, 0 warnings, 0 informations |
| `python -m vulture <Section 4 production files> --min-confidence 65` | no findings |
| `python -m bandit -q -lll <Section 4 production files>` | no high-severity findings |
| `python -m compileall -q engram scripts eval tests` | passed |
| `git diff --check` | passed |
| `python scripts/benchmark_resolution.py` | historical v1 latency/resource assessment passed; the current runner reports timing and assesses memory only |
| Section 4 invocation of `scripts/run_section3_mcp_conformance.py` | 1,000/1,000 turns and 1,003 protocol calls passed |

## Performance evidence

The reproducible offline result is [section4-benchmark-2026-08-12.json](section4-benchmark-2026-08-12.json), generated by `scripts/benchmark_resolution.py` with 200 samples and a 1,000-statement lexical corpus.

| Operation | p95 | Gate |
| --- | ---: | ---: |
| Frame construction | 1.0460 ms | 5 ms |
| Exact adapter | 1.2379 ms | 5 ms |
| Lexical adapter, 1,000 statements | 53.1342 ms | 100 ms |
| Completed executor | 0.2686 ms | 5 ms |
| Failed executor translation | 0.1475 ms | 5 ms |
| Resolver-result codec | 0.9209 ms | 5 ms |
| Peak traced lexical memory | 108,950 bytes | 16,777,216 bytes |

These are engineering regression gates, not Section 16 release SLOs or held-out relevance claims.

## MCP evidence

The fresh official-client evidence is [section4-mcp-conversation-1000-turns-2026-08-12.json](section4-mcp-conversation-1000-turns-2026-08-12.json). It called `engram_start`, issued 1,000 sequential `engram_send` calls, then called `engram_inspect` and `engram_stop`. Every turn returned a nonempty response; turn numbering, inspect count, and stop exchange count all reached 1,000. The run completed 1,003 MCP calls with p95 turn latency 10.6658 ms. The bounded artifact stores no raw prompt or response bodies.

Section 4 remains intentionally transport-neutral; the run is a cross-interface regression of the actual MCP deployment path, while Section 15 retains ownership of exposing unified resolution on the wire.

## Residual ownership

- Section 5 must normalize and fuse non-exact candidates, handle ambiguity, and calibrate general confidence.
- Section 7 must retrieve, validate, score usefulness, and package response-less Claims.
- Section 8 must populate contextual frame fields and compile canonical relation-aware graph plans.
- Section 15 must design and review MCP/gRPC representations for unified resolution.
- Section 16 must establish held-out quality, release, and operational gates.

No residual item prevents Section 4 completion.
