# Section 7 core evidence handoff

**Status:** Frozen for EGR-711  
**Scope:** Transport-neutral core semantics and actual adapter ownership; no adapter implementation or release claim

## Boundary

The Section 7 handoff is the Python `ResolutionResult` returned by `EngramCore.resolve_request`. It is one current pre-exposure core shape with no internal negotiation layer. `ResolverResult` is an internal producer/executor value. A complete unified result must not expose raw `ResolverResult.claim_evidence`; full Claim records cross the handoff only inside `ResolutionResult.evidence_package`.

Section 7 does not change CLI, MCP, the committed gRPC v1 protocol, or persistence. The future gRPC evidence API is the first external protocol boundary that requires a new version.

## Exact unified fields

The runtime `ResolutionResult` is a validated dictionary containing every
field below. `resolution_result_to_dict()` produces its JSON-compatible
dictionary shape; the runtime result intentionally has no attribute or
`.to_dict()` compatibility facade.

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
| `evidence_package` | `EvidencePackage` dictionary | Concrete empty or populated package; never omitted or null |

Decoders require exactly these fields and reject missing fields, extra fields, non-integer or unsupported schema values, malformed nested values, and any nested raw full-Claim records. Development-only Section 4 JSON lacking the new required fields is stale internal data, not a supported external payload.

## Outcome and absence invariants

| Outcome | Candidate | Minimal evidence | Package | Confidence |
| --- | --- | --- | --- | --- |
| `ANSWER` | Exactly one selected and response candidate | Empty | Unavailable concrete empty package | Available and positive |
| `EVIDENCE` | Unselected; response candidates may be present | May be present | May be available; at least one useful surface across candidates, minimal evidence, or retained package records | Unavailable `0.0` |
| `MISS` | Unselected and no response candidates | Empty | Unavailable empty or available empty; never retained records | Unavailable `0.0` |

An unavailable package must equal the canonical empty package. A producer that completes without a strict raw record leaves the package unavailable, because the fail-soft projection boundary cannot distinguish a dependency failure from a genuine no-hit. An available empty package means at least one trusted raw record reached policy/package evaluation but no record was retained. It cannot by itself authorize `EVIDENCE`. Package records never authorize `ANSWER`.

## Package construction and truncation

Only records already produced through fixed projection, current eligibility, exact disclosure, and publication revalidation by the allow-listed `structured_graph` and `support_semantic` producers are normalized. Raw record source must match its producer, and every normalized record must match the frame's exact disclosure scope and evaluation time. Unexpected producers are ignored; source, frame-binding, and cross-producer conflicts fail closed. The frozen usefulness policy then includes or excludes each normalized Claim.

The package:

- uses canonical Claim-ID order and one record per Claim ID;
- retains at most 10 records and never exceeds 65,536 complete serialized bytes;
- consumes the Claim-count allowance remaining after minimal response-support references;
- consumes complete package bytes within the aggregate evidence-byte allowance;
- records exact retained and package-input omitted counts; and
- uses sorted closed duplicate, record-limit, and serialized-size truncation reasons.

Complete result fitting applies deterministic priority:

1. remove nested resolver detail;
2. remove package records from the end of canonical order;
3. remove top-level minimal evidence from the end;
4. remove response candidates from the end; and
5. downgrade `ANSWER` or `EVIDENCE` only when its required useful output cannot fit.

`output_truncated`, `answer_exceeds_output_budget`, or `no_usable_output_after_truncation` records the corresponding result-level action. Output consumption is solved to the exact serialized-size fixed point.

## Budget and failure mapping

Resolver execution remains authoritative for resolver count, candidate count, graph rows, vector results, and producer working sets. Orchestration accounts for the final evidence package, frame diagnostics, normalization/policy/package working memory, elapsed time, and complete output.

| Condition | Core behavior |
| --- | --- |
| Producer dependency unavailable or no strict raw record | Typed unavailable/failed/empty resolver summary; package unavailable unless another trusted producer supplies a strict raw record; retain response-candidate output or `MISS` |
| Unexpected Claim producer | Ignore its raw records, strip them from the result, and add `claim_evidence_untrusted_producer` |
| Source, frame-scope/time, or cross-producer conflict | Package unavailable, stable `claim_evidence_conflict`, retain other response output or `MISS` |
| Projection or resolver failure | Fail soft through typed resolver state; never synthesize a Claim or answer |
| Usefulness exclusion | Available empty/partial package, stable reason counts and `claim_evidence_excluded`; retain response-candidate output or `MISS` |
| Evidence-byte allowance exhausted | No over-budget package, `claim_evidence_bytes_exhausted` when orchestration cannot fit the envelope; producer truncation remains typed in aggregate exhaustion |
| Working memory exhausted | No package publication, `claim_evidence_memory_exhausted`, capped reported consumption |
| Diagnostic allowance exhausted | Content-free compact marker or `{}`, `diagnostics_truncated`, `diagnostic_bytes` exhausted |
| Output allowance exhausted | Deterministic fitting and exact final output accounting |

Claim-only records create no response candidate or accounting observation. The finalizer therefore applies no statement candidacy or accepted-success credit for them. Ordinary request-level query accounting remains owned by the existing resolution service.

## Actual adapter mapping

### Python core

`EngramCore.resolve_request` returns the typed current `ResolutionResult`
dictionary directly. The stable version-1 mapping, authoritative identity and
budget inputs, concrete absence rules, feedback handoff, and compatibility
policy are documented in [Python API v1](../python-api.md). It does not add
an internal version-negotiation layer.

### CLI

CLI commands and output are unchanged. `query` continues to call the legacy query operation. No `resolve`, `evidence`, result-version, or package option is added by Section 7.

### MCP

MCP tools and schemas are unchanged. `engram_resolve` continues to commit a Regulator verdict for an existing proposal; it is not unified resolution. Section 7 adds no MCP evidence field or tool. The exact registered tool list remains:

`engram_start`, `engram_send`, `engram_inspect`, `engram_add_fact`, `engram_finish`, `engram_stop`, `engram_propose`, `engram_resolve`, `engram_learn_response`, and `engram_retire_response`.

### Current gRPC v1

`engram.v1.EngramService.Resolve` remains proposal resolution. Its request fields remain `proposal_id`, `outcome`, `statement_id`, and `reason`, and it returns `google.protobuf.Struct`. No Claim record, package, or unified-result message is added to `engram.v1`.

### Future gRPC evidence interface

Additive changes to the current `Resolve` RPC are unsafe: the RPC has different proposal-verdict semantics, and Claim-only evidence cannot be omitted without changing `EVIDENCE` into an apparent `MISS`. Section 15 must therefore introduce an explicit new protocol boundary, preferably an `engram.v2` typed resolution service/RPC and typed result message, while retaining `engram.v1.EngramService` unchanged. Committed stubs must be generated with pinned tools and checked byte-for-byte. Authorization, redaction, cancellation, deployment, and client migration belong to Section 15.

## Persistence

Resolution requests/results remain bounded transient Python state. Section 7 adds no durable result payload and no migration. Response artifacts, mutation receipts, feedback, and other persisted features retain their independently owned schema versions.

## Verification

- `tests/test_resolution_contracts.py` freezes exact fields, concrete absence, outcome/package invariants, and raw-record containment.
- `tests/test_resolvers.py` freezes Claim-only and response-candidate outcomes, package filtering and truncation, aggregate budgets, fail-soft behavior, and no response accounting.
- `tests/test_cli.py` freezes the existing command set without resolution/evidence additions.
- `tests/test_mcp_server.py` freezes the exact registered MCP tool list and proposal meaning of `engram_resolve`.
- `tests/test_grpc_server.py` freezes the current service method list, `ResolveRequest` fields, `Struct` response, absence of evidence messages, and generated-stub reproducibility.

Adapter authorization and the future gRPC generated contract are deliberately not Section 7 completion claims.
