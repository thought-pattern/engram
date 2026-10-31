# Engram gRPC integration

## Runtime boundary

`RegulatorOutcome` is Engram's standalone external verdict type; its name does
not assign Tapestry component ownership. In Tapestry, Will interprets/routes
Channel input under `will.yml` and owns recall sufficiency and the Engram bridge.
Utility serves inference wherever it is needed and sufficient; Regulator only judges outbound Channel soundness
and alone generates IDK. Tapestry selects only `model.actor` and `model.regulator`,
with Will/Utility/Regulator sharing the latter. Engram's independent API is unchanged.

Engram exposes one protobuf package, `engram`, from one source
contract: [engram.proto](../engram/engram.proto). The contract publishes two
services from the same generated modules:

- `engram.EngramService` owns conversation, proposal, response-cache, and
  status operations.
- `engram.EngramEvidenceService` owns unified resolution through
  `ResolveEvidence`.

These are capabilities of one Engram component, not protocol generations. One
`engram-grpc` process creates one `EngramCore`, registers both services and the
standard gRPC health service, and closes the core during graceful shutdown.
`ResolveEvidence` uses the same unified operation as Python `resolve_request`,
CLI `query`, and MCP `engram_query`. When graph access is enabled, transport and
rollout selection cannot remove it from a graph-eligible resolution plan.

Engram responses, conversations, proposals, and idempotency records live only
in bounded process memory. A new service process loads only the STATIC data
provided to its new core. Restart inherits no dynamic accepted responses,
learned conversational statements or facts, sessions, proposals, mutation
receipts, reports, turn diagnostics, or process counters. The running process is
the complete lifetime of those values. Optional Memgraph access supplies recall
reads and is not Engram-owned response or conversation state.

When graph recall is enabled, startup checks that the graph's schema is
compatible before publishing readiness: it must have every definition in
`engram/schema.cypher`, and it may have more. The graph's owner creates that
schema; Engram never writes to the graph.

During request resolution, a graph connection, query, or optional vector-index
failure contributes no graph result. If no local resolver supplies a result,
`ResolveEvidence` returns the same `MISS` it returns after a successful graph
query with no rows. Component status may still diagnose the graph failure; it is
not exposed as a separate unavailable resolution outcome.

## Installation and launch

```bash
python -m pip install -e .

engram-grpc \
  --bind 127.0.0.1:50051 \
  --config-path config.yml
```

Running the module is equivalent:

```bash
python -m engram.grpc_server --bind 127.0.0.1:50051 --config-path config.yml
```

The server accepts these options:

| Option | Default | Purpose |
| --- | --- | --- |
| `--bind` | `127.0.0.1:50051` | Listen address. |
| `--config-path` | empty | Engram YAML configuration. |
| `--max-workers` | `10` | Maximum concurrent unary RPC handlers. |
| `--grace-period` | `10` | Seconds allowed for RPCs to drain during shutdown. |
| `--tls-cert`, `--tls-key` | empty | PEM certificate/key pair for server TLS. |
| `--log-level` | `INFO` | Server log level. |

Without `--config-path` the server runs on the built-in defaults: no seed files
and no graph database. `--config-path config.yml` reads Engram's own file, which
can enable a graph database with `graph.enabled: true`. A host that runs Engram
as one of its parts, such as Tapestry, starts the server from its own code with
the complete configuration it passes to `engram.config.config_from_dict`, and
Engram reads no file.

## Process model

```text
gRPC clients
     |
     v
one Engram gRPC server process
     |
     v
one EngramCore
     |
     +-- bounded response and fact memory
     +-- isolated conversation contexts
     +-- bounded proposal and idempotency records
     +-- optional Memgraph recall reads
```

Concurrent handlers delegate to the same core. The core serializes mutations
and owns chat, retrieval, learning, idempotency, and eviction. The evidence
adapter connects the gRPC call lifecycle to `EngramCore.resolve_request`'s
cooperative cancellation callback. Cancellation or an expired deadline at a
cooperative boundary discards transient resolution work and permits retry with
the same request ID.

## Protocol and generated code

The source contract, generated messages, type stubs, and service stubs live
directly in the `engram` library:

```text
engram/engram.proto
engram/engram_pb2.py
engram/engram_pb2.pyi
engram/engram_pb2_grpc.py
```

RPC requests have explicit fields, and `Resolve` uses the `RegulatorOutcome`
enum. Conversation, inspection, and cache responses use
`google.protobuf.Struct` at the external transport boundary. `ResolutionResult`
names every top-level unified result field, while bounded candidate, evidence,
diagnostic, resolver, and budget records remain core-owned structures carried
through `Struct` fields.

Regenerate after changing the contract:

```bash
python -m pip install -e ".[dev]"
python -m grpc_tools.protoc -I. --python_out=. --pyi_out=. \
  --grpc_python_out=. engram/engram.proto
```

The network test suite regenerates these files in a temporary directory and
compares them byte-for-byte with the committed output. The tool versions are
pinned in `pyproject.toml`.

## RPC surface

`engram.EngramService` provides:

| RPC | Purpose |
| --- | --- |
| `StartConversation` | Start one conversation; omitted/empty `user_id` means "0", whose session starts fresh. |
| `Chat` | Submit one observed conversation turn. |
| `InspectConversation` | Return one active conversation and core diagnostics. |
| `FinishConversation` | Return the current process-local report while the conversation remains active. |
| `StopConversation` | Release one conversation; unknown user "0" also deletes its session. |
| `AddFact` | Add one process-memory fact with an opaque source label. |
| `SetPredicate`, `GetPredicate` | Write or read one conversation-scoped value. |
| `Propose` | Retrieve scoped response candidates and record candidacy. |
| `Resolve` | Commit one typed Regulator verdict for one concrete candidate. |
| `LearnResponse` | Cache one non-`IDK` answer with scope and opaque metadata; preserve admitted multiline response text exactly. |
| `RetireResponse` | Remove one dynamic cached response after its owner establishes staleness. |
| `ResponsesBySupport` | List ACTIVE accepted-response statement IDs whose support references any given durable record ID. |
| `MaintainEngagement` | Plan, prepare, physically purge, or resume an exactly scoped administrative operation. |
| `GetStatus` | Return lifecycle, readiness, component, rollout, and telemetry status. |

Starting unknown user "0" returns a `conversation_token`. `Chat`,
`InspectConversation`, `FinishConversation`, and `StopConversation` for "0" must
present that token in their `conversation_token` field or fail with
`PERMISSION_DENIED`. An unknown-user conversation left idle for 300 seconds no
longer blocks a new start.

`engram.EngramEvidenceService` provides:

| RPC | Purpose |
| --- | --- |
| `ResolveEvidence` | Run unified resolution and return an `ANSWER`, `EVIDENCE`, or `MISS` result with the bounded Proposition package when available. |

### Request limits

| Text | Limit (UTF-8) |
| --- | --- |
| `Chat` text | 16,384 bytes (`MAX_REQUEST_BYTES`) |
| `request` in `ResolveEvidence`, `Propose`, and `LearnResponse` | 4,096 bytes (`MAX_CACHE_REQUEST_BYTES`) |

A regulated-cache request becomes an exact-match lookup key, so its limit is
the key's. Normalization lowercases the text and expands contractions and some
characters, so the normalized form must fit too; a request close to the limit
can exceed it once normalized. Both checks run before any other work and return
`INVALID_ARGUMENT` with a message naming the limit. A request that normalizes
to nothing, such as punctuation only, is rejected the same way.

## Engagement maintenance

`MaintainEngagement` carries a closed `Struct` with `action` (`plan`, `prepare`,
`purge`, or `resume`), a bounded `operation_id`, an exact native engagement
`visibility_scope`, and an array of opaque graph `dependency_ids`. The operation,
scope and normalized dependency set own the pause; a conflicting command fails.
`plan` is read-only. `prepare` rejects new work and drains active resolution and
graph-operation slots, moving readiness out of service. `purge` requires that
pause and deletes matching response artifacts, affected mutation receipts and
feedback through their existing owners, restoring all three on failure.

Selection includes exact metadata scope, support scope/dependencies and historic
supersession predecessors. Existing receipts inherit statement/scope bindings
from affected artifacts, and pruning retains those bindings in the existing
tombstones. Removal can therefore find affected retained state after the response
itself was evicted. It creates no removal artifact or tombstone. Unrelated
artifacts, statistics, feedback and replay state remain unchanged. Drained
request/proposal caches and unscoped session context are discarded after purge.
`resume` requires successful local purge; Tapestry owns when to issue it after its
other store owners have completed. Failure leaves Engram paused for exact retry.
While paused, `plan` with the same operation and Scope may supply an empty
dependency array to inspect the existing `maintenance_binding` and `purged`
state. The coordinator uses that retained binding to recover after graph commit;
prepare, purge and resume still require the complete exact binding. A conflicting
nonempty dependency list is rejected.

This is a trusted administrative mutation, subject to the same access controls
as other mutation RPCs. Real gRPC tests cover the lifecycle, concurrent draining,
late-request rejection, physical deletion, rollback, eviction/pruning retention,
peer preservation and retry.

## Python client example

```python
import grpc
from google.protobuf.json_format import MessageToDict

from engram import engram_pb2, engram_pb2_grpc

with grpc.insecure_channel("127.0.0.1:50051") as channel:
    conversation = engram_pb2_grpc.EngramServiceStub(channel)
    conversation.StartConversation(
        engram_pb2.StartConversationRequest(user_id="Alice")
    )
    turn = conversation.Chat(
        engram_pb2.ChatRequest(user_id="Alice", text="Hello, Engram."),
        timeout=5,
    )
    print(MessageToDict(turn, preserving_proto_field_name=True).get("response", ""))

    evidence = engram_pb2_grpc.EngramEvidenceServiceStub(channel)
    result = evidence.ResolveEvidence(
        engram_pb2.ResolveEvidenceRequest(
            request="What evidence is available?",
            request_id="resolve-1",
            user_id="Alice",
        ),
        timeout=5,
    )
    print(result.outcome, result.evidence_package.retained_count)
```

Use `grpc.secure_channel` with matching client credentials when the server is
launched with `--tls-cert` and `--tls-key`.

## Errors, health, and shutdown

Core failures map at the transport boundary:

| Core exception | gRPC status |
| --- | --- |
| `InvalidRequestError` | `INVALID_ARGUMENT` |
| `ResourceNotFoundError` | `NOT_FOUND` |
| `ConflictError` | `ABORTED` |
| `ConversationOwnershipError` | `PERMISSION_DENIED` |
| `LifecycleError` | `FAILED_PRECONDITION` |
| `ResourceExhaustedError` (session limit with `session_overflow: reject`) | `RESOURCE_EXHAUSTED` |
| Cancellation or expired deadline | `CANCELLED` or `DEADLINE_EXCEEDED` |
| Unexpected failure | `INTERNAL` with the message `internal Engram failure` |

Every typed failure supplies `engram-error-type` in trailing metadata. A typed
failure's message is written for the caller; exception text from anywhere else
never reaches the client. Every failure is logged on the server in full, with
its traceback: internal failures at `ERROR`, cancellations at `INFO`, and other
rejected requests at `WARNING`.

The server registers `grpc.health.v1.Health` for the aggregate empty service
name, `engram.EngramService`, and `engram.EngramEvidenceService`. It reports
`SERVING` only while the shared core is ready and healthy. `GetStatus` reports
process state, readiness, `memory_only`, active conversation count, component
readiness, rollout state, and bounded telemetry, plus `health`: the same
snapshot the HTTP health page's API serves.

## HTTP health page

The gRPC server also serves a read-only health page and API for developers and
operators on its own standard-library threaded HTTP listener, set by the `http`
section of Engram's configuration (`127.0.0.1:8101` by default; loopback or a
private LAN unicast address; the listener binds exactly that port). Engram's
gRPC server is threaded, so the page needs no event loop. The server's
`--config-path` and Tapestry's managed server pass that section; a server built
in code without `http` serves no page, and GetStatus still carries the snapshot.

| Route | Answer |
| --- | --- |
| `GET /` | The page; it polls `/api/health` |
| `GET /health.js`, `/health-common.js`, `/health.css` | The page's renderer, the shared polling and drawing code, and the stylesheet |
| `GET /api/health` | The snapshot as JSON; GetStatus's `health` member holds the same snapshot |
| `GET /health/live` | 200 while the listener answers |
| `GET /health/ready` | 200 while the core is ready and healthy and shutdown has not begun, otherwise 503 |

Every answer carries `Cache-Control: no-store`, `X-Content-Type-Options:
nosniff` and a same-origin content security policy, and the listener writes no
access log. `health_assets/` holds the page, its renderer, and copies of
Tapestry's shared health page script and stylesheet, because Engram runs without
the tapestry package.

The snapshot has the same shape as every Tapestry component's. A server
interceptor counts every Engram call (the standard gRPC health checks are not
counted) from arrival to answer, by call name and outcome: `OK`, the gRPC status
the call ended with, or the exception class when no status was set. The page
shows:

| Panel | Figures |
| --- | --- |
| Service | Readiness, start time, uptime, process ID and incarnation, with a notice when the server restarts and its counters reset |
| Requests | Calls received and answered by call and outcome, the last call received and the last answer sent, and the average time to answer with its sample count |
| Queue | Always empty: calls wait for a gRPC worker inside the gRPC library, where they cannot be seen |
| Active work | Each call in progress with its elapsed time |
| Tokens | "Engram makes no model calls" |
| Errors | Failed calls by outcome, and the last one with its call and time |
| Recent activity | Items waiting, items finished per second and tokens per second from five-second samples kept for ten minutes |
| Recent outcomes | The latest 32 finished calls with outcome and run time |
| Engram | State, healthy, memory only, active conversations, rollout default mode and namespace overrides |
| Components | Each component's enabled and ready state, with its implementation, error or plugins |
| Recall and resolution | Pattern matcher, regulated cache, regulator outcomes, graph recall and resolution counts and times |
| Resolvers | Each resolver's invocations, states and selected contributions |

Only call names, outcomes, times and counts are kept: never conversation text,
user names or IDs, facts or responses. The page has no controls and never
contacts another service. A listener that cannot bind is logged and reported in
the snapshot's `http` state (`available` false, with the error type and code)
while Engram keeps serving gRPC.

SIGINT and SIGTERM mark health not serving, stop admission, allow in-flight
calls to drain for `--grace-period`, and close the core. Closing discards all
remaining process memory and disconnects graph resources. Repeated shutdown is
idempotent.

## Security and authority

- Bind to loopback unless remote clients are explicitly required.
- Use TLS or a trusted encrypted proxy for remote transport.
- Treat returned conversation reports as sensitive application data.
- Grant mutation RPCs only to callers authorized to change Engram process
  memory.
- Engram only reads the graph. Its graph client refuses mutating Cypher, so
  this does not depend on restricted credentials. The graph's owner creates the
  schema.

## Verification coverage

Network tests cover the complete service descriptors, multi-conversation
isolation, anonymous cleanup, shared process-memory facts, predicates,
in-memory reports, health, proposal and verdict operations, evidence
resolution, typed errors, cancellation, empty-memory restart, graceful
shutdown, TLS validation, and generated-code reproducibility. `tests/test_health.py`
covers the health page's routes and headers, GetStatus and `/api/health`
returning the same snapshot, figures that move with real calls and hold no
conversation content, and a port in use reported while Engram keeps serving.
