# Engram Python API v1

**Status:** Stable transport-neutral mapping contract  
**Owner:** EGR-1502  
**Result schema:** `ResolutionResult` schema version 1

## Boundary

`EngramCore.resolve_request` is the stable Python entry point for unified
resolution. It accepts concrete Python values and returns one validated
dictionary. Runtime records are dictionaries rather than attribute-bearing
objects; callers use keyed access and may use the explicit codecs in
`engram.resolution` when a JSON-compatible or serialized value is required.

The existing lower-level `Engram` query, pattern, graph, conversation, and
regulated-cache operations remain compatible and are not deprecated by this
contract. CLI, MCP, and the current gRPC v1 service remain separate adapters
with their existing contracts.

## Resolve inputs

| Input | Concrete type and absence | Rule |
| --- | --- | --- |
| `request` | nonempty string | Original caller text; bounded and validated |
| `request_id` | nonempty string | Retry identity; conflicting reuse fails |
| `user_id` | nonempty string, default `"0"` | Selects isolated, TTL-bound context for bounded follow-up enrichment |
| `namespace` | string, default `""` | Exact scope component |
| `context_fingerprint` | string, default `""` | Exact scope component |
| `identity` | mapping, default `{}` | Empty builds standalone identity; nonempty must be a validated authoritative `QueryIdentity` with matching scope |
| `required_metadata` | dictionary, default `{}` | Exact required accepted-response metadata |
| `required_source_label` | string, default `""` | Empty means no source-label requirement |
| `budget` | mapping, default `{}` | Empty captures the default bounded budget; nonempty must be a validated `ResolutionBudget` |
| `configured_resolvers` | tuple of nonempty strings, default `()` | Empty selects the configured plan |
| `accept_exact` | boolean, default `false` | Explicit direct-answer permission for one eligible exact result |
| `cancellation_check` | zero-argument callable, default `()` | Transient cooperative check; raises `ResolutionCancelledError` when the caller cancels |

Falsey non-mapping identity or budget values such as `()`, `[]`, `""`, `0`,
and `false` are malformed rather than alternate absence values. This preserves
one concrete absence type at the public boundary.

The cancellation callback is invoked before expensive resolver work and at
cooperative structured, semantic, and composition boundaries. It is not serialized
or included in the `request_id` signature. A cancelled attempt publishes no partial
resolver result, is not cached as a knowledge miss, and can be retried with the same
request ID. The callback cannot interrupt a thread already blocked in an external
driver call. Engram does not require a MemGraph timeout: the graph-bound request may
remain outstanding, while optional graph I/O isolation keeps unrelated local requests
and status available. The [deployment runbook](operations/deployment-and-rollback-v1.md)
defines graph disablement and supervisor-stop handling.

## Result access

The exact fields and outcome invariants remain frozen by the
[Section 7 core handoff](evidence/core-handoff.md). Typical keyed access is:

```python
result = core.resolve_request(
    "What is Engram?",
    "resolution-42",
    user_id="sarah",
    namespace="support",
    configured_resolvers=("exact", "pattern", "lexical"),
)

outcome = result["outcome"]
candidates = result["response_candidates"]
package_available = result["evidence_package_available"]
claim_records = result["evidence_package"]["records"]
```

`resolution_result_to_dict`, `resolution_result_to_json`, and their strict
decoders preserve schema version 1, concrete absence, exact accepted text,
bounded evidence, and unsupported-version rejection. Full Claim records occur
only inside `evidence_package`; they never authorize an Engram answer.

The compact previous query frame is session state, not accepted knowledge. It
is defined with canonical relation resolution in the
[Section 8 contextual contract](contextual/contracts-v2.md). Supplying
`user_id` does not change the version-1 result dictionary.

## Feedback and compatibility

`record_resolution_feedback` consumes one candidate from the keyed result and
one typed external Regulator outcome. `inspect_feedback_learning` returns a
bounded dictionary snapshot. Existing proposal `resolve` remains the regulated
proposal-verdict operation; it is distinct from unified `resolve_request`.

No lower-level Python call is removed or reinterpreted in v1, so no deprecation
warning is required. A future incompatible Python shape requires an explicit
new API/version and a documented migration period rather than changing this
mapping in place.

## Verification

- `tests/test_service.py` covers mapping-only identity and budget absence,
  authoritative identity input, exact result fields, keyed candidate feedback,
  and malformed boundary values.
- `tests/test_resolution_contracts.py` covers deterministic codecs, exact
  fields, schema rejection, concrete absence, and outcome invariants.
- `tests/test_resolvers.py` covers response candidates, full Claim packages,
  accounting, bounded execution, cancellation, and fail-soft dependency behavior.
- `tests/test_service.py` proves that cancellation is transient, publishes no cached
  result, and permits an identical request-ID retry.
- Existing CLI, MCP, gRPC, conversation, and regulated-cache suites protect the
  unchanged compatibility surfaces.
