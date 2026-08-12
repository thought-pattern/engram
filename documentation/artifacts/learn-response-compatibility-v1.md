# LearnResponse compatibility contract v1

Status: Implemented by EGR-308  
Authority: Section 3 of `ENGRAM-DEVELOPMENT.md`

## Wrapper behavior

`EngramCore.learn_response` retains the existing Python, MCP, and gRPC input and result shape while delegating creation to `AcceptedResponseService.learn_response` and the base-commit coordinator. For a new request identity, the wrapper constructs exactly one DYNAMIC, generation-1, ACTIVE artifact:

- request and exact response become authoritative identity/retrieval and response fields;
- namespace and context fingerprint form the exact scope;
- normalized user ID and source label become provenance;
- caller metadata remains bounded artifact metadata;
- metadata support records also populate typed support Claim IDs; and
- a UUIDv5 statement ID is deterministically namespaced by the request ID, while acceptance time is injected only for a new mutation.

The semantic receipt signature covers request, exact response, user, scope, source, and metadata but excludes generated statement ID/time. An exact retry can therefore replay across restart before constructing another artifact and performs no second checkpoint. Changed reuse conflicts.

## Compatibility guarantees

Complete normalized `IDK` remains rejected. User session context is updated after successful creation and exact replay. Regulated metrics distinguish creation and idempotent replay. A second request ID for an already-owned scoped retrieval key conflicts with the named owner; `LearnResponse` no longer performs implicit replacement or supersession.

Legacy response statements are mirrors rebuilt from the artifact repository. Exact scoped proposal lookup is contextually revalidated before keyword/vector fallback, so identities such as `when` and `where` remain distinct even when lexical terms overlap. Required metadata still filters exact results. Terminal artifacts remain present for history but are excluded from proposals.

## Proposal accounting

Candidate-query and accepted-hit accounting are authoritative artifact mutations. They increment artifact generation and statistics, update the compatibility view, and record internal durable receipts, but do not increment namespace epoch because statistics alone do not change direct-answer eligibility. Exact proposal/resolution retries do not double-count or checkpoint again. Candidate persistence captures the same query/hit state in artifacts and legacy views.

## Failure behavior

With a configured store, LearnResponse checkpoints the complete off-live candidate before publication. A failed or unresolved checkpoint leaves the artifact absent from live state and reports `state_changed=false`; retry after storage recovery is a new successful execution, not an idempotent replay of a mutation that never published. In-memory and deferred-checkpoint modes retain the same coordinator state machine without claiming durability.

## Evidence

`tests/test_service.py`, `tests/test_mcp_server.py`, `tests/test_grpc_server.py`, and the now-green when/where baseline regression cover artifact construction, support, exact text, scope, user context, metadata, durable restart retry, no implicit replacement, proposal query/hit equivalence, terminal exclusion, IDK, checkpoint failure, and adapter compatibility.
