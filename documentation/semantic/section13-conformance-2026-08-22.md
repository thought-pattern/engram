# Section 13 semantic retrieval and reranking conformance — 2026-08-22

## Outcome

Section 13 is component-complete. Engram now has a default-off standalone
semantic resolver over authoritative request representations and an independently
controlled transparent shortlist reranker. The pinned native CPU semantic model
passed every engineering value and resource gate and is promoted for opt-in
component use. The reranker passed its safety and resource checks but produced no
engineering-holdout recall gain, so it remains disabled by default and is not promoted.

This is component evidence, not Section 16 release approval. The corpus and
results are repository-visible engineering material; the disjoint project release
partitions, rollout, and release authority remain outside Section 13.

## Task evidence

| Task | Evidence |
| --- | --- |
| EGR-1301 | The [version-1 contract](contracts-v1.md) defines immutable embedding records over canonical requests and aliases, with representation origin, artifact generation, exact scope/lifecycle, and model/normalization identity. Accepted response prose is never encoded. |
| EGR-1302 | The [tracked model manifest](model-manifest-all-MiniLM-L6-v2-826711e5.json), [ADR 0007](../decisions/0007-section13-local-semantic-and-transparent-reranking.md), exact runtime identity/checksum gate, checksum-bound local `LICENSE`, CPU-only local loading, and explicit setup script establish the artifact policy. A caller-supplied checksum cannot self-approve another artifact, and runtime downloads are prohibited. |
| EGR-1303 | `StandaloneSemanticIndexOwner` supplies immutable build, incremental synchronization, removal of terminal/deleted projections without redundant model execution, consistency checking, clean rebuild outside the owner lock after a generation conflict, dimension checking, bounded streaming exact cosine search, cancellation, and fail-soft health. |
| EGR-1304 | `StandaloneSemanticResolver` runs after cheaper configured resolvers, revalidates the current authoritative artifact, emits alias and model provenance plus a positive unit-interval semantic score/accounting, obeys vector and memory budgets, and enters shared eligibility and fusion. |
| EGR-1305 | The [30-query benchmark corpus](../../eval/section13-semantic-v1.json) and [source-bound result](benchmark-2026-08-22.json) record native recall, drift, cold start, p50/p95/p99/maximum, throughput, memory, record count, and artifact size. Timing is descriptive. ONNX and quantized variants are explicitly unavailable because no reviewed local artifacts are provisioned. |
| EGR-1306 | `transparent_logistic_v1` accepts only the deterministic top-N already eligible fused shortlist and bounds shortlist size, serialized input, output, and cancellation. Measured model time and target exceedance are observational; scorer or deterministic input-bound failure preserves baseline order. |
| EGR-1307 | The benchmark evaluates the visible fixed logistic coefficients on disjoint train, calibration, and repository-visible engineering-holdout partitions. A pairwise encoder is explicitly unavailable because there is no approved local artifact; it is neither downloaded nor simulated. |
| EGR-1308 | Core health exposes semantic artifact identity, readiness, generations, record count, and bounded error type plus reranker implementation/version and low-cardinality counters. Either feature rolls back through its independent configuration flag without authoritative-data migration. |
| EGR-1309 | Separate gate decisions were completed. Semantic passed four non-time checks and is promoted for opt-in component use. Reranking passed three non-time non-regression/resource checks but did not pass a positive-value gate because recall gain was zero, so it remains unpromoted. The final official-MCP gate passed all 1,000 Sarah turns with the pinned semantic component, reranker, and live MemGraph ready. |

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
New downloads are staged beside the final directory and checksum-verified before
an atomic publish. Interrupted downloads leave neither a partial final directory
nor a manifest; incomplete or conflicting existing destinations fail instead of
being overwritten.

## Engineering benchmark results

The corpus contains 12 documents and 30 queries split into ten train, ten
calibration, and ten engineering-holdout rows. Each partition contains nine positive queries
and one negative query.

| Measure | Semantic result | Gate |
| --- | ---: | ---: |
| Engineering-holdout recall@1 | 0.8889 | at least 0.75 |
| Gain over sparse engineering-holdout recall@1 | +0.1111 | at least +0.10 |
| Engineering-holdout false-answer rate | 0.0000 | at most 0.10 |
| Native query p50 / p95 / p99 / maximum | 19.3821 / 21.2535 / 21.5287 / 21.5287 ms | Descriptive |
| Cold start | 834.6215 ms | Descriptive |
| Peak observed process RSS | 456.4688 MiB | at most 768 MiB |
| Throughput | 52.5416 queries/second | Descriptive |
| Index build | 335.5429 ms for 36 records | Descriptive |
| Maximum repeated-score drift | 0.0000 | descriptive |

Native execution is the only available reviewed backend. ONNX is unavailable
because no separately reviewed artifact is provisioned; runtime package presence
is reported separately and cannot make that backend available. Quantized is also
unavailable because no reviewed artifact is provisioned. No score is imputed for
either unavailable path.

| Measure | Reranker result | Gate |
| --- | ---: | ---: |
| Engineering-holdout recall@1 gain | 0.0000 | at least 0.00 safety floor |
| Engineering-holdout false-answer-rate delta | 0.0000 | at most 0.00 |
| Overhead p50 / p95 / p99 / maximum | 0.0954 / 0.1382 / 0.1407 / 0.1407 ms | Descriptive |
| Peak incremental process RSS | 0.3438 MiB | at most 16 MiB |
| Positive held-out value | false | required for promotion |

The reranker therefore remains a tested, reversible component but is not an
enabled or promoted retrieval improvement.

## Verification

- Focused semantic and reranker modules contribute **32 passing tests** to the
  final run, including remediation regressions from the comprehensive audit.
- Complete repository suite: **1,650 passed** in 124.80 seconds.
- Pyright: **0 errors, 0 warnings, 0 information messages**.
- Ruff lint and isort: passed after final formatting.
- A direct full-tree write-mode Black run completed successfully; the final pass
  left all 147 files unchanged.
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

The regenerated benchmark binds to governed source SHA-256
`f6ce0c0166dc9af13a92c3ab362576e9f62b77959e606ab0f5f66d03d1a93e78`.
The earlier transport artifact remains historical evidence bound to its recorded
pre-remediation digest
`b7a1c3ec957dfd79a45de2b462c1e7c34a9ffbb8e44bc9f5436018933a278454`;
it is not represented as source-identical to the regenerated benchmark.

The MCP seed supplies patterns rather than accepted-response artifacts, so the
standalone semantic index correctly reported zero projected records in this
transport run. Real model loading, non-empty projection, retrieval, and
independent value were exercised by the benchmark and focused integration tests;
the MCP run proves transport continuity, component readiness, strict turn-level
evaluation, preference continuity, and live graph use.
