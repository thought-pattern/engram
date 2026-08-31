# Engram gRPC Integration

## Status and scope

Engram provides the unchanged proposal/conversation API in `engram.v1` and a
separate unified-resolution evidence API in `engram.v2`. The packaged
`engram-grpc` process creates exactly one `EngramCore`, exposes both services
through thin protobuf adapters, publishes the standard gRPC health service,
and closes the core during graceful shutdown.

Deploy one server process with one JSON store.

The v1 contract carries proposal and conversation data. The v2
`ResolveEvidence` result carries the bounded Proposition evidence package. When
graph recall is enabled, Engram reads the configured Memgraph instance directly.
Its corrected read subset understands Tapestry's half-open Proposition times and
excludes inactive or retrieval-ineligible Propositions. Tapestry deployments use
the shared root Memgraph schema; standalone deployments use Engram's exact
independently installable subset.

The projection carries canonical subject, predicate, and object identity plus
the Proposition lifecycle, scope, ownership, and support-derived trust inputs.
Serving uses read-only graph access. Startup verifies the selected deployment
mode and compatible schema before graph recall becomes ready.

## Vector-search boundary

When graph vector recall is enabled, `Propose` embeds the request with the
configured local sentence-transformer and queries Memgraph's Proposition
vector index. ANN results are intersected with the Proposition identifiers in
each response's ordered `tapestry-engram-support-v1` mappings. Vector similarity therefore
helps find a previously verified response whose durable support is semantically
related to the request. Proposals remain scoped to stored responses. Keyword
retrieval remains active and the two scores are merged before candidate selection.

Engram's general read-only Cypher guard still rejects `CALL`. The sole exception
is an internal, fixed `vector_search.search` query with server-owned Cypher and
index configuration, validated identifiers, bounded results, and active
canonical Propositions. The same ANN lookup is available as a fallback for conversational
graph recall after exact canonical label, alias, and keyword lookup misses.

The standalone server enables graph access through `--config-path`. With vectors
enabled it loads and probes the local embedding model and
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

Store, seed, transcript/report locations, binding, and TLS come from server
configuration. Stable SHA-256-derived transcript and report filenames keep
caller-owned user labels within their configured directories.

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

Concurrent RPC handlers delegate to the same core. The core serializes state
transitions and owns chat, retrieval, learning, idempotency, and persistence.

The v2 evidence adapter also connects the gRPC call lifecycle to
`EngramCore.resolve_request`'s cooperative cancellation callback. A cancellation
or expired deadline at a cooperative boundary discards the transient resolution,
permitting retry with the same request ID.

## Protocol and generated code

The source contracts are [v1/engram.proto](../engram/v1/engram.proto) and
[v2/engram.proto](../engram/v2/engram.proto). Generated Python messages, type
stubs, and service stubs are committed beside each source. RPC requests have
explicit fields and v1 `Resolve` uses the `RegulatorOutcome` enum. Engram
conversation, inspection, and cache results use
`google.protobuf.Struct` because those JSON-ready diagnostic payloads are
extensible application data.

The v2 `ResolutionResult` explicitly versions and names every top-level unified
result field. Its `EvidencePackage` explicitly carries wire version, records,
retained/omitted counts, truncation state, and reasons. Complex candidate,
evidence, diagnostic, resolver, and budget records remain bounded core-owned
structures carried through `Struct` fields.

Install the development dependencies and regenerate after changing the proto:

```bash
python -m pip install -e ".[dev]"
python -m grpc_tools.protoc -I. --python_out=. --pyi_out=. \
  --grpc_python_out=. engram/v1/engram.proto
python -m grpc_tools.protoc -I. --python_out=. --pyi_out=. \
  --grpc_python_out=. engram/v2/engram.proto
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
| `FinishConversation` | Flush and write JSON/Markdown reports while the user runtime remains active. |
| `StopConversation` | Flush and release one user runtime while the server remains active. |
| `AddFact` | Add a shared, unattributed fact with an opaque source label. |
| `SetPredicate`, `GetPredicate` | Write/read a caller-owned value in one user context. |
| `Propose` | Retrieve scoped response-cache candidates and record candidacy. |
| `Resolve` | Commit one typed Regulator verdict with idempotent accepted credit. |
| `LearnResponse` | Cache a non-`IDK` Actor answer with scope and metadata. |
| `RetireResponse` | Retire one dynamic, patternless cached answer. |
| `GetStatus` | Return lifecycle, readiness, durability, checkpoint, and conversation status. |
| `Flush` | Explicitly checkpoint the configured store. |

The separate `engram.v2.EngramEvidenceService` has one RPC:

| RPC | Purpose |
| --- | --- |
| `ResolveEvidence` | Run `EngramCore.resolve_request` and return the versioned ANSWER, EVIDENCE, or MISS result, including the bounded Proposition package when available. |

`Propose`, `Resolve`, `LearnResponse`, and `RetireResponse` implement the same
Tapestry contract documented in the
[Tapestry–Engram integration guide](https://github.com/thought-pattern/tapestry/blob/develop/project/design/engram-integration.md).
These RPCs operate at service scope outside the chatbot lifecycle.

## Python client example

```python
import grpc
from google.protobuf.json_format import MessageToDict

from engram.v1 import engram_pb2, engram_pb2_grpc
from engram.v2 import engram_pb2 as evidence_pb2
from engram.v2 import engram_pb2_grpc as evidence_pb2_grpc

with grpc.insecure_channel("127.0.0.1:50051") as channel:
    stub = engram_pb2_grpc.EngramServiceStub(channel)
    stub.StartConversation(engram_pb2.StartConversationRequest(user_id="Alice"))
    turn = stub.Chat(
        engram_pb2.ChatRequest(user_id="Alice", text="Hello, Engram."),
        timeout=5,
    )
    print(MessageToDict(turn, preserving_proto_field_name=True)["response"])

    evidence_stub = evidence_pb2_grpc.EngramEvidenceServiceStub(channel)
    result = evidence_stub.ResolveEvidence(
        evidence_pb2.ResolveEvidenceRequest(
            request="What evidence is available?",
            request_id="resolve-1",
            user_id="Alice",
        ),
        timeout=5,
    )
    print(result.outcome, result.evidence_package.retained_count)
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
already applied to live memory before its checkpoint failed. Client details are
the generic `Engram persistence failure`; detailed exceptions remain in server logs.

## Health and readiness

The server registers `grpc.health.v1.Health` for the aggregate empty service
name, `engram.v1.EngramService`, and `engram.v2.EngramEvidenceService`. It
reports `SERVING` only while the core is both ready and healthy. A degraded
store, closing core, closed core, or graceful shutdown reports `NOT_SERVING`.

`GetStatus` provides the detailed source data: `state`, `ready`, `healthy`,
`durability`, `dirty`, `last_checkpoint_at`, a redacted exception-class
`last_persistence_error`, `active_conversations`, `store_path`, and a bounded
`telemetry` aggregate with fixed outcome, resolver, latency, resource, rebuild,
durability, and Regulator keys. A bounded `components` object has
`enabled` and `ready` Booleans for graph, vector, and spaCy. The component
status contains fixed component identifiers.

## Persistence, retry, and deadlines

With a configured store, successful durable mutations synchronously use the
core's atomic checkpoint path. Proposals and idempotency records remain
bounded, five-minute, process-local state.

A checkpoint error leaves the in-memory mutation applied. While degraded, the
core remains ready and standard health becomes `NOT_SERVING`. Correct the
store and either call `Flush` or repeat the exact regulated-cache operation
with the same `request_id`; its idempotency path retries persistence with one
application and credit.

A v1 handler may complete after a client deadline or cancellation. V2 resolution
checks cooperative cancellation around graph calls; an executing driver call
continues until the driver returns. Published work remains applied. Inspect user
context before repeating an ambiguous chat turn. Regulated-cache calls retain and reuse their logical
`request_id`. Recovery and ambiguous-outcome handling are described in the
[deployment and rollback runbook](operations/deployment-and-rollback-v1.md).

After restart, learned responses, facts, user contexts, statistics, and
retirements come from the last completed checkpoint. Outstanding proposal IDs
expire; the Actor handles unresolved requests again.

## Graceful shutdown

SIGINT and SIGTERM request shutdown. The process marks health not-serving,
stops accepting new RPCs, allows in-flight calls to complete within
`--grace-period`, then calls `EngramCore.close()` for the final checkpoint.
Repeated server shutdown is idempotent.

If the final checkpoint fails, the process logs the persistence failure and
exits unsuccessfully. The core's `PersistenceError` still distinguishes
whether live state differed from the last durable checkpoint. An application
embedding `EngramGrpcServer` may correct the store and call `stop()` again to
retry the final checkpoint. The transport remains stopped.

## Security and authority boundaries

- Bind to loopback unless remote clients are explicitly required.
- Use TLS or a trusted encrypted proxy for remote transport.
- Protect the store, transcripts, and reports as application data. Engram
  creates service-owned artifact directories with owner-only permissions
  (`0700`) and writes stores, transcripts, and reports as owner-only files
  (`0600`).
- `source_label` and metadata record provenance; deployment policy supplies authorization.
- Grant `AddFact`, `LearnResponse`, and `RetireResponse` only to callers that
  may change shared cache content.
- Runtime graph access remains read-only. Standalone Graph schema setup is the
  separate explicit `scripts/setup_schema.py --apply` administration path.
  A Tapestry-managed graph receives no Engram DDL and is inspected with
  `scripts/verify_schema.py --deployment tapestry_managed`.

## Verification coverage

Network-level tests cover multi-user conversation isolation, shared facts,
predicates, reports, health, all regulated-cache phases, typed errors,
checkpoint degradation/recovery, restart behavior, client deadlines,
in-flight graceful shutdown, TLS configuration validation, and reproducible
stub generation.
