# Engram Python API

**Status:** Current transport-neutral mapping contract
**Owner:** Engram project

## Boundary

External Regulator outcome names below belong to Engram's standalone API, not
Tapestry's component ownership. Tapestry Will owns Channel input interpretation
under `will.yml`, routing and recall sufficiency; Regulator only reviews outbound
Channel soundness and alone generates IDK. Utility serves inference wherever it
is needed and sufficient. Tapestry's only selections are `model.actor` and `model.regulator`,
with Will/Utility/Regulator sharing the latter; this does not alter Engram's API.

`EngramCore.resolve_request` is the Python entry point for unified
resolution. It accepts concrete Python values and returns one validated
dictionary. Callers read dictionaries with `.get(key, concrete_default)`.
Candidate, evidence-reference and result codecs belong to `engram.resolution`;
frame and provenance codecs to `engram.resolution_frame`; resource limits and
consumption to `engram.resolution_budget`; full-Proposition evidence to
`engram.resolution_evidence`; and bounded evidence collections to
`engram.resolution_package`. Shared native-value validation and explicit public
JSON codecs belong to `engram.resolution_values`. Python handoffs retain native
values; use a codec only at its documented external interchange boundary.

Lower-level `Engram` statement, pattern, graph, and conversation operations are
current first-party interfaces. MCP and the gRPC services are
separate `EngramCore` adapters.

Conversation methods use canonical unknown user `"0"` for omitted or empty
labels. Unknown conversations initialize fresh session state and delete it on
stop; finish remains report-only. A failed initialization restores the prior
session before propagating its error. Named identities retain their exact
nonblank labels and reusable context; shared facts and accepted responses are
not deleted by conversation teardown.
Starting `"0"` returns a `conversation_token`; `chat`, `inspect_conversation`,
`finish_conversation`, and `stop_conversation` for `"0"` must pass it as
`conversation_token` or raise `ConversationOwnershipError`. An unknown-user
conversation idle for 300 seconds no longer blocks a new start.
Active conversations are bounded by `max_sessions`, since each owns one session.
A start first releases conversations whose session was evicted or expired. At
capacity, `session_overflow` applies to conversations as it does to sessions:
`reject` raises `SessionLimitExceededError`, `expire_oldest` releases the earliest
started conversation, and `lru` the least recently active. A conversation with a
turn in flight is never released.

`close` drains in-flight work, then releases conversations as stop does and
discards tokens, activity, cached resolutions, retry accounting, negative results,
and proposal state. A core that built its own `Engram` also clears its sessions;
the named sessions of a supplied `Engram` remain its caller's.

## Clock and graph encoder boundaries

`Engram` accepts an injected callable clock. Its `clock()` method validates that each result is an aware UTC datetime before timestamp consumers use it. The configured graph encoder is an optional capability: encoding requires a callable `encode` operation whose NumPy result contains one row, then checks that row against the configured vector dimension. Invalid clock results and malformed or absent encoders fail at their owning boundary. Matcher tables and conversational confidence/context limits retain their existing shape, type and finite-value validation before work begins.

## Process lifetime and static startup data

Each new process loads only the STATIC data provided for that startup. Structured
pattern, response, and template entries are loaded once into a fresh `Engram`
with `load_static_data` before it is passed to `EngramCore`. The operation rejects
an Engram that already contains statements, sessions, accepted responses, or
mutation receipts; it is not a refresh or synchronization interface.

Restart requires the owner to provide the current static data again. Dynamic
accepted responses, learned conversational statements and facts, sessions,
proposals, mutation receipts, reports, turn diagnostics, and their process
counters are not copied or recovered.

## Friendly fact phrasing

`engram.phrasing.phrase_fact` and `phrase_facts` render canonical triples for
recall without changing their identity. The idiomatic frames come from the
packaged `engram/data/fact-phrasing.toml` resource through `engram.constants_language`.
There is one inventory, also read as data by Tapestry's independent Research
process. Engram does not import Tapestry. Startup validates the resource's
bounded mapping and strictly bare subject/object placeholders; missing or
invalid data fails explicitly rather than selecting a copied fallback map.

Idiomatic overrides bypass spaCy. Other labels are phrased from their grammar,
with a bare-active fallback when Engram's NLP model is unavailable. A label that
starts with an auxiliary keeps its own. A label spaCy misreads in isolation, a
finite verb such as `prevents` tagged as a plural noun or `get` tagged as an
infinitive, is re-read after a subject pronoun. It becomes an active verb only if
WordNet knows its lemma as a verb, so a noun role such as `genre` keeps its
possessive frame. Without WordNet data the parsed reading stands. Tapestry
derives identical frames.
Braces in dynamic labels and argument text remain literal, including in that
fallback. Predicate slug underscores still normalize to spaces and sentence
lists retain their supplied order. Phrasing does not establish source entailment.

## Resolve inputs

| Input | Concrete type and absence | Rule |
| --- | --- | --- |
| `request` | nonempty string | Original caller text; bounded and validated |
| `request_id` | nonempty string | Retry identity; conflicting reuse fails |
| `user_id` | nonempty string, default `"0"` | Selects isolated, TTL-bound context for bounded follow-up enrichment |
| `namespace` | string, default `""` | Exact scope component |
| `context_fingerprint` | string, default `""` | Exact scope component |
| `identity` | mapping, default `{}` | Empty builds standalone identity; nonempty must be a validated authoritative `QueryIdentity` with matching scope |
| `required_metadata` | dictionary, default `{}` | Exact required accepted-response metadata |
| `required_source_label` | string, default `""` | Empty accepts every source label |
| `budget` | mapping, default `{}` | Empty captures the default bounded budget; nonempty must be a validated `ResolutionBudget` |
| `configured_resolvers` | tuple of nonempty strings, default `()` | Empty selects the configured plan |
| `accept_exact` | boolean, default `false` | Explicit direct-answer permission for one eligible exact result; without it that result is returned as `EVIDENCE` with reason `exact_answer_not_permitted` and earns no success credit |
| `cancellation_check` | zero-argument callable, default `()` | Transient cooperative check; raises `ResolutionCancelledError` when the caller cancels |

Falsey non-mapping identity or budget values such as `()`, `[]`, `""`, `0`,
and `false` are malformed. This preserves
one concrete absence type at the public boundary.

The cancellation callback is invoked while a request waits behind another
request with its request ID or user, before expensive resolver work, and at
cooperative structured, semantic, and composition boundaries. It remains transient
and outside the `request_id` signature. Cancellation discards partial resolution and
permits retry with the same request ID. An executing external driver call continues
until the driver returns. Graph I/O isolation keeps unrelated local requests and
status available during that call.

`resolve_request` also applies the process configuration's exact namespace rollout
selection. The selected mode participates in retry identity.
When graph access is enabled, `structured_graph` remains in every resolver plan,
including caller-supplied plans and every rollout mode. `disabled` executes
resolution and suppresses its result to `MISS`; `shadow` executes resolution while
suppressing candidate output and accepted-success credit; `evidence_only`
downgrades an answer to `EVIDENCE`; `rollback` uses exact and configured graph
retrieval and returns evidence or `MISS`; and `regulated_direct_answer` preserves
the behavior described by `accept_exact`. An interface cannot disable a configured
graph through resolver selection; graph participation is disabled only by
`graph.enabled: false`.
The configured graph resolver runs before local exact-answer short-circuiting and
uses the evaluation time captured by the shared request clock.
`core.status()["rollout"]` reports the default mode, override count,
and fixed per-mode counts aggregated across namespaces.

## Result access

`ResolutionResult` is a validated dictionary with one `ANSWER`, `EVIDENCE`, or
`MISS` outcome, bounded candidates, resolver results, diagnostics, budget
accounting, and an optional bounded Proposition evidence package. Typical keyed
access is:

```python
result = core.resolve_request(
    "What is Engram?",
    "resolution-42",
    user_id="account-42",
    namespace="support",
    configured_resolvers=("exact", "sparse", "support_semantic"),
)

outcome = result["outcome"]
candidates = result["response_candidates"]
package_available = result["evidence_package_available"]
proposition_records = result["evidence_package"]["records"]
```

`resolution_result_to_dict`, `resolution_result_to_json`, and their strict
decoders preserve concrete absence, exact accepted text, and
bounded evidence. Full Proposition records occur
inside `evidence_package`; fusion authorizes an Engram answer.

The compact previous query frame supplies session context; the repository owns
accepted knowledge. It
is defined with canonical relation resolution in the
[graph retrieval contracts](graph-retrieval.md). `user_id` selects the bounded
conversation context used to enrich the frame.

## Feedback and accounting

`record_resolution_feedback` consumes one candidate from the keyed result and
one typed external Regulator outcome. `inspect_feedback_learning` returns a
bounded dictionary snapshot. Proposal `resolve` remains the regulated
proposal-verdict operation; it is distinct from unified `resolve_request`.

Accepted-response candidacy and success statistics mutate only the authoritative
artifact collection. The conversational statement matcher has separate accounting.

`learn_response` admits one new artifact without implicit replacement.
Its idempotency signature preserves the exact admitted request and response,
including tab, line-feed, and carriage-return characters; canonical JSON escaping
does not rewrite the stored text. Other control characters and surrogates remain
invalid.
`supersede_response` atomically retires the current generation and admits its
explicit replacement, while `retire_response` removes an artifact established as
globally stale. Generated conversational or pipeline responses are not admitted by
any implicit learning hook.

## Operational telemetry

`EngramCore.operational_telemetry()` returns the fixed-cardinality process
aggregate. `core.status()["telemetry"]` returns the same information alongside
readiness. It includes outcomes, fixed resolver contributions and states, observed
latency buckets, budget/resource consumption, fixed Regulator outcomes, and graph
consultation/hit/miss/failure counters through fixed aggregate keys.

## Verification

- `tests/test_service.py` covers mapping-only identity and budget absence,
  authoritative identity input, exact result fields, keyed candidate feedback,
  malformed boundary values, and the shared core's restart and close lifecycle.
- `tests/test_resolution_contracts.py` covers deterministic codecs, exact
  fields, schema rejection, concrete absence, and outcome invariants.
- `tests/test_resolvers.py` covers the exact, structured graph, and support-linked
  semantic resolver adapters and their full Proposition evidence.
- `tests/test_resolver_execution.py` covers registry plans, bounded fail-soft
  execution, lease and reservation codecs, cancellation, and accounting.
- `tests/test_orchestrator_proposition_evidence.py` and
  `tests/test_resolution_orchestrator.py` cover full Proposition packages, fused
  outcomes, cancellation, and the complete output budget.
- `tests/test_service_resolution.py` covers exact answers, transient accounting,
  and receipts through `EngramCore`.
- `tests/test_grpc_server.py` and `tests/test_mcp_server.py` cover the public
  transport adapters and their process lifecycle.
- `tests/test_service.py` proves transient cancellation and identical request-ID retry.
- MCP, gRPC, conversation, and regulated-cache suites verify the current
  first-party interfaces.
