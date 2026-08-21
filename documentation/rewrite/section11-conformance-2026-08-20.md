# Section 11 symbolic rewrite conformance

Date: 2026-08-20  
Result: component engineering PASS; release approval remains false

## Implemented boundary

`engram/rewrite.py` supplies exact version-1 rule validation, package corpus loading, deterministic matching, bounded execution, trace population, and structural lint. `EngramCore` eagerly loads the corpus only when `retrieval_rewrites_enabled` is true and applies it after contextual enrichment but before resolver planning. The authoritative query identity is unchanged.

The pattern resolver reads the pre-rewrite representation. All retrieval resolvers read the final representation, and a rewritten exact hit cannot short-circuit the remaining configured resolvers. Focused tests prove both directions: a pattern that matches only rewritten text remains a miss, while a pattern matching the original request remains eligible. Rewrites therefore cannot act as AIML redirects or response templates.

Resource stops are explicit. The standalone engine reports depth, expansion, cycle, output, or time limits; frame integration rejects any non-fixed-point result before resolver planning. Cooperative caller cancellation propagates without publishing a partial frame.

## Corpus and lint

The production corpus contains 23 rules across all eight required classes. The repository-visible engineering corpus contains 23 positive cases and 12 negative controls. Provenance and licensing are recorded in `provenance-and-licensing-2026-08-20.md`.

`python scripts/lint_rewrite_corpus.py` passed with zero errors. It checks cycles, unreachable rules, input collisions, overbroad rules, duplicate outputs, regression reachability, and unexpected identity loss. It reports two warnings:

- six independently constrained question/repair prefixes intentionally remove their prefix and therefore share the empty output template;
- two separately constrained contextual rules intentionally produce `what about {subject}`.

Neither warning is an input collision or an unreachable rule. The machine-readable result is `lint-2026-08-20.json`.

## Engineering promotion comparison

`python scripts/benchmark_rewrites.py --repeats 100` passed the frozen repository-visible gate:

| Measure | Baseline | Rewrites | Verdict |
| --- | ---: | ---: | --- |
| Exact recall | 3/23 (13.04%) | 23/23 (100%) | +86.96 percentage points; required +25 |
| Semantic collisions | 0 | 0 | Pass, maximum 0 |
| False direct answers on negative controls | 0/12 | 0/12 | Pass, maximum 0.5% |

The 3 baseline hits are contraction cases already equal under identity retrieval normalization; the remaining 20 gains come from the new reduction layer. Across 3,500 observations, rewrite duration was p50 0.50945 ms, p95 0.6955 ms, p99 0.8665 ms, and maximum 2.1152 ms. Durations are observations, not an answer-policy gate. The complete result is `benchmark-2026-08-20.json`.

## Verification

- Focused rewrite suite: 16 passed.
- Related resolution/configuration/context suite: 188 passed before the final full run.
- Full repository suite: 1,516 passed in 125.58 seconds.
- Ruff: pass.
- Black: 129 files unchanged after formatting.
- isort: pass.
- Pyright: 0 errors, 0 warnings, 0 informations.
- Bandit medium/high scan: pass.
- Bandit broad scan: 18 reviewed low-severity findings, zero medium, zero high; the one Section 11 addition is a false positive on the `token_sequence` enum string.
- Vulture: pass when generated protobuf modules are excluded; its only broad-scan findings are generated imports.
- Pinned `requirements.txt` audit: no known vulnerabilities (`pip-audit --no-deps --disable-pip`); dependency hashes remain a future supply-chain hardening item.

The trace fixtures are `trace-fixtures-v1.json`. The governed source digest shared by lint, benchmark, the Section 16 foundation, and the final MCP run is `f7d7804b89b8eef7703d4b8c124c4fac0d4236d76268ee6933bfdb8d61444f52`. The digest includes the packaged production JSON corpus as well as code, scripts, evaluation JSON, tests, and operational configuration.

## Live MCP and MemGraph

The final artifact `section11-mcp-sarah-preferences-memgraph-enabled-1000-turns-2026-08-20.json` uses the official MCP client against the repository MCP server, supplies the configured live MemGraph endpoint, and enables retrieval rewrites through an ephemeral configuration. MemGraph preflight reported enabled and ready.

Sarah declared that she likes sushi and cats and dislikes dogs. All 1,000 turns received the complete structural evaluation plus profile or live-graph continuity checks. All 1,000 passed, with zero failed turns. Sources were 995 pattern replies and five explicit MemGraph replies at turns 200, 400, 600, 800, and 1,000. Observed complete-turn p95 was 3.8110 ms and maximum was 4,253.2911 ms; no timing gate was applied.

This MCP run is adapter/startup/non-regression evidence because the current MCP surface does not expose the newer unified resolution request. The focused transport-neutral integration tests are the evidence that rewritten exact lookup and pattern isolation execute correctly.

## Release custody

This evidence promotes the optional component only. The evaluation corpus is repository-visible engineering data, not an independently controlled Section 16 protected partition. No numerical release owner has approved the pending Section 16 gates, so `release_ready` and `release_approved` remain false. The feature therefore remains disabled by default.
