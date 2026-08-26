# Engram MCPServer Integration

## Status and scope

Engram provides an MCPServer stdio server for agents and LLM hosts that need a
persistent conversational process or a Regulator-controlled response cache.
The ten tools expose one conversation lifecycle, inspection, explicit
shared-fact ingestion, report generation, and a two-phase propose/resolve cache
interface. `engram/mcp_server.py` is a thin adapter over the transport-neutral
`EngramCore` in `engram/service.py`, which is also used by the human CLI.
The interface compatibility contract freezes the ten names, input schemas,
defaults, and descriptions. Output schemas remain unspecified. The gRPC v2
service owns unified Claim evidence.

MCP tool calls remain synchronous at this boundary. If a client abandons its
wait, already-started mutation work may complete; retry the same request ID to
recover the receipt outcome. Shared retry, durability, and shutdown behavior is
described in the
[deployment and rollback runbook](operations/deployment-and-rollback-v1.md).

This document covers:

1. the conversational MCP interface; and
2. the implemented Regulator-controlled Tapestry interface defined in the
   [Tapestry–Engram integration guide](https://github.com/thought-pattern/tapestry/blob/develop/project/design/engram-integration.md).

## Process model

```text
MCP host
    |
    | starts and owns one stdio process
    v
engram-mcp / python -m engram.mcp_server
    |
    v
MCPConversationService
    |
    v
EngramCore
    |
    v
one MCP-active ConversationRuntime over a shared Engram instance
```

The MCP host starts and stops the process. `engram_start` creates a conversation;
`engram_stop` releases it.

The MCP adapter exposes one active `ConversationRuntime` at a time.
`EngramCore` can own multiple user runtimes, and the gRPC interface
uses that capability for concurrent user contexts. Run separate MCP processes
for concurrent tool lifecycles. User context is keyed by the caller-owned
`user_id`.

## Installation and launch

From the repository:

```bash
python -m pip install -e .
python -m engram.mcp_server
```

After installation, the console entry point is equivalent:

```bash
engram-mcp
```

A host configuration can launch the module from a checkout:

```json
{
  "mcpServers": {
    "engram": {
      "command": "python",
      "args": ["-m", "engram.mcp_server"],
      "cwd": "."
    }
  }
}
```

## Implemented tools

### `engram_start`

Starts one persistent conversation. Starting a second conversation before
`engram_stop` returns an MCP tool error.

Startup runs the same transport-neutral component preflight as Python and gRPC. Enabled graph access must connect, enabled vector recall must load its local model and probe its configured index, and enabled spaCy-backed behavior must load the pre-provisioned English model before the tool reports success.

| Argument | Default | Meaning |
| --- | --- | --- |
| `user_id` | `"0"` | Arbitrary caller-owned, case-sensitive user label. Empty values normalize to `"0"`. |
| `initial_bot_text` | `""` | Engram utterance immediately before the first user turn; establishes `<that>`-style context. |
| `seed_path` | `data/seed.json` | Seed corpus synchronized into the instance. Pass an empty string to skip seeding. |
| `store_path` | `""` | Optional persistent Engram JSON store. An existing store is loaded; a missing path is created when saved. |
| `config_path` | `""` | Optional YAML configuration path. |
| `transcript_path` | `""` | Optional JSON recovery transcript updated after every turn. |
| `random_seed` | `0` | Deterministic per-turn random seed value. Nonzero values enable seeding automatically. |
| `random_seed_present` | `false` | Explicitly enables the seed, preserving seed `0` as a meaningful value. |

The result includes `started`, normalized `user_id`, `initial_bot_text`,
`turn_count`, `statement_count`, and the resolved `store_path`.

### `engram_send`

Sends exactly one non-empty user message after the caller has observed the
previous Engram response. Each `engram_send` submits one message.

The returned turn event includes:

- turn number, input, response, and user ID;
- source, score, matched pattern, and wildcard captures;
- dialogue act, active topic, and canonical entities;
- conversational fact-admission decisions;
- elapsed time;
- previous-response and predicate changes;
- statements learned during that turn.

The call mutates conversation state and may learn conversational facts according
to Engram configuration. Cache proposals use `engram_propose`.

### `engram_inspect`

Returns the active state and preserves the turn count:

- user ID, initial bot text, and turn count;
- session predicates, histories, active topic, entities, and fact diagnostics;
- current Engram metrics;
- dynamic statements and their provenance;
- unique learned texts and the latest turn;
- regulated-cache proposal, miss, acceptance, rejection, learning,
  retirement, and idempotent-retry counters.

### `engram_add_fact`

Adds one shared, unattributed fact and preserves user conversation context.

| Argument | Default | Meaning |
| --- | --- | --- |
| `text` | required | One non-empty fact string. |
| `source_label` | `""` | Opaque caller-owned provenance label. |

Use this for research or tool facts. Cache Actor answers with
`engram_learn_response`. The result contains ID, text, patterns, attribution,
and source label.

### `engram_finish`

Writes complete JSON and Markdown reports and keeps the conversation active.

| Argument | Default | Meaning |
| --- | --- | --- |
| `output_prefix` | `engram-mcp-transcript` | Path prefix; `.json` and `.md` are added. |

If `store_path` was configured, the Engram store is also saved. The result
contains the report summary and both resolved paths. Continue calling
`engram_send` after `engram_finish` when more turns are required.

### `engram_stop`

Saves the configured store, returns the final user ID and summary, and releases
the active `ConversationRuntime`. Call `engram_finish` first to write reports.

All other tools require an active conversation established by `engram_start`.

`engram_inspect` includes `core_status`, the transport-neutral lifecycle and
durability snapshot. Its fields include `state`, `ready`, `healthy`,
`durability`, `dirty`, the last checkpoint/error-class information, and the number
of active conversations. `telemetry` is the shared fixed-cardinality process
aggregate for outcome, resolver contribution/state, latency, budget/resource,
rebuild, durability, and fixed Regulator outcomes. The bounded `components`
object reports `enabled` and `ready` for graph, vector, and spaCy.

## Required lifecycle

```text
1. Host starts the MCP process.
2. Client calls engram_start once.
3. Client uses either `engram_send` for chatbot turns or the regulated-cache
   sequence described below.
4. Client uses `engram_inspect` or `engram_add_fact` when needed.
5. Client calls engram_finish when reports are required.
6. Client calls engram_stop to persist and release the conversation.
7. Host eventually terminates the MCP process.
```

Example call sequence, shown as tool names and JSON arguments:

```text
engram_start {
  "user_id": "Robin",
  "initial_bot_text": ".",
  "store_path": "state/engram.json",
  "random_seed": 42
}

engram_send {"text": "Hello, Engram."}
engram_inspect {}
engram_add_fact {
  "text": "Tokyo is the capital of Japan.",
  "source_label": "research-tool"
}
engram_finish {"output_prefix": "reports/robin-conversation"}
engram_stop {}
```

Tool calls are sequential in one conversation. The next input may depend on
the response from the previous `engram_send`; each observed response determines
the next input.

## Persistence and recovery

`EngramCore` owns persistence; `store_path` and `transcript_path` serve
different purposes:

- `store_path` persists the complete Engram store, indexes, statistics,
  sessions, and non-secret configuration across MCP process restarts;
- `transcript_path` is a recovery log of the current conversation and is
  atomically updated after each turn;
- `engram_finish` creates analysis reports but leaves the runtime active.

When an existing store is loaded and `seed_path` is non-empty, the configured
seed is synchronized into it. Dynamic learned content survives that refresh.
Graph credentials remain in runtime configuration.

Successful durable mutations are atomically checkpointed when `store_path` is
configured. Core validation, not-found, conflict, lifecycle, and persistence
failures are surfaced by MCPServer as tool errors. If a checkpoint fails after a
mutation, the tool call fails but the mutation remains applied in the live MCP
process; `core_status` reports `durability: "degraded"` and `dirty: true`.

The regulated-cache tools support safe recovery: retry the exact request with
the same `request_id`, or reach an explicit `flush`/lifecycle boundary after
the store becomes available. The idempotency path checkpoints again while
preserving one learning, retirement, or credit operation. An abrupt host exit before
recovery loses that uncheckpointed state, as well as all transient proposals.
The per-turn transcript separately describes observed chatbot turns.
`engram_finish` and `engram_stop` remain explicit report and lifecycle
boundaries.

## Security and authority boundaries

- The server uses local stdio transport; remote access, authentication, and
  process isolation belong to the MCP host.
- Treat `store_path`, `config_path`, `seed_path`, transcript paths, and report
  prefixes as trusted deployment configuration.
- `source_label` records opaque provenance; MCP host policy supplies authorization.
- `engram_add_fact` is an explicit write to shared Engram knowledge and should
  be granted only to callers authorized to add facts.
- Runtime graph operations are read-only. Graph schema setup remains a
  separate administrative utility.
- Conversation reports can contain user inputs, responses, learned facts, and
  context; protect them as application data.

## Regulated-cache tools

`engram_send` returns a completed chatbot response and performs normal chatbot
state changes. The four tools below provide speculative proposal and explicit
Regulator decision handling.

### `engram_propose`

Retrieves candidates, records candidacy, and preserves displayed response
context. It uses keyword retrieval only; scripted pattern responses
are excluded because pattern selection has autonomous success accounting.

Input:

```json
{
  "request": "What are the support hours?",
  "request_id": "req-123",
  "user_id": "Robin",
  "namespace": "support",
  "context_fingerprint": "account-tier:pro",
  "required_metadata": {
    "actor_version": "actor-7",
    "policy_version": "regulator-4"
  },
  "required_source_label": "tapestry:actor",
  "limit": 1
}
```

`namespace` and `context_fingerprint` are exact-match scope keys. Empty values
match only entries carrying empty scope values. `required_metadata` applies
exact top-level matches to the stored `tapestry` metadata, and
`required_source_label` optionally filters provenance. This permits Actor,
prompt, source-data, tool-set, and policy version isolation before regulation.

Output:

```json
{
  "proposal_id": "proposal-456",
  "request_id": "req-123",
  "user_id": "Robin",
  "namespace": "support",
  "context_fingerprint": "account-tier:pro",
  "resolved_request": "What are the support hours?",
  "keywords": ["support", "hours"],
  "candidates": [
    {
      "statement_id": "stmt_abc",
      "response": "Support is available from 09:00 to 17:00 UTC.",
      "score": 0.91,
      "tier": "DYNAMIC",
      "created_at": "2026-07-19T12:00:00+00:00",
      "hit_count": 3,
      "query_count": 5,
      "source_label": "tapestry:actor",
      "introduced_by_user_id": "",
      "metadata": {
        "tapestry": {
          "namespace": "support",
          "context_fingerprint": "account-tier:pro"
        }
      }
    }
  ],
  "idempotent": false
}
```

Returned candidates receive one candidacy/query count; hit counts stay unchanged.
The server binds statement IDs, response snapshots, and keywords to the
proposal. Reusing a
`request_id` with identical arguments returns the same proposal; conflicting
reuse is an error.

### `engram_resolve`

Commits the Regulator verdict exactly once. Supported outcomes are `accepted`,
`rejected_quality`, `rejected_context`, `rejected_stale`, and
`rejected_policy`.

```json
{
  "proposal_id": "proposal-456",
  "outcome": "accepted",
  "statement_id": "stmt_abc",
  "reason": "applicable_and_supported"
}
```

`accepted` requires a candidate `statement_id`, records one hit, and updates
that user's previous-response context. Rejection leaves hit counts unchanged.
Retrying the same verdict returns `idempotent: true` with one credit; a conflicting
second verdict is an error. Rejection counts by outcome are available through
`engram_inspect`.

### `engram_learn_response`

Caches a completed Actor answer. Empty output and a normalized complete value
of `IDK` are rejected.

```json
{
  "request": "What are the support hours?",
  "response": "Support is available from 09:00 to 17:00 UTC.",
  "request_id": "req-123",
  "user_id": "Robin",
  "namespace": "support",
  "context_fingerprint": "account-tier:pro",
  "source_label": "tapestry:actor",
  "metadata": {
    "actor_version": "actor-7",
    "prompt_version": "support-12",
    "policy_version": "regulator-4"
  }
}
```

The result returns `learned`, `statement_id`, `action` (`created` or
`rejected_capacity`), scope, provenance, and `idempotent`. The compatibility
operation preserves existing knowledge identity. A canonical or alias collision
in the same exact scope names the existing owner and is rejected; the
transport-neutral supersession operation handles replacement. Actor responses remain shared knowledge
(`introduced_by_user_id` is `""`), while the calling user's previous-response
context is updated. `request_id` makes retries idempotent and conflicting reuse
is an error.

### `engram_retire_response`

Explicitly retires a response that the Regulator has determined is globally
stale. Use `rejected_context` for a context mismatch.

```json
{
  "statement_id": "stmt_abc",
  "reason": "superseded_source_data",
  "request_id": "req-123"
}
```

Only dynamic, patternless entries can be retired through this tool. The result
is retry-safe by `request_id`; conflicting request reuse is an error.

### Transient state and persistence

Proposals and idempotency records are process-local, retained for five minutes,
and bounded to 1,000 records of each kind. They are cleared by `engram_stop`
and process exit. Learned responses, their scope/provenance metadata,
query statistics, accepted hit statistics, retirements, and user context are
checkpointed to the normal Engram store after their successful mutating call
when `store_path` is configured.

Service calls are serialized by the conversation service lock. Concurrent
identical resolutions therefore record exactly one accepted hit.

## Failure handling

| Condition | Client behavior |
| --- | --- |
| MCP server unavailable | Bypass Engram and invoke the Actor. |
| `engram_start` fails | Fix configuration or bypass Engram before sending turns. |
| Conversation tool times out | Treat the turn result as unknown and inspect before retrying a mutating call. |
| `engram_finish` fails | Keep the conversation active and retry with a valid writable output prefix. |
| `engram_stop` fails | Treat store durability as unknown and surface the failure. |
| Regulated-cache call fails | Return the Actor response. |
| Regulator unavailable | Invoke the Actor. |

## Verification checklist

- The host can list all ten tools after startup.
- `engram_start` followed by `engram_send` preserves one runtime across calls.
- A second `engram_start` fails until `engram_stop`.
- `user_id` defaults to `"0"` and preserves explicit case.
- `engram_inspect` preserves the turn count.
- `engram_add_fact` changes shared knowledge and preserves user context.
- `engram_finish` writes both reports and allows another send.
- `engram_stop` persists the configured store and releases the runtime.
- Restarting the MCP process with the same store restores durable state.
- A proposal records candidacy but earns a hit only after acceptance.
- Scope and required metadata prevent incompatible responses from becoming candidates.
- Proposal, learn, resolve, and retirement retries are idempotent; conflicting retries fail.
- Transient proposals expire; learned responses restore from the store.
- Adaptive conversation tests observe every response before sending the next
  input.
- The regulated-cache tools satisfy the acceptance tests in the
  [Tapestry–Engram integration guide](https://github.com/thought-pattern/tapestry/blob/develop/project/design/engram-integration.md).
