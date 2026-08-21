# MemGraph conversation comparison — 2026-08-20

## Outcome

MemGraph materially changes graph-dependent conversation behavior. With no seed data, all five fixed Sarah/Abraham prompts changed from an empty `none` result with graph disabled to a factual `graph` result with graph enabled. The configured graph was both enabled and ready.

The seeded 1,000-turn Sarah/sushi comparison did not change at all. Its seed contains a `*` pattern, so every turn is satisfied by pattern/cache retrieval before legacy graph fallback is considered. The enabled and disabled runs have identical ordered response and observation hashes. That run verifies MCP continuity and configuration loading, but it does not verify graph retrieval.

## Conversation A/B

The reproducible runner is [`scripts/compare_mcp_memgraph.py`](../../scripts/compare_mcp_memgraph.py), and the complete machine-readable result is [mcp-memgraph-comparison-2026-08-20.json](mcp-memgraph-comparison-2026-08-20.json). It uses the official MCP `Client` directly against the repository `MCPServer`, supplies no seed, and changes only whether `config.yml` is passed to `engram_start`.

| Prompt | MemGraph disabled | MemGraph enabled |
| --- | --- | --- |
| Tell me about Sarah. | empty, source `none` | `Sarah appears in Bible. Sarah is married to Abraham. Abraham is married to Sarah.`, source `graph` |
| Who is Sarah married to? | empty, source `none` | same bounded Sarah facts, source `graph` |
| What work is Sarah present in? | empty, source `none` | same bounded Sarah facts, source `graph` |
| Tell me about Abraham. | empty, source `none` | Sarah marriage plus four bounded child facts, source `graph` |
| Who is Abraham married to? | empty, source `none` | same bounded Abraham facts, source `graph` |

All five response hashes and all five source values changed. The disabled warm turns took 1.7008–6.4706 ms. The enabled warm turns took 1,393.5619–2,960.6501 ms, and the graph-enabled initialization turn took 3,940.5248 ms. These are reported observations, not pass/fail gates.

## Seeded 1,000-turn control

The graph-disabled and graph-enabled Sarah/sushi artifacts both passed 1,000/1,000 turns and all 17,000 checks. Their ordered response hash is `93b6f99d5105121d8ce368bb9b5d1efa164519cd525e3ace6387a7dc5a7f6ff4`, and their ordered observation hash is `a34169355a1198f95a81e3d8021f414880230321ac6ae04646e9140326cd86d6`.

The disabled run recorded 100 cache and 900 pattern turns at 3.9463 ms p50 and 10.3238 ms p95. The enabled run reported `graph_enabled: true` and `graph_ready: true`, but still recorded the same 100 cache and 900 pattern turns at 3.9297 ms p50 and 10.1364 ms p95. No graph turn occurred because the catch-all pattern won first.

Artifacts:

- [MemGraph disabled](section8-mcp-sarah-sushi-memgraph-disabled-1000-turns-2026-08-20.json)
- [MemGraph enabled](section8-mcp-sarah-sushi-memgraph-enabled-1000-turns-2026-08-20.json)

## Section 8 live probe

The comparison runner also exercises the fixed Section 8 capabilities directly. The live graph produced one canonical Sarah entity in 1,740.8917 ms, one `married_to` Predicate in 1,482.5165 ms, and one bounded one-hop Claim identifying Abraham in 53.1550 ms.

The same question through the complete transport-neutral resolver took 8,071.1110 ms and returned `EVIDENCE`. It retained one Claim, the response candidate `Sarah — married to: Abraham.`, a completed structured-graph result, and no exhausted dimensions. Resolution budget v2 reports these lengths but does not use them to suppress an otherwise valid result.

## Defects found and corrected

- The MCP conformance runner previously never passed `config_path`, so its graph component was disabled despite `config.yml` enabling MemGraph. It now records whether the config was supplied and whether graph is enabled and ready.
- Two legacy fact queries used invalid `MATCH`/`OPTIONAL MATCH` ordering for MemGraph. Their fixed read-only query ordering now executes against the configured server.
- Live Predicate nodes use the established `label` property rather than the new optional `primary_label`. Canonical Predicate lookup now accepts either property and still returns the strict `primary_label` result contract.
- Graph-backed legacy replies were incorrectly reported as source `pattern`. They now report source `graph`, with a focused behavior test.
- Uncalibrated total, per-resolver, and one-hop latency budgets suppressed valid graph evidence. Those time fields and deadline branches were removed from current budget and plan contracts; elapsed time remains reported.

No graph writes were performed.
