# Engram gRPC Integration

## Status and scope

Engram provides a versioned unary gRPC API in `engram.v1`. The packaged
`engram-grpc` process creates exactly one `EngramCore`, exposes it through a
thin protobuf adapter, publishes the standard gRPC health service, and closes
the core during graceful shutdown.

There is no replication, load balancing, distributed locking, shared
transaction service, or cross-instance consistency protocol. Deploy one
server process with one JSON store.

The gRPC contract does not transport Knowledge Graph Claim records. When graph
recall is enabled, Engram reads the configured Memgraph instance directly. Its
Schema 3.5 read subset understands Tapestry's half-open Claim times and excludes
closed or retrieval-only (`generic_relation`) Claims. Consequently Phase A does
not add protobuf fields or create a separate Engram graph in a Tapestry
deployment.

Schema 3.5 may also expose `research_leaf_proof_id` on a Claim as an
application-owned proof receipt. Engram does not interpret or mutate it; recall
continues to rely on the active canonical Claim and configured vector index.

The subset also indexes the canonical semantic fingerprint and carries Claim
trust/ownership classification. These are graph properties read from the shared
Memgraph schema; Engram remains read-only and never assigns identity, temporal
bounds, trust, or ownership itself.

## Vector-search boundary

When graph vector recall is enabled, `Propose` embeds the request with the
configured local sentence-transformer and queries Memgraph's Claim-premise
vector index. ANN results are intersected with the Claim identifiers already
stored in each exactly scoped cached response. Vector similarity therefore
helps find a previously verified response whose durable support is semantically
related to the request; it never returns arbitrary Claim, Passage, proof, or KG
text as an Engram answer. Keyword retrieval remains active and the two scores
are merged before candidate selection.

Engram's general read-only Cypher guard still rejects `CALL`. The sole exception
is an internal, fixed `vector_search.search` query: callers cannot supply its
Cypher or index name, the configured identifier is validated, the result limit
is bounded, and inactive, closed, non-canonical, or retrieval-only Claims are
excluded. The same ANN lookup is available as a fallback for conversational
graph recall after exact canonical label, alias, and keyword lookup misses.

The standalone server defaults to no graph access unless `--config-path` is
supplied. With vectors enabled it loads and probes the local embedding model and
the configured Memgraph vector index before publishing a healthy gRPC service;
invalid graph, model, index, or dimension configuration fails startup.

## Installation and launch

```bash
python -m pip install -e .

engram-grpc \
  --bind 127.0.0.1:50051 \
  --store-path state/engram.json \
  --seed-path data/seed.json \
  --transcript-directory state/transcripts \
  --report-directory state/reports
```

Running the module is equivalent:

```bash
python -m engram.grpc_server --bind 127.0.0.1:50051
```

The server accepts these deployment options:

| Option | Default | Purpose |
| --- | --- | --- |
| `--bind` | `127.0.0.1:50051` | Listen address. |
| `--store-path` | empty | Persistent JSON store; empty disables store persistence. |
| `--seed-path` | `data/seed.json` | Seed synchronized at startup; pass an empty value to disable it. |
| `--config-path` | empty | Engram YAML configuration. |
| `--transcript-directory` | empty | Server-owned per-user recovery transcript directory. |
| `--report-directory` | empty | Server-owned output directory required by `FinishConversation`. |
| `--max-workers` | `10` | Maximum concurrent unary RPC handlers. |
| `--grace-period` | `10` | Seconds allowed for RPCs to drain during shutdown. |
| `--tls-cert`, `--tls-key` | empty | PEM certificate/key pair for server TLS. |
| `--log-level` | `INFO` | Server log level. |

Store, seed, configuration, transcript/report locations, binding, and TLS are
server configuration. Clients cannot submit filesystem paths through RPCs.
Transcript and report filenames are stable SHA-256-derived names, so arbitrary
caller-owned user labels cannot escape their configured directories.

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
     +-- shared Engram knowledge and response cache
     +-- isolated user conversation contexts
     +-- bounded in-memory proposal/idempotency records
     +-- one optional JSON store
```

Concurrent RPC handlers all delegate to the same core. The core serializes
state transitions with its application lock; the adapter does not reimplement
chat, retrieval, learning, idempotency, or persistence logic.

## Protocol and generated code

The source contract is [engram.proto](../engram/v1/engram.proto). Generated
Python messages, type stubs, and service stubs are committed beside it. RPC
requests have explicit fields and `Resolve` uses the `RegulatorOutcome` enum.
Engram conversation, inspection, and cache results use
`google.protobuf.Struct` because those JSON-ready diagnostic payloads are
extensible application data rather than stable scalar records.

Install the development dependencies and regenerate after changing the proto:

```bash
python -m pip install -e ".[dev]"
python -m grpc_tools.protoc -I. --python_out=. --pyi_out=. \
  --grpc_python_out=. engram/v1/engram.proto
```

The test suite regenerates the files in a temporary directory and compares
them byte-for-byte with the committed output. Regenerate with the pinned
`grpcio-tools` and `protobuf` versions in `pyproject.toml`; the committed stubs
currently target `grpcio-tools` 1.83.0 and protobuf 7.35.1.

## RPC surface

| RPC | Purpose |
| --- | --- |
| `StartConversation` | Start an isolated user runtime; an empty `user_id` becomes `"0"`. |
| `Chat` | Submit one observed chatbot turn. |
| `InspectConversation` | Return user context, learned knowledge, metrics, and core status. |
| `FinishConversation` | Flush and write JSON/Markdown reports without stopping the user runtime. |
| `StopConversation` | Flush and release one user runtime without stopping the server. |
| `AddFact` | Add a shared, unattributed fact with an opaque source label. |
| `SetPredicate`, `GetPredicate` | Write/read a caller-owned value in one user context. |
| `Propose` | Retrieve scoped response-cache candidates without recording a hit. |
| `Resolve` | Commit one typed Regulator verdict; accepted retries cannot double-credit. |
| `LearnResponse` | Cache a non-`IDK` Actor answer with scope and metadata. |
| `RetireResponse` | Retire one dynamic, patternless cached answer. |
| `GetStatus` | Return lifecycle, readiness, durability, checkpoint, and conversation status. |
| `Flush` | Explicitly checkpoint the configured store. |

`Propose`, `Resolve`, `LearnResponse`, and `RetireResponse` implement the same
Tapestry contract documented in the
[Tapestry–Engram integration guide](https://github.com/thought-pattern/tapestry/blob/develop/project/design/engram-integration.md).
They do not require an active chatbot conversation.

## Python client example

```python
import grpc
from google.protobuf.json_format import MessageToDict

from engram.v1 import engram_pb2, engram_pb2_grpc

with grpc.insecure_channel("127.0.0.1:50051") as channel:
    stub = engram_pb2_grpc.EngramServiceStub(channel)
    stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
    turn = stub.Chat(
        engram_pb2.ChatRequest(user_id="Alice", text="Hello, Engram."),
        timeout=5,
    )
    print(MessageToDict(turn, preserving_proto_field_name=True)["response"])
```

Use `grpc.secure_channel` with matching client credentials when the server is
launched with `--tls-cert` and `--tls-key`. The built-in TLS option provides
server authentication and encryption; application authentication and
authorization remain deployment responsibilities.

## Error contract

Core failures map consistently at the transport boundary:

| Core exception | gRPC status |
| --- | --- |
| `InvalidRequestError` | `INVALID_ARGUMENT` |
| `ResourceNotFoundError` | `NOT_FOUND` |
| `ConflictError` | `ABORTED` |
| `LifecycleError` | `FAILED_PRECONDITION` |
| `PersistenceError` | `UNAVAILABLE` |
| Unexpected adapter failure | `INTERNAL` with a generic client message |

Every typed failure supplies `engram-error-type` in trailing metadata.
Persistence failures also supply `engram-operation` and
`engram-state-changed`. The latter is `true` when the requested mutation was
already applied to live memory before its checkpoint failed.

## Health and readiness

The server registers `grpc.health.v1.Health` for both the aggregate empty
service name and `engram.v1.EngramService`. It reports `SERVING` only while the
core is both ready and healthy. A degraded store, closing core, closed core, or
graceful shutdown reports `NOT_SERVING`.

`GetStatus` provides the detailed source data: `state`, `ready`, `healthy`,
`durability`, `dirty`, `last_checkpoint_at`, `last_persistence_error`,
`active_conversations`, and `store_path`.

## Persistence, retry, and deadlines

With a configured store, successful durable mutations synchronously use the
core's atomic checkpoint path. Proposals and idempotency records remain
bounded, five-minute, process-local state and are never serialized.

A checkpoint error does not roll back an in-memory mutation. While degraded,
the core remains ready but standard health becomes `NOT_SERVING`. Correct the
store and either call `Flush` or repeat the exact regulated-cache operation
with the same `request_id`; its idempotency path retries persistence without
applying or crediting the mutation twice.

A client deadline or cancellation also cannot claim rollback after handler
execution has started. Chat calls are not idempotent, so inspect their user
context before deciding whether to repeat an ambiguous timed-out turn.
Regulated-cache calls should retain and reuse their logical `request_id`.

After restart, learned responses, facts, user contexts, statistics, and
retirements come from the last completed checkpoint. Outstanding proposal IDs
are expired; invoke the Actor again instead of assuming that an unresolved
candidate was accepted.

## Graceful shutdown

SIGINT and SIGTERM request shutdown. The process marks health not-serving,
stops accepting new RPCs, allows in-flight calls to complete within
`--grace-period`, then calls `EngramCore.close()` for the final checkpoint.
Repeated server shutdown is idempotent.

If the final checkpoint fails, the process logs the persistence failure and
exits unsuccessfully. The core's `PersistenceError` still distinguishes
whether live state differed from the last durable checkpoint. An application
embedding `EngramGrpcServer` may correct the store and call `stop()` again to
retry the final checkpoint; the already-stopped transport is not restarted.

## Security and authority boundaries

- Bind to loopback unless remote clients are explicitly required.
- Use TLS or a trusted encrypted proxy for remote transport.
- Protect the store, transcripts, and reports as application data. Engram
  creates service-owned artifact directories with owner-only permissions
  (`0700`) and writes stores, transcripts, and reports as owner-only files
  (`0600`) on platforms that support POSIX modes. Deployment ACLs remain the
  authoritative boundary on non-POSIX systems.
- `source_label` and metadata are provenance, not authorization decisions.
- Grant `AddFact`, `LearnResponse`, and `RetireResponse` only to callers that
  may change shared cache content.
- Runtime graph access remains read-only. Graph schema setup remains the
  separate administrative utility in `scripts/setup_schema.py`.

## Verification coverage

Network-level tests cover multi-user conversation isolation, shared facts,
predicates, reports, health, all regulated-cache phases, typed errors,
checkpoint degradation/recovery, restart behavior, client deadlines,
in-flight graceful shutdown, TLS configuration validation, and reproducible
stub generation.
