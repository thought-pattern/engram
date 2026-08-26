# Engram Python API v1

**Status:** Stable transport-neutral mapping contract
**Owner:** Engram project
**Result schema:** `ResolutionResult` schema version 1

## Boundary

`EngramCore.resolve_request` is the stable Python entry point for unified
resolution. It accepts concrete Python values and returns one validated
dictionary. Callers use keyed access and may use the explicit codecs in
`engram.resolution` when a JSON-compatible or serialized value is required.

The existing lower-level `Engram` query, pattern, graph, conversation, and
regulated-cache operations continue unchanged. CLI, MCP, and the current gRPC
v1 service remain separate adapters
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
| `required_source_label` | string, default `""` | Empty accepts every source label |
| `budget` | mapping, default `{}` | Empty captures the default bounded budget; nonempty must be a validated `ResolutionBudget` |
| `configured_resolvers` | tuple of nonempty strings, default `()` | Empty selects the configured plan |
| `accept_exact` | boolean, default `false` | Explicit direct-answer permission for one eligible exact result |
| `cancellation_check` | zero-argument callable, default `()` | Transient cooperative check; raises `ResolutionCancelledError` when the caller cancels |

Falsey non-mapping identity or budget values such as `()`, `[]`, `""`, `0`,
and `false` are malformed. This preserves
one concrete absence type at the public boundary.

The cancellation callback is invoked before expensive resolver work and at
cooperative structured, semantic, and composition boundaries. It remains transient
and outside the `request_id` signature. Cancellation discards partial resolution and
permits retry with the same request ID. An executing external driver call continues
until the driver returns. Graph I/O isolation keeps unrelated local requests and
status available during that call. The
[deployment runbook](operations/deployment-and-rollback-v1.md) defines graph
disablement and supervisor-stop handling.

`resolve_request` also applies the process configuration's exact namespace rollout
selection. The policy version and selected mode participate in retry identity.
`disabled` returns a resolver-free `MISS`; `shadow` suppresses candidate output and
accepted-success credit; `evidence_only` downgrades an answer to `EVIDENCE`;
`rollback` executes exact-only retrieval and returns evidence or `MISS`; and
`regulated_direct_answer` preserves the behavior described by `accept_exact`.
`core.status()["rollout"]` reports the policy version, default mode, override count,
and fixed per-mode counts aggregated across namespaces.

## Result access

The exact fields and outcome invariants remain frozen by the
[unified resolution result contract](evidence/resolution-result-v1.md). Typical keyed access is:

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
inside `evidence_package`; fusion authorizes an Engram answer.

The compact previous query frame supplies session context; the repository owns
accepted knowledge. It
is defined with canonical relation resolution in the
[graph retrieval contracts](graph-retrieval.md). Supplying
`user_id` preserves the version-1 result dictionary.

## Feedback and compatibility

`record_resolution_feedback` consumes one candidate from the keyed result and
one typed external Regulator outcome. `inspect_feedback_learning` returns a
bounded dictionary snapshot. Existing proposal `resolve` remains the regulated
proposal-verdict operation; it is distinct from unified `resolve_request`.

Version 1 retains every lower-level Python call. A future incompatible Python
shape requires a new API version and documented migration period.

## Operational telemetry

`EngramCore.operational_telemetry()` returns the fixed-cardinality schema-version 1
process aggregate. `core.status()["telemetry"]` returns the same information alongside
readiness. It includes outcomes, fixed resolver contributions and states, observed
latency buckets, budget/resource consumption, rebuilds, durability, and fixed
Regulator outcomes through fixed aggregate keys. Operational use and incident handling
are documented in the [deployment runbook](operations/deployment-and-rollback-v1.md).

## Verification

- `tests/test_service.py` covers mapping-only identity and budget absence,
  authoritative identity input, exact result fields, keyed candidate feedback,
  and malformed boundary values.
- `tests/test_resolution_contracts.py` covers deterministic codecs, exact
  fields, schema rejection, concrete absence, and outcome invariants.
- `tests/test_resolvers.py` covers response candidates, full Claim packages,
  accounting, bounded execution, cancellation, and fail-soft dependency behavior.
- `tests/test_service.py` proves transient cancellation and identical request-ID retry.
- Existing CLI, MCP, gRPC, conversation, and regulated-cache suites protect the
  unchanged compatibility surfaces.
