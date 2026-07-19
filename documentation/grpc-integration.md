# Engram gRPC Integration Plan

## Status

The transport-neutral preparation is complete. No gRPC package, protobuf,
generated stub, server, or gRPC test has been added yet.

Engram will run as one process containing exactly one shared `EngramCore`.
There is no replication, load balancing, shared transaction service,
distributed locking, or cross-instance consistency protocol.

## Work boundary

### Completed non-gRPC work

- `EngramCore` owns the shared `Engram`, user conversations, persistence, and
  regulated-cache lifecycle.
- MCP and CLI behavior delegates to that core through thin adapters.
- One core can isolate multiple `user_id` conversation contexts while sharing
  learned knowledge.
- Regulated-cache operations do not require an active chatbot conversation.
- A configured store is written atomically after successful durable mutations.
- `close()` and the context-manager exit perform a final flush.
- Proposals and idempotency records remain bounded, expiring, process-local
  state and are never serialized.
- Restart and adapter-parity behavior is covered by the Python test suite.

### Remaining gRPC work

1. Define the protobuf package, messages, enums, and service.
2. Add `grpcio` as a runtime dependency and `grpcio-tools` as a development
   dependency.
3. Generate and commit the Python protobuf and gRPC stubs.
4. Implement a thin gRPC adapter over one `EngramCore` instance.
5. Map core validation and lifecycle failures to gRPC status codes.
6. Add the standard gRPC health service.
7. Wire process signals to graceful server shutdown and `EngramCore.close()`.
8. Add in-process protocol, restart, deadline, and shutdown tests.
9. Document launch configuration and provide a packaged server entry point.

## Single-instance process model

```text
gRPC clients
     |
     v
one gRPC server process
     |
     v
one EngramCore
     |
     +-- shared Engram knowledge and response cache
     +-- isolated user conversation contexts
     +-- in-memory proposal/idempotency records
     +-- one configured JSON store
```

The server creates `EngramCore` once during process startup. Store, seed,
configuration, transcript/report location, bind address, and transport security
are server configuration. They are not supplied as ordinary request fields.

## Proposed unary RPC surface

- `StartConversation`
- `Chat`
- `InspectConversation`
- `StopConversation`
- `AddFact`
- `Propose`
- `Resolve`
- `LearnResponse`
- `RetireResponse`
- optional administrative `Flush`

The existing MCP contracts define the response-cache field semantics. The
protobuf should use explicit enums for Regulator outcomes and explicit fields
for `request_id`, `proposal_id`, `user_id`, namespace, context fingerprint,
source label, and caller metadata.

## Durability model

When `store_path` is configured, `EngramCore` performs a synchronous atomic
checkpoint after successful operations that change durable state:

- starting a conversation;
- completing a chat turn;
- adding a fact or setting a predicate;
- proposing a candidate, because retrieval changes query statistics;
- accepting a proposal, because it changes hit statistics and user context;
- learning or retiring a response.

Rejected verdicts, proposal records, idempotency records, and regulated-cache
counters are transient. They do not require a store write. `finish`, `stop`,
`flush`, and `close` remain explicit persistence boundaries, and graceful gRPC
shutdown must call `close()`.

`checkpoint_on_mutation=False` exists for controlled batch/embedded use. The
gRPC server should leave it enabled.

## Restart behavior

After a clean or unclean process restart:

- learned responses, facts, user contexts, query/hit statistics, and retired
  state come from the last completed atomic checkpoint;
- outstanding proposal IDs are unknown and must be treated as expired;
- a caller with an unresolved proposal should invoke the Actor again rather
  than assume the cached response was accepted.

## Initial acceptance criteria

- Exactly one `EngramCore` is created by the server process.
- Every RPC delegates to that instance; handlers contain no duplicate Engram
  behavior.
- Alice and Carol retain isolated contexts and shared intended knowledge.
- Accepted proposals record one hit under concurrent identical retries.
- Learned and retired state survives process restart without an explicit Flush
  RPC.
- Transient proposals do not survive restart.
- Health changes to not-serving before graceful shutdown begins.
- Shutdown stops new calls, completes or cancels in-flight work within its
  grace period, closes the core, and exits.
