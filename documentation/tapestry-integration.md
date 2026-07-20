# Tapestry–Engram Regulated Response Cache Integration

## Status and scope

This document defines the integration in which Tapestry treats Engram as a
speculative response cache. Engram proposes a previously stored answer, the
Tapestry Regulator decides whether that answer is acceptable for the current
request, and the Actor handles every rejected proposal or cache miss. A final
Actor answer is learned by Engram unless the Actor reports `IDK`.

The complete process-boundary workflow is implemented by the MCP
`engram_propose`, `engram_resolve`, `engram_learn_response`, and
`engram_retire_response` tools and by the gRPC `Propose`, `Resolve`,
`LearnResponse`, and `RetireResponse` methods. The same operations are
available directly on the transport-neutral `EngramCore` for applications
embedding Engram in process. The lower-level Python API also remains
available. See [mcp-integration.md](mcp-integration.md) and
[grpc-integration.md](grpc-integration.md) for the transport contracts.

## Architecture

```text
User request
    |
    v
Engram proposes a cached response
    |
    v
Tapestry Regulator
    | accepted                         | rejected or no candidate
    v                                  v
Return cached response               Actor
                                       | IDK              | answered
                                       v                  v
                                     Return IDK     Learn response in Engram
                                                          |
                                                          v
                                                   Return Actor response
```

Engram is not authoritative in this design. Its retrieval score ranks
candidates; it does not establish that a candidate is correct. The Regulator
is the acceptance boundary, and the Actor is the source of new cache content.

## Component responsibilities

| Component | Responsibilities |
| --- | --- |
| Tapestry request layer | Assign request identity, user identity, namespace, and relevant context. |
| Engram | Retrieve candidates, expose score and provenance, record accepted hits, cache Actor answers, persist state, and evict weak dynamic entries. |
| Regulator | Evaluate the candidate against the complete current request and return a structured verdict. |
| Actor | Produce the final answer when no candidate is accepted and distinguish an answer from `IDK`. |
| Integration adapter | Orchestrate the lifecycle, update conversation context, apply cache policy, and emit metrics. |

## Request lifecycle

### 1. Establish request identity and scope

Every request should carry:

- `request_id`: unique for one logical attempt and reused on retries;
- `user_id`: the caller-owned, case-sensitive conversation label, defaulting
  to `"0"` when absent;
- `namespace`: the Tapestry application or corpus to which the answer belongs;
- `context_fingerprint`: a stable identifier for any state that can change the
  correct answer;
- Actor, prompt, tool-set, and policy versions used to produce new content.

Conversation context is isolated by `user_id`; learned Actor responses remain
shared. Namespace and context fingerprint are enforced as exact response-cache
scope keys during MCP proposals and replacement. Arbitrary Actor, prompt,
tool-set, source-data, and policy versions can be enforced with
`required_metadata`.

### 2. Ask Engram for a proposal

Call `engram_propose` with the request identity and scope:

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
  "limit": 1
}
```

The result contains:

- `matches`: ranked `(statement, score)` pairs;
- `keywords`: the keywords responsible for candidacy;
- `resolved_query`: the request after conversational reference expansion.

Use `limit=1` when the Regulator evaluates only one candidate. Engram increments
each returned statement's query count, but it does not record a hit or change
the previous-response context. A candidate earns positive credit only after
the Regulator accepts it. Repeating an identical `request_id` returns the same
proposal.

Do not use `pipeline.respond()` for this decision boundary. That function is an
autonomous pattern → cache → LLM pipeline and accepts a sufficiently confident
cache result itself. Do not use `pattern_query()` for a regulated proposal
without a custom adapter either; pattern selection records both candidacy and
success immediately. `engram_propose` excludes patterns for that reason.

### 3. Regulate the proposal

The Regulator should receive at least:

- the original and resolved requests;
- the candidate response;
- statement ID, retrieval score, tier, creation time, and hit/query counts;
- `source_label`, `introduced_by_user_id`, and caller-owned metadata;
- namespace, context, Actor, prompt, and policy versions;
- the current user context required to judge applicability.

A useful verdict is structured rather than Boolean:

```json
{
  "outcome": "accepted",
  "reason": "applicable_and_supported"
}
```

Supported outcomes should include `accepted`, `rejected_quality`,
`rejected_context`, `rejected_stale`, and `rejected_policy`.

Commit the verdict with `engram_resolve`, passing the `proposal_id`, outcome,
candidate `statement_id` when applicable, and a reason. Acceptance records one
hit and updates the user's previous-response context. A rejected proposal
receives no hit. Its query count has already increased, so repeated rejection
naturally lowers its hit rate and eviction protection. Identical resolution
retries are no-ops; conflicting second verdicts fail.

### 4. Invoke the Actor on rejection or miss

The Actor receives the original request and whatever context Tapestry normally
provides. A structured result is preferable to comparing free text:

```json
{
  "status": "answered",
  "text": "The final response.",
  "cacheable": true
}
```

Use `status: "unknown"` for `IDK`. If the current Actor contract returns a
string sentinel, compare its normalized complete value; do not treat a response
that merely contains the letters `IDK` as unknown.

### 5. Learn eligible Actor answers

When the Actor answered and the response is cacheable, call
`engram_learn_response` with the resolved request, final response, same
`request_id`, user and scope, `source_label`, and provenance metadata.

The tool indexes the answer under the request's keywords. If a dynamic,
patternless response already has the same keyword set and exact scope, Engram
replaces it in place and resets its statistics rather than accumulating
duplicates. Other scopes remain independent. Provenance metadata is persisted.

When the core has a configured store, a successful learn, accepted resolution,
or retirement is atomically checkpointed before the call returns. Proposal and
idempotency records remain transient and are intentionally absent after a
process restart.

If a checkpoint fails, Engram raises a transport-neutral `PersistenceError`
and keeps the already-applied mutation in its live single-instance state.
`state_changed` indicates whether this occurred. Tapestry must not interpret
the error as a rollback: retry the exact operation with the same `request_id`
after storage recovers. Engram's idempotency path retries persistence without
learning, retiring, or crediting the item twice. `EngramCore.status()` reports
the intervening state as ready but unhealthy, with degraded durability and
dirty state. If the process exits before a successful checkpoint, the dirty
mutation is lost.

Do not pass an Actor answer through conversational fact extraction merely to
cache it. Response caching and durable fact ingestion are different actions:

- use `learn_from_response()` for an answer associated with a request;
- use `add_fact()` only when Tapestry deliberately promotes a supported fact;
- use `engram_add_fact` for the corresponding explicit MCP operation.

Never cache `IDK`, empty output, transport errors, incomplete streaming output,
or responses the Actor marks non-cacheable.

## In-process orchestration equivalent

The following example shows the existing API boundary. Application-specific
Regulator and Actor result types are abbreviated:

```python
from engram import sessions


def tapestry_response(engram, request, regulator, actor, user_id="0"):
    user_id = sessions.normalize_user_id(user_id)

    def in_scope(statement):
        if statement["pattern"]:
            return False
        metadata = statement.get("template", {}).get("tapestry", {})
        return metadata.get("namespace", "") == "support" and metadata.get("context_fingerprint", "") == ""

    proposal = engram.query(request, user_id=user_id, limit=1, statement_filter=in_scope)
    candidate = proposal["matches"][0] if proposal["matches"] else None

    if candidate:
        statement, score = candidate
        verdict = regulator.evaluate(
            request=request,
            resolved_request=proposal["resolved_query"],
            response=statement["text"],
            score=score,
            statement=statement,
        )
        if verdict.outcome == "accepted":
            engram.record_hit(proposal["keywords"], statement_id=statement["id"])
            sessions.update_session_context(engram, user_id, statement["text"])
            return statement["text"]

    actor_result = actor.respond(request)
    if actor_result.status == "answered":
        if actor_result.cacheable:
            engram.learn_from_response(
                proposal["resolved_query"],
                actor_result.text,
                template={
                    "tapestry": {
                        **actor_result.provenance,
                        "namespace": "support",
                        "context_fingerprint": "",
                    }
                },
                source_label="tapestry:actor",
            )
        sessions.update_session_context(engram, user_id, actor_result.text)
    return actor_result.text
```

The adapter should return its own envelope identifying `engram`, `actor`, or
`idk` as the final source. Do not infer that source later from the response
text.

## Rejection and replacement policy

| Regulator result | Engram action |
| --- | --- |
| Accepted | Record a hit for the accepted statement and keywords. |
| Rejected for quality | Do not record a hit; learn a replacement if the Actor answers. |
| Rejected as stale everywhere | Retire the stale statement, then learn an Actor replacement if available. |
| Rejected for current context | Do not retire a globally valid statement; route to the Actor. |
| Rejected by current policy | Do not record a hit; retire only when the entry is invalid under every supported policy. |
| No candidate | Route directly to the Actor. |
| Actor returns IDK | Return the Actor result and do not write a response cache entry. |

`engram_retire_response` is an explicit destructive decision restricted to
dynamic, patternless cache entries. It should be used only when Tapestry knows
the entry is globally invalid, not merely inapplicable to one request. The
in-process equivalent is `retire_statement(statement_id)`.

## Versioning and invalidation

Actor answers may become stale when any of these change:

- Actor implementation or model;
- system prompt or routing policy;
- enabled tools or external data contract;
- application namespace;
- source data revision;
- Regulator policy.

Store these versions in metadata and pass the currently required values through
`engram_propose.required_metadata`. Mismatched entries are excluded before
scoring and candidacy accounting. Regulation remains mandatory because matching
scope and versions do not prove an answer correct.

## Failure behavior

| Failure | Required behavior |
| --- | --- |
| Engram unavailable or times out | Bypass the cache and call the Actor. |
| Regulator unavailable | Do not return the unregulated candidate; call the Actor. |
| Actor unavailable | Return the Tapestry failure result; do not cache it. |
| Actor answers but Engram learning fails | Return the Actor answer and report the cache-write failure asynchronously. |
| Persistence fails | Keep serving from the live process if permitted, but surface degraded durability. |
| Retry after an uncertain accept/write | Use `request_id` and idempotent integration operations to avoid duplicate credit or writes. |

Cache availability must never be required to obtain an Actor answer. Cache
writes are off the user-response critical path once the final response exists.

## Metrics

At minimum, publish:

- proposal count and proposal latency;
- proposal coverage rate;
- Regulator acceptance rate by statement and rejection reason;
- Actor bypass rate and Actor latency;
- learned, replaced, retired, and failed cache writes;
- `IDK` rate;
- accepted-cache latency versus Actor latency;
- statement hit/query rate and eviction count;
- version- or context-mismatch rate;
- responses later corrected after prior acceptance.

The primary value metric is avoided Actor work subject to an acceptable
Regulator error rate, not raw Engram hit rate.

## Operational rollout

1. Start with one namespace, an explicit context fingerprint, and `limit: 1`.
2. Use structured Actor status and provenance; treat literal `IDK` only as a
   compatibility sentinel.
3. Add required Actor, prompt, source-data, tool-set, and policy versions as
   Tapestry makes them stable.
4. Observe acceptance and rejection counters through `engram_inspect` before
   tuning eviction or candidate limits.
5. Consider provisional pattern matching only if pattern success accounting is
   later separated from selection.

## Acceptance tests

The integration is ready when tests demonstrate:

- an accepted candidate records exactly one hit and bypasses the Actor;
- a rejected candidate records no hit and invokes the Actor;
- an Actor answer is retrieved on the next equivalent request;
- `IDK` is never learned;
- context-specific rejection does not retire globally valid content;
- stale rejection can explicitly retire and replace a statement;
- Alice and Carol retain separate conversation state;
- context-independent Actor answers are shared when intended;
- retries do not double-credit acceptance or duplicate learned entries;
- Engram, Regulator, Actor, and persistence failures follow the fallback table;
- persisted state behaves the same after process restart.
