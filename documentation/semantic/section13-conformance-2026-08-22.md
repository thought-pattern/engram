# Section 13 semantic retrieval and reranking conformance — 2026-08-22

## Outcome

Section 13 is component-complete. Engram now has a default-off standalone
semantic resolver over authoritative request representations and an independently
controlled transparent shortlist reranker. The pinned native CPU semantic model
passed every engineering value and resource gate and is promoted for opt-in
component use. The reranker passed its safety and resource checks but produced no
release-set recall gain, so it remains disabled by default and is not promoted.

This is component evidence, not Section 16 release approval. The corpus and
results are repository-visible engineering material; protected evaluation,
rollout, and release authority remain outside Section 13.

## Task evidence

| Task | Evidence |
| --- | --- |
| EGR-1301 | The [version-1 contract](contracts-v1.md) defines immutable embedding records over canonical requests and aliases, with representation origin, artifact generation, exact scope/lifecycle, and model/normalization identity. Accepted response prose is never encoded. |
| EGR-1302 | The [tracked model manifest](model-manifest-all-MiniLM-L6-v2-826711e5.json), [ADR 0007](../decisions/0007-section13-local-semantic-and-transparent-reranking.md), exact runtime identity/checksum gate, checksum-bound local `LICENSE`, CPU-only local loading, and explicit setup script establish the artifact policy. A caller-supplied checksum cannot self-approve another artifact, and runtime downloads are prohibited. |
| EGR-1303 | `StandaloneSemanticIndexOwner` supplies immutable build, incremental synchronization, removal of terminal/deleted projections without redundant model execution, consistency checking, clean rebuild, dimension checking, bounded streaming exact cosine search, cancellation, and fail-soft health. |
| EGR-1304 | `StandaloneSemanticResolver` runs after cheaper configured resolvers, revalidates the current authoritative artifact, emits alias and model provenance plus semantic score/accounting, obeys vector and memory budgets, and enters shared eligibility and fusion. |
| EGR-1305 | The [30-query benchmark corpus](../../eval/section13-semantic-v1.json) and [source-bound result](benchmark-2026-08-22.json) record native recall, drift, cold start, p50/p95, throughput, memory, record count, and artifact size. ONNX and quantized variants are explicitly unavailable because no reviewed local artifacts are provisioned. |
| EGR-1306 | `transparent_logistic_v1` accepts only an already eligible fused shortlist and bounds shortlist size, serialized input, measured model time, output, and cancellation. Any scorer/input/time failure preserves baseline order. |
| EGR-1307 | The benchmark evaluates the visible fixed logistic coefficients on disjoint train, calibration, and release partitions. A pairwise encoder is explicitly unavailable because there is no approved local artifact; it is neither downloaded nor simulated. |
| EGR-1308 | Core health exposes semantic artifact identity, readiness, generations, record count, and bounded error type plus reranker implementation/version and low-cardinality counters. Either feature rolls back through its independent configuration flag without authoritative-data migration. |
| EGR-1309 | Independent gate decisions were completed. Semantic passed all six checks and is promoted for opt-in component use. Reranking passed four non-regression/resource checks but did not pass a positive-value gate because recall gain was zero, so it remains unpromoted. The final official-MCP gate passed all 1,000 Sarah turns with the pinned semantic component, reranker, and live MemGraph ready. |

## Model artifact and one-time provisioning

The reviewed artifact is `sentence-transformers/all-MiniLM-L6-v2` at immutable
revision `826711e54e001c83835913827a843d8dd0a1def9`, Apache-2.0, dimension 384.
Its Engram payload checksum is
`ff12d37a18ee862cd4a5b8476466bc84f06d0801da9b749023eed74251cefcb8`
and its measured payload is 91,627,950 bytes.

Local model files and the machine-local manifest live beneath the ignored,
non-hidden `data/artifacts/models/` directory. The provisioning script downloads
only during explicit setup. A subsequent execution verified the manifest,
license, identity, and complete payload checksum, rewrote only the relocated
manifest path, reported `reused: true`, and did not call the download path.
Incomplete or conflicting destinations fail instead of being overwritten.

## Independent benchmark results

The corpus contains 12 documents and 30 queries split into ten train, ten
calibration, and ten release rows. Each partition contains nine positive queries
and one negative query.

| Measure | Semantic result | Gate |
| --- | ---: | ---: |
| Release recall@1 | 0.8889 | at least 0.75 |
| Gain over sparse release recall@1 | +0.1111 | at least +0.10 |
| Release false-answer rate | 0.0000 | at most 0.10 |
| Native query p50 / p95 | 21.7869 / 26.1084 ms | p95 at most 500 ms |
| Cold start | 1,216.2818 ms | at most 15,000 ms |
| Peak observed process RSS | 456.5938 MiB | at most 768 MiB |
| Throughput | 45.7585 queries/second | descriptive |
| Index build | 384.3974 ms for 36 records | descriptive |
| Maximum repeated-score drift | 0.0000 | descriptive |

Native execution is the only available reviewed backend. ONNX is unavailable
because no separately reviewed artifact is provisioned; runtime package presence
is reported separately and cannot make that backend available. Quantized is also
unavailable because no reviewed artifact is provisioned. No score is imputed for
either unavailable path.

| Measure | Reranker result | Gate |
| --- | ---: | ---: |
| Release recall@1 gain | 0.0000 | at least 0.00 safety floor |
| Release false-answer-rate delta | 0.0000 | at most 0.00 |
| Overhead p50 / p95 | 0.0985 / 0.1794 ms | p95 at most 5 ms |
| Peak incremental process RSS | 0.4375 MiB | at most 16 MiB |
| Positive held-out value | false | required for promotion |

The reranker therefore remains a tested, reversible component but is not an
enabled or promoted retrieval improvement.

## Verification

- Focused semantic and reranker modules contribute **29 passing tests** to the
  final run, including remediation regressions from the comprehensive audit.
- Complete repository suite: **1,566 passed** in 180.47 seconds.
- Pyright: **0 errors, 0 warnings, 0 information messages**.
- Ruff lint and changed-surface Python formatting, plus isort: passed after final formatting.
- The host's installed Black 26.5.1 package did not complete even an import or
  `--version` probe and is not reported as a passing gate. Ruff formatting is the
  verified formatter result for this run.
- Bandit: no high-severity findings at medium-or-higher confidence.
- Vulture: the project-owned pass is clean when compiler-generated protobuf and
  gRPC files are excluded; no project-owned Section 13 dead code was found.

## Official MCP and live MemGraph

The [final MCP artifact](section13-mcp-sarah-preferences-memgraph-enabled-1000-turns-2026-08-22.json)
uses the official MCP client against the repository MCP server. Its ephemeral
configuration retains the configured live MemGraph endpoint and enables the
pinned semantic artifact and transparent reranker. Health inspection reported
graph, semantic, and reranker components ready; the semantic identity reports the
expected immutable model revision.

Sarah declared that she likes sushi and cats and dislikes dogs. All **1,000
requested turns completed**, all **1,000 were evaluated**, and all **1,000
passed**, with zero failed checks. The run made 1,003 MCP calls and produced 995
pattern responses plus five live-MemGraph responses at turns 200, 400, 600, 800,
and 1,000. Observed turn p95 was 3.9347 ms and total run duration was
25.734753 seconds; elapsed values are descriptive and do not control knowledge
eligibility.

The benchmark and MCP artifacts both bind to governed source SHA-256
`b7a1c3ec957dfd79a45de2b462c1e7c34a9ffbb8e44bc9f5436018933a278454`.

The MCP seed supplies patterns rather than accepted-response artifacts, so the
standalone semantic index correctly reported zero projected records in this
transport run. Real model loading, non-empty projection, retrieval, and
independent value were exercised by the benchmark and focused integration tests;
the MCP run proves transport continuity, component readiness, strict turn-level
evaluation, preference continuity, and live graph use.
