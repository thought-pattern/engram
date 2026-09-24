# Engram gRPC integration

## Runtime boundary

Engram exposes one unversioned protobuf package, `engram`, from one source
contract: [engram.proto](../engram/engram.proto). The contract publishes two
services from the same generated modules:

- `engram.EngramService` owns conversation, proposal, response-cache, and
  status operations.
- `engram.EngramEvidenceService` owns unified resolution through
  `ResolveEvidence`.

These are capabilities of one Engram component, not protocol generations. One
`engram-grpc` process creates one `EngramCore`, registers both services and the
standard gRPC health service, and closes the core during graceful shutdown.

Engram responses, conversations, proposals, and idempotency records live only
in bounded process memory. A new service process loads only the STATIC data
provided to its new core. Restart inherits no dynamic accepted responses,
learned conversational statements or facts, sessions, proposals, mutation
receipts, reports, turn diagnostics, or process counters. The running process is
the complete lifetime of those values. Optional Memgraph access supplies recall
reads and is not Engram-owned response or conversation state.

When graph recall is enabled, startup verifies the selected deployment mode and
schema before publishing readiness. A Tapestry deployment uses Tapestry's graph
schema. An independent Engram deployment installs the current Engram recall
subset through the separate administrative schema command.

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

Regenerate after changing the Tapestry-dictated contract:

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
| `StartConversation` | Start one isolated conversation; an empty `user_id` receives a fresh anonymous context. |
| `Chat` | Submit one observed conversation turn. |
| `InspectConversation` | Return one active conversation and core diagnostics. |
| `FinishConversation` | Return the current process-local report while the conversation remains active. |
| `StopConversation` | Release one conversation; an anonymous context is deleted. |
| `AddFact` | Add one process-memory fact with an opaque source label. |
| `SetPredicate`, `GetPredicate` | Write or read one conversation-scoped value. |
| `Propose` | Retrieve scoped response candidates and record candidacy. |
| `Resolve` | Commit one typed Regulator verdict for one concrete candidate. |
| `LearnResponse` | Cache one non-`IDK` answer with scope and opaque metadata. |
| `RetireResponse` | Remove one dynamic cached response after its owner establishes staleness. |
| `GetStatus` | Return lifecycle, readiness, component, rollout, and telemetry status. |

`engram.EngramEvidenceService` provides:

| RPC | Purpose |
| --- | --- |
| `ResolveEvidence` | Run unified resolution and return an `ANSWER`, `EVIDENCE`, or `MISS` result with the bounded Proposition package when available. |

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
| `LifecycleError` | `FAILED_PRECONDITION` |
| Cancellation or expired deadline | `CANCELLED` or `DEADLINE_EXCEEDED` |
| Unexpected adapter failure | `INTERNAL` with a generic client message |

Every typed failure supplies `engram-error-type` in trailing metadata. Detailed
unexpected exceptions remain in server logs.

The server registers `grpc.health.v1.Health` for the aggregate empty service
name, `engram.EngramService`, and `engram.EngramEvidenceService`. It reports
`SERVING` only while the shared core is ready and healthy. `GetStatus` reports
process state, readiness, `memory_only`, active conversation count, component
readiness, rollout state, and bounded telemetry.

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
- Runtime graph operations issue reads and no writes. This behavior does not
  require restricted credentials or an interface incapable of mutation. Schema
  setup is a separate explicit administrative operation.

## Verification coverage

Network tests cover the complete service descriptors, multi-conversation
isolation, anonymous cleanup, shared process-memory facts, predicates,
in-memory reports, health, proposal and verdict operations, evidence
resolution, typed errors, cancellation, empty-memory restart, graceful
shutdown, TLS validation, and generated-code reproducibility.
