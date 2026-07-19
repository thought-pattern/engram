# Engram FastMCP Integration

## Status and scope

Engram provides a FastMCP stdio server for agents and LLM hosts that need a
persistent conversational process or a Regulator-controlled response cache.
The ten tools expose one conversation lifecycle, inspection, explicit
shared-fact ingestion, report generation, and a two-phase propose/resolve cache
interface. `engram/mcp_server.py` is a thin adapter over the transport-neutral
`EngramCore` in `engram/service.py`, which is also used by the human CLI.

This document covers:

1. the conversational FastMCP interface; and
2. the implemented Regulator-controlled Tapestry interface defined in
   [tapestry-integration.md](tapestry-integration.md).

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

The MCP host owns process lifetime. `engram_start` creates a conversation
inside the process; it does not start the process. `engram_stop` releases that
conversation; it does not terminate the MCP server.

The MCP adapter deliberately exposes one active `ConversationRuntime` at a
time. `EngramCore` itself can own multiple user runtimes, so a future interface
does not inherit that MCP lifecycle restriction. Run separate MCP processes
when a host needs independently owned concurrent tool lifecycles. User context
is keyed by the caller-owned `user_id`.

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
      "cwd": "/absolute/path/to/engram"
    }
  }
}
```

Or it can use the installed entry point without a repository working directory:

```json
{
  "mcpServers": {
    "engram": {
      "command": "engram-mcp"
    }
  }
}
```

Use an absolute executable path when the MCP host has a restricted or different
`PATH`. A “program not found” startup failure occurs before Engram runs and
must be fixed in the host command configuration.

## Implemented tools

### `engram_start`

Starts one persistent conversation. Starting a second conversation before
`engram_stop` returns an MCP tool error.

| Argument | Default | Meaning |
| --- | --- | --- |
| `user_id` | `"0"` | Arbitrary caller-owned, case-sensitive user label. Empty values normalize to `"0"`. |
| `initial_bot_text` | `""` | Engram utterance immediately before the first user turn; establishes `<that>`-style context. |
| `seed_path` | `data/seed.json` | Seed corpus synchronized into the instance. Pass an empty string to skip seeding. |
| `store_path` | `""` | Optional persistent Engram JSON store. An existing store is loaded; a missing path is created when saved. |
| `config_path` | `""` | Optional YAML configuration path. |
| `transcript_path` | `""` | Optional JSON recovery transcript updated after every turn. |
| `random_seed` | `null` | Optional deterministic per-turn random seed for reproducible testing. |

The result includes `started`, normalized `user_id`, `initial_bot_text`,
`turn_count`, `statement_count`, and the resolved `store_path`.

### `engram_send`

Sends exactly one non-empty user message after the caller has observed the
previous Engram response. There is deliberately no batch conversation tool.

The returned turn event includes:

- turn number, input, response, and user ID;
- source, score, matched pattern, and wildcard captures;
- dialogue act, active topic, and canonical entities;
- conversational fact-admission decisions;
- elapsed time;
- previous-response and predicate changes;
- statements learned during that turn.

The call mutates conversation state and may learn conversational facts according
to Engram configuration. It is a chatbot operation, not a read-only cache
proposal.

### `engram_inspect`

Returns the active state without advancing the conversation:

- user ID, initial bot text, and turn count;
- session predicates, histories, active topic, entities, and fact diagnostics;
- current Engram metrics;
- dynamic statements and their provenance;
- unique learned texts and the latest turn;
- regulated-cache proposal, miss, acceptance, rejection, learning,
  retirement, and idempotent-retry counters.

### `engram_add_fact`

Adds one shared, unattributed fact without modifying user conversation context.

| Argument | Default | Meaning |
| --- | --- | --- |
| `text` | required | One non-empty fact string. |
| `source_label` | `""` | Opaque caller-owned provenance label. |

Use this for deliberate research or tool facts, not for caching an Actor answer
to a request. The result is a statement view containing ID, text, patterns,
attribution, and source label.

### `engram_finish`

Writes complete JSON and Markdown reports without ending the conversation.

| Argument | Default | Meaning |
| --- | --- | --- |
| `output_prefix` | `engram-mcp-transcript` | Path prefix; `.json` and `.md` are added. |

If `store_path` was configured, the Engram store is also saved. The result
contains the report summary and both resolved paths. Continue calling
`engram_send` after `engram_finish` when more turns are required.

### `engram_stop`

Saves the configured store, returns the final user ID and summary, and releases
the active `ConversationRuntime`. It does not write conversation reports; call
`engram_finish` first when reports are required.

All other tools require an active conversation established by `engram_start`.

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
the response from the previous `engram_send`; precomputing a batch would hide
that dependency and would not constitute an observed conversation.

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
Graph credentials are never persisted.

If the host process exits without `engram_stop`, the per-turn transcript can
still describe the observed conversation, but only a previously completed
store save is durable. Use `engram_finish` or `engram_stop` at controlled
boundaries.

## Security and authority boundaries

- The server uses local stdio transport; remote access, authentication, and
  process isolation belong to the MCP host.
- Treat `store_path`, `config_path`, `seed_path`, transcript paths, and report
  prefixes as trusted deployment configuration rather than arbitrary end-user
  input.
- `source_label` is opaque provenance, not an authorization decision.
- `engram_add_fact` is an explicit write to shared Engram knowledge and should
  be granted only to callers authorized to add facts.
- Runtime graph operations are read-only. Graph schema setup remains a
  separate administrative utility.
- Conversation reports can contain user inputs, responses, learned facts, and
  context; protect them as application data.

## Regulated-cache tools

Do not use `engram_send` as a Tapestry cache proposal. It returns a completed
chatbot response and performs normal chatbot state changes. The four tools in
this section provide the separate speculative proposal and explicit Regulator
decision boundary.

### `engram_propose`

Retrieves candidates without recording success or updating the displayed
response context. It uses keyword retrieval only; scripted pattern responses
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
      "introduced_by_user_id": null,
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

Returned candidates receive one candidacy/query count and no hit. The server
retains their statement IDs, response snapshots, and keywords, so a client
cannot credit an unrelated or subsequently replaced response. Reusing a
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
that user's previous-response context. Rejection records no hit. Retrying the
same verdict returns `idempotent: true` without double credit; a conflicting
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
`replaced`), scope, provenance, and `idempotent`. Responses replace in place
only when their query keyword set, namespace, and context fingerprint all
match. Actor responses remain shared knowledge (`introduced_by_user_id` is
null), while the calling user's previous-response context is updated.
`request_id` makes retries idempotent and conflicting reuse is an error.

### `engram_retire_response`

Explicitly retires a response that the Regulator has determined is globally
stale. Context mismatch alone must not invoke this tool.

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
and are never serialized. Learned responses, their scope/provenance metadata,
and accepted hit statistics are part of the normal Engram store and become
durable at `engram_finish` or `engram_stop` when `store_path` is configured.

Service calls are serialized by the conversation service lock. Concurrent
identical resolutions therefore record exactly one accepted hit.

## Failure handling

| Condition | Client behavior |
| --- | --- |
| MCP server unavailable | Bypass Engram and invoke the Actor. |
| `engram_start` fails | Do not send turns; fix configuration or bypass Engram. |
| Conversation tool times out | Treat the turn result as unknown and inspect before retrying a mutating call. |
| `engram_finish` fails | Keep the conversation active and retry with a valid writable output prefix. |
| `engram_stop` fails | Treat store durability as unknown and surface the failure. |
| Regulated-cache call fails | Actor response remains available; cache failure must not block the user response. |
| Regulator unavailable | Never return an unregulated proposal; invoke the Actor. |

## Verification checklist

- The host can list all ten tools after startup.
- `engram_start` followed by `engram_send` preserves one runtime across calls.
- A second `engram_start` fails until `engram_stop`.
- `user_id` defaults to `"0"` and preserves explicit case.
- `engram_inspect` does not advance the turn count.
- `engram_add_fact` changes shared knowledge but not user context.
- `engram_finish` writes both reports and allows another send.
- `engram_stop` persists the configured store and releases the runtime.
- Restarting the MCP process with the same store restores durable state.
- A proposal records candidacy but earns a hit only after acceptance.
- Scope and required metadata prevent incompatible responses from becoming candidates.
- Proposal, learn, resolve, and retirement retries are idempotent; conflicting retries fail.
- Transient proposals expire and are not restored, while learned responses are restored.
- Adaptive conversation tests observe every response before sending the next
  input.
- The regulated-cache tools satisfy the acceptance tests in
  [tapestry-integration.md](tapestry-integration.md).
