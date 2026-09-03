# Engram Python API v1

**Status:** Current transport-neutral mapping contract
**Owner:** Engram project
**Result schema:** `ResolutionResult` schema version 1

## Boundary

`EngramCore.resolve_request` is the Python entry point for unified
resolution. It accepts concrete Python values and returns one validated
dictionary. Callers use keyed access and may use the explicit codecs in
`engram.resolution` when a JSON-compatible or serialized value is required.

Lower-level `Engram` statement, pattern, graph, and conversation operations are
current first-party interfaces. MCP and the unversioned gRPC services are
separate `EngramCore` adapters.

## Process lifetime and static startup data

Each new process loads only the STATIC data provided for that startup. Structured
pattern, response, and template entries are loaded once into a fresh `Engram`
with `load_static_data` before it is passed to `EngramCore`. The operation rejects
an Engram that already contains statements, sessions, accepted responses, or
mutation receipts; it is not a refresh or synchronization interface.

Restart requires the owner to provide the current static data again. Dynamic
accepted responses, learned conversational statements and facts, sessions,
proposals, mutation receipts, reports, turn diagnostics, and their process
counters are not copied or recovered.

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
status available during that call.

`resolve_request` also applies the process configuration's exact namespace rollout
selection. The policy version and selected mode participate in retry identity.
When graph access is enabled, `structured_graph` remains in every resolver plan,
including caller-supplied plans and every rollout mode. `disabled` executes
resolution and suppresses its result to `MISS`; `shadow` executes resolution while
suppressing candidate output and accepted-success credit; `evidence_only`
downgrades an answer to `EVIDENCE`; `rollback` uses exact and configured graph
retrieval and returns evidence or `MISS`; and `regulated_direct_answer` preserves
the behavior described by `accept_exact`. An interface cannot disable a configured
graph through resolver selection; graph participation is disabled only by
`graph.enabled: false`.
The configured graph resolver runs before local exact-answer short-circuiting and
uses the evaluation time captured by the shared request clock.
`core.status()["rollout"]` reports the policy version, default mode, override count,
and fixed per-mode counts aggregated across namespaces.

## Result access

`ResolutionResult` is a validated dictionary with one `ANSWER`, `EVIDENCE`, or
`MISS` outcome, bounded candidates, resolver results, diagnostics, budget
accounting, and an optional bounded Proposition evidence package. Typical keyed
access is:

```python
result = core.resolve_request(
    "What is Engram?",
    "resolution-42",
    user_id="account-42",
    namespace="support",
    configured_resolvers=("exact", "sparse", "support_semantic"),
)

outcome = result["outcome"]
candidates = result["response_candidates"]
package_available = result["evidence_package_available"]
proposition_records = result["evidence_package"]["records"]
```

`resolution_result_to_dict`, `resolution_result_to_json`, and their strict
decoders preserve schema version 1, concrete absence, exact accepted text,
bounded evidence, and unsupported-version rejection. Full Proposition records occur
inside `evidence_package`; fusion authorizes an Engram answer.

The compact previous query frame supplies session context; the repository owns
accepted knowledge. It
is defined with canonical relation resolution in the
[graph retrieval contracts](graph-retrieval.md). `user_id` selects the bounded
conversation context used to enrich the frame.

## Feedback and accounting

`record_resolution_feedback` consumes one candidate from the keyed result and
one typed external Regulator outcome. `inspect_feedback_learning` returns a
bounded dictionary snapshot. Proposal `resolve` remains the regulated
proposal-verdict operation; it is distinct from unified `resolve_request`.

Accepted-response candidacy and success statistics mutate only the authoritative
artifact collection. The conversational statement matcher has separate accounting.

`learn_response` admits one new artifact without implicit replacement.
`supersede_response` atomically retires the current generation and admits its
explicit replacement, while `retire_response` removes an artifact established as
globally stale. Generated conversational or pipeline responses are not admitted by
any implicit learning hook.

## Operational telemetry

`EngramCore.operational_telemetry()` returns the fixed-cardinality schema-version 1
process aggregate. `core.status()["telemetry"]` returns the same information alongside
readiness. It includes outcomes, fixed resolver contributions and states, observed
latency buckets, budget/resource consumption, fixed Regulator outcomes, and graph
consultation/hit/miss/failure counters through fixed aggregate keys.

## Verification

- `tests/test_service.py` covers mapping-only identity and budget absence,
  authoritative identity input, exact result fields, keyed candidate feedback,
  and malformed boundary values.
- `tests/test_resolution_contracts.py` covers deterministic codecs, exact
  fields, schema rejection, concrete absence, and outcome invariants.
- `tests/test_resolvers.py` covers response candidates, full Proposition packages,
  accounting, bounded execution, cancellation, and fail-soft dependency behavior.
- `tests/test_service.py` proves transient cancellation and identical request-ID retry.
- MCP, gRPC, conversation, and regulated-cache suites verify the current
  first-party interfaces.
