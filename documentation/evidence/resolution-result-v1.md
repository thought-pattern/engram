# Unified resolution result contract v1

**Status:** Current implemented transport-neutral contract
**Scope:** Core result semantics and completed adapter ownership

## Boundary

The contract is the Python `ResolutionResult` returned by `EngramCore.resolve_request`.
`ResolverResult` is an internal producer value. Full Claim records cross the
boundary inside `ResolutionResult.evidence_package`.

`engram.v2.EngramEvidenceService` is the typed external boundary. CLI, MCP, and
gRPC v1 retain their current interfaces. Resolution results are transient.

## Exact unified fields

The runtime `ResolutionResult` is a validated dictionary containing every
field below. `resolution_result_to_dict()` produces its JSON-compatible
dictionary shape. Runtime callers use keyed access.

| Field | Concrete type and absence | Meaning |
| --- | --- | --- |
| `schema_version` | integer `1` | Exact format marker for the one current core shape |
| `outcome` | `ResolutionOutcome` enum (`ANSWER`, `EVIDENCE`, or `MISS`) | Closed orchestration outcome; codec emits its string value |
| `selected_candidate` | candidate dictionary or `{}` | Present only for `ANSWER` |
| `selected_candidate_available` | boolean | Presence of `selected_candidate` |
| `response_candidates` | tuple; codec array | Bounded non-answer fallback candidates; exactly the selected candidate for `ANSWER` |
| `evidence` | tuple; codec array | Bounded minimal `EvidenceReference` values; empty when absent |
| `confidence` | finite number | Positive only for `ANSWER`; otherwise `0.0` |
| `confidence_available` | boolean | True only for `ANSWER` |
| `reason_codes` | ordered unique string tuple; codec array | Stable policy, exhaustion, truncation, and accounting reasons |
| `frame_diagnostics` | dictionary | Bounded content-free counts and policy/execution summaries; `{}` when omitted |
| `resolver_results` | tuple; codec array | Bounded internal execution summaries with empty `claim_evidence` at this handoff |
| `budget` | `BudgetConsumption` dictionary | Aggregate measured/capped use and sorted exhausted dimensions |
| `evidence_package_available` | boolean | Whether full-Claim package evaluation completed |
| `evidence_package` | `EvidencePackage` dictionary | Concrete empty or populated package |

Decoders accept the exact current fields and schema version and reject malformed
values. Full Claim records belong in `evidence_package`.

## Outcome and absence invariants

| Outcome | Candidate | Minimal evidence | Package | Confidence |
| --- | --- | --- | --- | --- |
| `ANSWER` | Exactly one selected and response candidate | Empty | Unavailable concrete empty package | Available and positive |
| `EVIDENCE` | Unselected; response candidates may be present | May be present | May be available; at least one useful surface across candidates, minimal evidence, or retained package records | Unavailable `0.0` |
| `MISS` | Unselected and empty response candidates | Empty | Unavailable empty or available empty | Unavailable `0.0` |

An unavailable package equals the canonical empty package. An available empty
package means trusted input reached package evaluation and policy retained zero
records. `EVIDENCE` requires a useful response candidate, minimal reference, or
package record. Fusion authorizes `ANSWER`.

## Package construction and truncation

Only the fixed `structured_graph` and `support_semantic` producers can contribute
full Claim records. The record contains canonical Claim identifiers, identity,
lifecycle and temporal fields, ownership, supplied trust, and retrieval
measurements. Every projection is reread by Claim ID and revalidated before
publication.

Records from both producers are normalized by Claim ID. Compatible contributions
merge deterministically and contradictory canonical or eligibility state rejects the
group. The usefulness policy requires complete canonical identity plus a qualifying
structured match or semantic similarity. Trust remains the supplied value and is
used only when an operator configures the versioned inclusion floor.

The package:

- uses canonical Claim-ID order and one record per Claim ID;
- retains at most 10 records and 65,536 complete serialized bytes;
- consumes the Claim-count allowance remaining after minimal response-support references;
- consumes complete package bytes within the aggregate evidence-byte allowance;
- records exact retained and package-input omitted counts; and
- uses sorted closed duplicate, record-limit, and serialized-size truncation reasons.

Complete result fitting applies deterministic priority:

1. remove nested resolver detail;
2. remove package records from the end of canonical order;
3. remove top-level minimal evidence from the end;
4. remove response candidates from the end; and
5. downgrade `ANSWER` or `EVIDENCE` when required useful output exceeds the budget.

`output_truncated`, `answer_exceeds_output_budget`, or `no_usable_output_after_truncation` records the corresponding result-level action. Output consumption is solved to the exact serialized-size fixed point.

## Budget and failure mapping

Resolver execution remains authoritative for resolver count, candidate count, graph rows, vector results, and producer working sets. Orchestration accounts for the final evidence package, frame diagnostics, normalization/policy/package working memory, elapsed time, and complete output.

`ResolutionBudget` version 2 contains resource ceilings and the trusted monotonic
measurement start. Resolver leases carry the applicable candidate,
graph-row, vector-result, evidence, byte, output, diagnostic, and working-memory
allowances. The executor measures each resolver and the complete turn. Cooperative
caller cancellation discards partial work and permits retry with the same request ID.

| Condition | Core behavior |
| --- | --- |
| Producer dependency unavailable or missing strict raw record | Typed resolver summary; another trusted producer may supply the package; retain response-candidate output or `MISS` |
| Unexpected Claim producer | Ignore its raw records, strip them from the result, and add `claim_evidence_untrusted_producer` |
| Source, frame-scope/time, or cross-producer conflict | Package unavailable, stable `claim_evidence_conflict`, retain other response output or `MISS` |
| Projection or resolver failure | Typed resolver state and empty Claim output |
| Usefulness exclusion | Available empty/partial package, stable reason counts and `claim_evidence_excluded`; retain response-candidate output or `MISS` |
| Evidence-byte allowance exhausted | Package fitted to allowance, `claim_evidence_bytes_exhausted`; producer truncation remains typed in aggregate exhaustion |
| Working memory exhausted | Empty package, `claim_evidence_memory_exhausted`, capped reported consumption |
| Diagnostic allowance exhausted | Content-free compact marker or `{}`, `diagnostics_truncated`, `diagnostic_bytes` exhausted |
| Output allowance exhausted | Deterministic fitting and exact final output accounting |

Claim-only records leave statement candidacy and accepted-success counters
unchanged. The resolution service owns request-level query accounting.

## Adapter mapping

### Python core

`EngramCore.resolve_request` returns the typed current `ResolutionResult`
dictionary directly. The stable version-1 mapping, authoritative identity and
budget inputs, concrete absence rules, feedback handoff, and compatibility
policy are documented in [Python API v1](../python-api.md).

### CLI

CLI `query` continues to call the legacy query operation.

### MCP

`engram_resolve` commits a Regulator verdict for an existing proposal. The
registered MCP tools are:

`engram_start`, `engram_send`, `engram_inspect`, `engram_add_fact`, `engram_finish`, `engram_stop`, `engram_propose`, `engram_resolve`, `engram_learn_response`, and `engram_retire_response`.

### Current gRPC v1

`engram.v1.EngramService.Resolve` handles proposal resolution. Its request fields
are `proposal_id`, `outcome`, `statement_id`, and `reason`, and it returns
`google.protobuf.Struct`.

### Current gRPC v2 evidence interface

`engram.v2.EngramEvidenceService.ResolveEvidence` exposes the unified result through
its own typed service while retaining `engram.v1.EngramService` unchanged. Generated
stubs are committed and reproducible from the v2 protocol. The adapter applies the
implemented authorization, redaction, cancellation, error, readiness, and shutdown
contracts documented in [gRPC integration](../grpc-integration.md).

## Persistence

Resolution requests and results are bounded transient Python state. Response
artifacts, mutation receipts, feedback, and other durable features retain their
schema versions.

## Verification

- `tests/test_resolution_contracts.py` freezes exact fields, concrete absence, outcome/package invariants, and raw-record containment.
- `tests/test_resolvers.py` freezes Claim-only and response-candidate outcomes,
  package filtering and truncation, aggregate budgets, fail-soft behavior, and
  Claim-only accounting.
- `tests/test_cli.py` freezes the existing command set.
- `tests/test_mcp_server.py` freezes the exact registered MCP tool list and proposal meaning of `engram_resolve`.
- `tests/test_grpc_server.py` freezes both service method lists, v1 proposal-verdict
  compatibility, v2 evidence messages, authorization/error translation, and
  generated-stub reproducibility.

The current adapter mappings are documented in the [Python API](../python-api.md)
and [gRPC integration guide](../grpc-integration.md).
