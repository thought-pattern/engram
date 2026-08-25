# Section 15 conformance report — 2026-08-23

**Status:** All ten engineering tasks complete; Section 16 release qualification remains open
**Governed source SHA-256:** `f6ce0c0166dc9af13a92c3ab362576e9f62b77959e606ab0f5f66d03d1a93e78`

## Requirement audit

| Task | Completion evidence |
| --- | --- |
| EGR-1501 | The [interface matrix](section15-interface-contracts-v1.md) maps identity, lifecycle, indexes, resolution/evidence, feedback, persistence, and readiness to one `EngramCore` or lower-layer owner. Python, MCP, and both gRPC services translate rather than reimplement those rules. |
| EGR-1502 | The [Python API v1 contract](../python-api.md) defines the stable mapping-only identity, scope, resolution, evidence-package, feedback, concrete-absence, and cancellation boundary. Existing lower-level calls remain compatible and require no deprecation warning. |
| EGR-1503 | MCP remains exactly ten tools. The official-protocol test freezes every tool name, description, input property, default, required field, and absent output schema. No Section 15 MCP tool was added or changed. |
| EGR-1504 | The unchanged `engram.v1` descriptor retains its 14 RPCs. The separate `engram.v2.EngramEvidenceService.ResolveEvidence` exposes the versioned unified result and evidence package, delegates to the core once, propagates cooperative cancellation, and has byte-for-byte regeneration coverage for Python, typing, and gRPC stubs. |
| EGR-1505 | The [schema-management contract](persistence-schema-management-v1.md) covers the exact cross-feature manifest, startup validation/rebuild/readiness, bounded quarantine summary, explicit non-overwriting migration output, idempotence proof, backup use, and no-automatic-downgrade rule. |
| EGR-1506 | The [concurrency/idempotency contract](section15-concurrency-idempotency-v1.md) and network tests prove exact/conflicting retries, a six-way Python/MCP/gRPC proposal race, single publication, consistent visibility, Python and v2 cancellation, v1/MCP started-work behavior, deadlines, durability recovery, and graceful shutdown without adapter-owned coordination. |
| EGR-1507 | The [security/privacy review](security-privacy-review-2026-08-20.md) records shared UTF-8 and collection bounds, strict identifiers, fixed read-only graph access, deployment-owned mutation authority, and exception-content redaction. Malformed nested Unicode now becomes the same typed boundary error rather than escaping as an encoder failure. |
| EGR-1508 | Required NLTK data and enabled local spaCy/model artifacts are preflighted; optional graph/index/model readiness remains explicit. Serving paths use check-only resource calls and local-only model loading, with no runtime download or dependency acquisition. |
| EGR-1509 | The [telemetry contract and cardinality review](section15-operational-telemetry-v1.md) defines one process-lifetime, fixed-key aggregate for outcomes, resolver contribution/state, Regulator rejection categories, latency, resources/exhaustion, rebuilds, durability, and compatibility gauges without raw or high-cardinality values. |
| EGR-1510 | The [deployment and rollback runbook](deployment-and-rollback-v1.md) covers standalone, graph, semantic/utility, and Tapestry modes; readiness; startup rebuild; degradation; backup/migration; repair; feature/policy rollback; authority; and incident response. Its existing isolated rollback exercise remains applicable. |

Every required Section 15 evidence class exists: cross-adapter matrix, generated-stub check, persistence compatibility tests, concurrency tests, security review, offline-start tests, telemetry cardinality review, rollback exercise, and live MCP evidence.

## Comprehensive review and remediation

| ID | Defect | Remediation and evidence |
| --- | --- | --- |
| S15-REV-01 | The MCP harness reported optional enablement from command flags, so a supplied configuration could be described incorrectly. | Read sparse, semantic, reranker, utility, graph, and rewrite state from the runtime/component configuration. The final artifact reports every enabled component as ready and rewrites as enabled. |
| S15-REV-02 | The production catch-all seed intercepted graph probes, so the first 1,000-turn attempt tested pattern priority instead of live graph fallback. | Retained the [failed diagnostic artifact](section15-mcp-conversation-1000-turns-2026-08-23-failed-default-seed.json), which correctly failed 700 turns, then used the existing no-catch-all Section 11 evaluation seed. The final run has five graph-sourced probes. No production matcher behavior was weakened to make the test pass. |
| S15-REV-03 | A nested unpaired Unicode surrogate could pass JSON construction and raise `UnicodeEncodeError` while creating an idempotency signature. | Translate that encoder failure to `InvalidRequestError` at the existing shared signature boundary and cover it in the security suite. No generic sanitizer or replacement encoding was added. |
| S15-REV-04 | The current style guide and AST source-contract test exempted generated `engram/v1` files but omitted the new generated `engram/v2` directory. | Name both generated directories explicitly; regeneration remains the authority and generated files are not reformatted or hand-edited. |
| S15-REV-05 | Dependency-free static CI would make Pyright fail on absent heavy third-party runtime imports, encouraging installation of the entire application dependency tree just to lint it. | Suppress only `reportMissingImports` in the Pyright project configuration. Project modules remain analyzed; dependency availability remains owned by installation and startup preflight. CI continues to run only analysis tools plus runner-provided Node for `pyright@latest`. |
| S15-REV-06 | Current contributor and deployment text still required `black --check` and described the implemented v2 evidence API as future work. | Align the style guide with write-mode Black and update README/runbook text to distinguish unchanged v1 from the separate current v2 service. |
| S15-REV-07 | After switching the harness to actual component state, four obsolete boolean parameters remained and failed the dead-code gate. | Remove the unused plumbing; flags still construct the temporary configuration in `main`, while `_run` reports actual state. Vulture is clean. |
| S15-REV-08 | The host environment had `tzdata 2024.1`, while repository metadata pins `2026.3`. | Align the development environment to the declared `tzdata 2026.3` dependency and verify that the final artifact reports it. No runtime downloader was introduced. |

The review specifically checked for excessive defensive behavior. Section 15 adds no internal RBAC/token subsystem, retry framework, graph timeout policy, runtime downloader, alternate answer path, persisted telemetry event stream, or dynamic-label collector. New broad exception boundaries either record a rebuild/checkpoint failure and re-raise, retain an already-documented optional-component fail-soft boundary, or convert an unexpected gRPC adapter failure to a generic internal response. They do not silently replace knowledge or fusion behavior. The explicit migration revalidation and source-preserving output are required operator checks, not serving-path redundancy.

### Second-to-last end-to-end review

| ID | Defect | Remediation and evidence |
| --- | --- | --- |
| E2E-01 | The negative-cache fail-open block also swallowed validation, request publication, contextual checkpoint, and telemetry failures after a confirmed hit. | Limit fail-open handling to negative ownership, key construction, lookup, and hit construction. Two focused regressions prove checkpoint and telemetry failures remain visible without falling through to ordinary resolvers; the existing lookup-failure test continues to prove the optimization itself fails open. |
| E2E-02 | NLP fact and entity extraction converted every tokenizer, tagger, or chunker defect into ordinary absence. | Remove both blanket catches. Two dependency-failure cases now propagate while ordinary no-fact/no-entity inputs retain concrete empty results. |
| E2E-03 | Template graph and triple callbacks converted unexpected callback defects into empty recall. | Call the graph owner directly. Expected configured graph unavailability is already normalized by that owner; two focused tests prove unexpected callback defects remain visible. |
| E2E-04 | The repository-visible Section 13 corpus and benchmark used `release` partition and metric names despite having no Section 16 custody or authority. | Rename the public partition, query IDs, variables, metric keys, and documentation to `engineering_holdout`; add explicit non-release metadata, source-contract coverage, benchmark schema 2, and a regenerated source-bound artifact. |
| E2E-05 | ADR 0004 described a numerical table as accepted first release gates before Section 16 had approved project gates. | Supersede the old table, make the Section 16 manifest the sole numerical authority, retain timing as observation only, and add a source-contract check for the authority marker. |
| E2E-06 | The semantic benchmark imports are not both declared as direct development dependencies. | Intentionally not changed at review direction. Runtime dependency declarations remain unchanged. |
| E2E-07 | Suppressing third-party missing imports in Pyright also left no focused static proof that project-owned imports resolve. | Add an AST source-contract check over `engram`, `scripts`, and `eval`, and run that focused test in dependency-light CI. |
| E2E-08 | Write-mode Black in CI could repair formatting and still let the job pass. | Keep write-mode Black and follow it with `git diff --exit-code -- '*.py'`; no `black --check` mode or application deployment was added. |
| E2E-09 | The development plan imposed full-suite, migration, concurrency, adapter, benchmark, and rollback evidence on every work item regardless of its boundary. | Split universal completion evidence from short, boundary-specific conditional evidence. Section 16 release qualification remains separate. |
| E2E-10 | Composition caught `Exception` and then defensively tested for two `BaseException` subclasses that the catch could never receive. | Remove the unreachable branch while preserving the existing dependency-failure result for ordinary query exceptions. |

These changes deliberately remove silent fallback and unreachable defense instead of adding retry, transaction, schema, or policy frameworks. The limited-system boundary remains unchanged.

## Verification

The second-review affected gate passed 137 feedback, NLP, graph, composition, semantic, evaluation-foundation, and source-contract tests. The complete repository then passed:

```text
1,650 passed in 124.80s
```

Repository-wide gates passed:

- `black -l 132 -t py311 .` — final pass left 147 files unchanged;
- `isort --check-only .`;
- `ruff check --line-length 132 .`;
- `python -m compileall -q engram scripts eval tests`;
- `bandit -q -ii -lll -r engram scripts eval`;
- `vulture`; and
- `pyright` — 0 errors, 0 warnings, and 0 informational diagnostics.

The full suite includes both v1/v2 generated-stub reproducibility, adapter network behavior, offline preflight, persistence compatibility, concurrency, redaction, telemetry, source-style, and rollback-document link checks.

## Required live MCP conversation

The [final machine-readable artifact](section15-mcp-conversation-1000-turns-2026-08-23.json) records:

- official MCP client against the repository MCP server;
- Sarah as the isolated user, liking sushi and cats and disliking dogs;
- 1,000 requested, completed, and evaluated turns and 1,003 tool calls;
- 1,000 passing turns, zero failed turns, and zero failure categories;
- 16 invariant checks plus exactly one applicable profile/graph check on every turn. The artifact lists 18 possible names because preference continuity and live-graph source checks apply to different turns;
- one run-length pass record of 1,000 consecutive passes;
- live MemGraph enabled and ready, with five scheduled graph questions returning source `graph` and 995 ordinary profile turns returning source `pattern`;
- sparse, semantic, reranker, utility, and retrieval rewrites enabled, with all enabled optional components ready;
- packaged timezone data `2026.3`;
- observed p50/p95/maximum turn durations of 2.9979/4.7168/4,466.9026 ms, reported without a timing quality gate; and
- governed source digest `f6ce0c0166dc9af13a92c3ab362576e9f62b77959e606ab0f5f66d03d1a93e78`, matching the regenerated semantic benchmark.

The graph probes occur every 200 turns, including turn 1,000. That last turn passed all invariant checks and its conditional `memgraph_source_when_asked` check with source `graph`. This is Section 15 engineering conformance evidence, not one of the project-owned Section 16 release partitions.
