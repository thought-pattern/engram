# Section 15 concurrency and idempotency contract v1

**Status:** Implemented by EGR-1506
**Scope:** Python, unchanged MCP tools, gRPC v1, and gRPC v2 evidence resolution

## One ownership boundary

Every adapter delegates to one `EngramCore`. Mutation receipts, proposal state, per-user resolution serialization, checkpoints, repository publication, compatibility-view refresh, and index refresh remain core or lower-layer responsibilities. No adapter keeps a competing receipt ledger, live repository, checkpoint coordinator, or response cache.

The executable cross-adapter race starts one MCP-owned core, then calls that same core through Python, MCP, and a network gRPC server. An exact `LearnResponse` retry returns the same statement and receipt through all three boundaries; changed payload under that request ID returns the same conflict. Six concurrent accept attempts against one proposal produce one non-idempotent result, five replays, one hit, and the same session response through Python, MCP, and gRPC inspection.

Atomic visibility is inherited from the Section 3 coordinator. The authoritative repository, receipt, compatibility statement, and response indexes publish together after the configured checkpoint. Adapter tests assert repository consistency after races; Section 3 fault-injection tests cover before-write, durable-write, publication, and recovery boundaries.

## Deadline and cancellation behavior

| Boundary | Behavior |
| --- | --- |
| Python unified resolution | Accepts `cancellation_check`. A raised `ResolutionCancelledError` is transient, records no completed request result or negative miss, and permits the same request ID to be retried. The caller owns any wall-clock deadline. |
| MCP v1 tools | Schemas remain unchanged and expose no unified-resolution operation or deadline field. The MCP host may abandon its wait, but a synchronous mutation already running may complete. The caller must retry with the same request ID to learn the recorded outcome; the adapter never claims rollback. |
| gRPC v1 | A client deadline can end the RPC wait, but already-started conversation or mutation work may complete. Stable request IDs remain the recovery mechanism. |
| gRPC v2 evidence | The adapter connects gRPC cancellation and deadline state to the core cooperative callback. Cancellation observed at a cooperative boundary raises a typed transient error and leaves the request ID uncached. Work inside a graph-driver call remains non-interruptible until the driver returns. |

This contract does not promise asynchronous thread interruption or transaction rollback after publication. Cancellation is cooperative; durable mutation outcome is determined by the receipt and checkpoint state, not by whether the caller was still waiting.

## Shutdown

Core shutdown stops admission and waits for active resolution and graph-operation counts to drain. The gRPC server enters graceful shutdown, drains within its configured grace period, then closes the shared core. A non-responsive external driver may still require the deployment supervisor's ordinary hard-stop procedure. MCP stop serializes with its single process-owned conversation and closes only after the core operation returns.

## Verification

- `tests/test_service.py` covers transient Python cancellation, retry identity, per-user serialization, unrelated local progress during graph I/O, and shutdown waiting.
- `tests/test_responses.py` covers concurrent exact retries, conflicting writes, supersession races, checkpoints, and atomic visibility.
- `tests/test_mcp_server.py` covers concurrent proposal verdict replay and caller abandonment of a started mutation.
- `tests/test_grpc_server.py` covers cross-adapter exact/conflicting retries, six-way proposal resolution, consistent inspection, v1 started-work deadlines, v2 cooperative cancellation/deadlines, durable recovery, and graceful shutdown.
