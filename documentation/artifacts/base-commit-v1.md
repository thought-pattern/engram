# Accepted-response base commit contract v1

Status: Implemented by EGR-302  
Authority: Section 3 of `ENGRAM-DEVELOPMENT.md`

## Command boundary

`AcceptedResponseService.commit_response(artifact, request_id)` is the transport-neutral create operation. The caller supplies a fully validated `CachedResponseArtifact`; constructing that value enforces the identity, canonical and alias representation, scope, tier, lifecycle, support, temporal, epoch, provenance, statistics, metadata, exact response, and codec bounds. Base commit additionally requires generation 1, `ACTIVE`, and no caller-provided reserved `lifecycle_audit` metadata.

The exact response Unicode scalar sequence is never normalized or rewritten. Only a separate normalized comparison is used to reject an empty response or a complete `IDK` response such as whitespace/case/punctuation variants.

## Identity and collision policy

Every canonical and alias binding is compared against the repository's complete scoped retrieval ownership before admission. Any owner—including an inactive historical owner—blocks base commit, and the stable conflict names all existing statement IDs. Identical text in another exact scope is independent and can be admitted. Existing statement IDs also conflict.

Base commit never replaces or supersedes. Historical-key reuse is reserved for explicit supersession with an expected owner and generation.

## Idempotency and results

The payload signature covers the complete deterministic artifact dictionary. Receipt lookup, collision checking, capacity planning, receipt sequence allocation, candidate construction, checkpoint, and publication execute beneath the coordinator mutation lock.

- An exact completed retry returns the original receipt, marks the invocation replayed, and performs no checkpoint.
- Reusing a request ID with changed artifact content conflicts without mutation.
- Prepared or expired identities require recovery or report expiry; neither is applied as new.
- A created result is `CREATED` or `CREATED_WITH_EVICTION` and records every zero-to-generation creation and generation-to-zero eviction.
- `REJECTED_CAPACITY` records a replayable completed receipt while leaving repository generation and namespace epochs unchanged.

Successful residency changes increment every affected namespace exactly once in the same candidate. STATIC retention and DYNAMIC eviction are inherited from the versioned tier-admission policy.

## Durability

Configured persistence serializes the complete off-live response candidate alongside compatible top-level legacy statements and keyword ownership, then atomically replaces the file before publication. Restart rebuilds derived repository state and preserves artifact, epoch, and receipt authority. Durable comparison excludes transient in-memory repository/index generations, which are rebuilt rather than persisted. An indeterminate acknowledgement can therefore reload the file, identify the exact authoritative candidate, and publish it without a second write.

## Evidence

`tests/test_responses.py` covers exact Unicode preservation, field restrictions, normalized `IDK`, canonical and alias owner conflicts, cross-scope independence, exact and changed retries, concurrent same-request serialization, DYNAMIC eviction, protected-capacity rejection, namespace effects, real atomic-file restart, and indeterminate durable recovery.
