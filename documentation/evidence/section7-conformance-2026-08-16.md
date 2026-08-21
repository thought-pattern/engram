# Section 7 conformance report — 2026-08-16

Status: **PASS**

Section 7 now provides bounded, currently disclosure-eligible, response-less Claim evidence through the transport-neutral core without granting answer authority. This is an in-place completion of the one pre-exposure core result mechanism. CLI and MCP remain unchanged, the existing `engram.v1` gRPC proposal service remains unchanged, and Section 15 must introduce an explicitly versioned external gRPC boundary if it later exposes evidence.

## Requirement evidence

| Gate | Evidence | Result |
|---|---|---|
| Evolution and exact core shape | [ADR 0005](../decisions/0005-section7-evidence-compatibility.md), [core handoff](core-handoff.md), `tests/test_resolution_contracts.py` | One current exact `ResolverResult` and `ResolutionResult`; stale development shapes are rejected; no internal upgrade, downgrade, or compatibility projection. |
| Strict record and package codecs | [contracts](contracts-v1.md), `tests/test_evidence.py` | Deterministic codecs, exact fields, unsupported versions, wrong wire types, concrete absence, excluded content, nonempty bounded reasons, current-only validity, canonical order, deduplication, and count/size/reason bounds pass. |
| Fixed projection boundary | `engram/graph.py`, `engram/core.py`, `tests/test_claim_projection.py` | Only allow-listed read-only structured, vector, and by-ID projections are accepted; malformed, over-returned, content-bearing, and conflicting rows are rejected or fail soft at the transport-neutral vector boundary. Structured zero-row attempts are capped at three, and the by-ID query exposes a second row to the strict one-row decoder so duplicate canonical matches cannot hide behind `LIMIT 1`. |
| Current disclosure eligibility | `engram/evidence.py`, `tests/test_claim_eligibility.py` | Active, system-current, half-open valid-time, proof-canonical identity, retrieval-only exclusion, public disclosure, exact trusted-scope disclosure, falsey/malformed authority rejection, missing trust, identity change, fixed by-ID provenance, and publication revalidation matrices pass. |
| Structured and semantic production | `engram/resolvers.py`, `tests/test_resolvers.py` | Both producers emit revalidated full records without phrasing Claim text. The semantic path preserves support-to-artifact response candidates, raw similarity, shared vector accounting, and zero-candidate response-less discovery. |
| Canonicalization and usefulness | `engram/evidence.py`, `tests/test_evidence.py`, `tests/test_resolvers.py` | Cross-producer Claim-ID merge is order independent, retains compatible features and bounded sources, derives source agreement, rejects conflicts, distinguishes unavailable from zero, and returns closed unfitted-policy reasons. |
| Orchestration and accounting | `engram/resolvers.py`, `tests/test_resolution_contracts.py`, `tests/test_resolvers.py` | ANSWER retains priority; useful Claims can produce only EVIDENCE; excluded noise remains MISS; response candidates survive Claim exclusion; Claim-only evidence creates no candidacy or success accounting; aggregate consumption and final output bytes are exact. Raw sources must match their producer, only the two trusted producers participate, frame scope/time must match, and no-record producer completion leaves the package unavailable. |
| Resource and failure behavior | `tests/test_resolvers.py`, `tests/test_claim_projection.py` | Claim count, evidence bytes, complete output, diagnostics, working memory, graph rows, and vector results are bounded. Elapsed time is reported without changing completed evidence. Graph/model/index unavailability, resolver exceptions, projection conflicts, and partial dependency failure fail soft without leaking raw records. |
| Interface boundary | `tests/test_cli.py`, `tests/test_mcp_server.py`, `tests/test_grpc_server.py`, [1,000-turn artifact](section7-mcp-conversation-1000-turns-2026-08-16.json) | CLI commands and MCP tools are unchanged. The current gRPC v1 descriptor and proposal `Resolve` fields are unchanged. Future evidence transport is owned by a new versioned gRPC interface in Section 15. The official MCP client completed and individually evaluated all 1,000 turns. |
| Inspection and cardinality | `tests/test_resolvers.py`, `tests/test_claim_projection.py` | Claim inspection is a fixed ten-key aggregate with closed reason keys and byte truncation; it contains no Claim or canonical entity IDs. No new metric labels were added. Vector projection warnings report only the bounded exception class, not exception text or Claim content. |

## Verification

The final focused command covered the evidence, projection, eligibility, result, resolver, CLI, MCP, and gRPC suites: **232 passed in 64.45 s**. The final repository run passed **1,422 tests in 105.15 s**.

Static and security gates on the same final code state:

- Pyright 1.1.404: **0 errors, 0 warnings**.
- Ruff: **all checks passed**.
- Black 24.3.0: **106 files unchanged** using the repository's 132-column Python 3.11 formatting policy.
- isort: **all Section 7 changed Python files passed**; the repository-wide command still identifies eight untouched legacy files outside the Section 7 change surface.
- Vulture at the configured 65 percent confidence: **no findings**.
- Bandit high-severity scan of the Section 7 production surface: **no findings**.
- `compileall`: **passed** for `engram`, `scripts`, `eval`, and `tests`.
- Evidence JSON validation and `git diff --check`: **passed**; line-ending messages were informational only.

During the initial gate, Pyright exposed 33 real contract/fixture typing defects. Fixing them uncovered a falsey-invalid eligibility-evaluator acceptance bug and made the merge nonempty invariant explicit. A new logging regression then revealed that the prior semantic fail-soft fixture was malformed: it passed because missing vector configuration raised `KeyError` before the fake client ran. The fixture now supplies the complete boundary, asserts the client is called exactly once, and proves that sensitive Claim content is absent from the warning.

The subsequent adversarial review found and remediated additional contract defects rather than weakening tests around them: inactive/system-noncurrent evidence records could be directly constructed; empty selection reasons were accepted; a falsey or equality-overloaded visibility authority could be mistaken for concrete absence; by-ID `LIMIT 1` hid duplicate rows; revalidation did not require the fixed by-ID projection identifier; raw records did not have to match their producing resolver; the execution-report normalizer and orchestrator could accept unexpected producer names; normalized disclosure scope/evaluation time were not rebound to the current frame; structured zero-row requests were not attempt-bounded; and a producer completing with no strict records incorrectly implied package evaluation availability. Regression fixtures for each behavior are included in the final totals above. Pyright also caught one malformed revalidator test-double signature introduced during that review.

## Engineering benchmark

The reproducible runner is `python scripts/benchmark_evidence.py --samples 100`; the checked [machine-readable artifact](benchmark-2026-08-16.json) passes all 16 engineering gates on Python 3.12.2 / Windows:

| Measurement | Recorded result |
|---|---:|
| Record codec p95 | 0.9865 ms |
| Ten-record package p95 | 13.3157 ms |
| 1,000-record normalization p95 | 166.0313 ms |
| 1,000-record usefulness policy p95 | 49.1077 ms |
| Partial-failure orchestration p95 | 25.7659 ms |
| Ten-record serialized package | 12,530 bytes |
| 1,000-record peak traced memory | 701,355 bytes |

The artifact also proves order-independent canonicalization, the ten-record/64-KiB package bounds, visible partial failure with retained EVIDENCE, containment of full records to the package, no response accounting for Claim-only evidence, exact output accounting, and working-memory compliance. Its synthetic data did not select or fit policy thresholds. The 109.0144 ms and 25.7659 ms orchestration p95 runs are retained as local observations; the current benchmark runner applies no latency pass/fail threshold.

## MCP long-conversation evaluation

The fresh [Section 7 MCP artifact](section7-mcp-conversation-1000-turns-2026-08-16.json) records **1,000 completed and 1,000 evaluated turns** through the official MCP `Client` against the repository `MCPServer`. Every turn received 16 checks: exact event fields, sequence, input/user continuity, nonempty response/source, finite score, string dialogue fields, concrete list/object fields, and nonnegative elapsed time. All **16,000 checks passed**; the ordered pass run is `1 × 1000`, `failed_turns` and failure counts are empty, inspection reported turn 1,000, and stop reported 1,000 exchanges. The artifact retains response byte counts and SHA-256 digests for the first and last evaluation rather than raw response content. Turn latency was 5.5665 ms p50 and 13.9455 ms p95; the first-turn initialization maximum was 588.6984 ms.

## Deferred ownership

Section 8 owns relation-aware contextual lookup; Section 9 owns historical time, relative trust, multi-value, and contradiction policy; Section 10 owns multi-hop paths; Section 15 owns stable Python exposure and a future explicitly versioned gRPC evidence interface; Section 16 owns held-out usefulness, avoided Tapestry calls/tokens, chaos, calibration, adapter-outage, and release approval. None of those deferred gates are claimed here.
