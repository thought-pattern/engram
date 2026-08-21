# Unified resolution resource and timing contract v2

Status: current internal contract, superseding the deadline-bearing `ResolutionBudget` and `ResolverBudget` v1 shapes.

## ResolutionBudget

`ResolutionBudget` version 2 has exactly these fields:

- `schema_version`
- `max_resolvers`
- `max_candidates`
- `max_graph_rows`
- `max_vector_results`
- `max_evidence`
- `max_evidence_bytes`
- `max_output_bytes`
- `max_diagnostic_bytes`
- `max_working_memory_bytes`
- `allowed_cost_classes`
- `started_ns`

The `max_*` values and cost classes are non-time resource controls. `started_ns` is recaptured from the trusted monotonic clock when a frame is built and exists only to measure complete resolution duration. There is no total-time limit, per-resolver time limit, or deadline.

`ResolverBudget` version 2 carries the applicable candidate, graph-row, vector-result, evidence, byte, output, diagnostic, and working-memory allowances. It has no deadline.

## Execution and reporting

The executor runs its deterministic resolver plan subject to non-time resource availability. It measures every resolver with the injected monotonic clock and replaces any resolver-supplied elapsed value with the observed `BudgetConsumption.elapsed_ns`. The orchestrator reports complete turn length as at least the difference between its trusted measurement start and final observation.

Elapsed time never changes resolver state, truncates a result, adds an exhausted dimension, suppresses evidence, or converts an otherwise completed lookup into `MISS`. A backend exception, including an externally produced timeout exception, is a typed resolver failure; it is not reinterpreted as evidence that no knowledge exists.

The Python core accepts a transient cooperative cancellation callback. It is checked before resolver work, between resolvers, and within supported structured, semantic, evidence, and composition loops. `ResolutionCancelledError` propagates without a partial execution report, cached resolution record, or reusable knowledge miss. The callback is not serialized or encoded in request identity.

Client deadlines and backend operational controls remain adapter/runtime concerns and are not encoded in the resolution budget. A cooperative check cannot interrupt a thread blocked in an external driver call, so enabled graph deployments require Memgraph's server-side query-execution timeout as the hard in-flight bound. These controls stop work or classify operational failure; they are not knowledge-quality policy. The [deployment runbook](../operations/deployment-and-rollback-v1.md) defines the operator requirement.

## Migration

Version 1 budget and lease payloads are not current shapes. Internal callers rebuild budgets through `resolution_budget()` or `capture_resolution_budget()` and must not copy v1 deadline fields into a v2 request. Resolution requests and query frames are transient, so no durable knowledge migration is required.

Behavior coverage proves codecs and resource bounds, reports an injected eight-second resolver as completed, and does not pin production turn lengths in unit tests. It also proves that caller cancellation publishes no partial executor result, is not cached, and permits an identical request-ID retry. Live turn lengths are recorded by the MemGraph comparison artifact rather than enforced as gates.
