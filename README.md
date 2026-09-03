# Engram

An in-process conversational retrieval and regulated-response component with
optional graph-backed evidence resolution.

## Overview

ENGRAM supplies deterministic conversation behavior and an in-process accepted-
response artifact collection for regulated information recall.

Key features:

- **Keyword matching** - Fast, predictable retrieval using keyword overlap
- **Optional sparse retrieval** - Fielded BM25 with phrase and technical-identifier signals
- **Optional semantic retrieval and reranking** - Offline local request embeddings and a bounded transparent shortlist scorer
- **Hit-rate tracking** - Learning signal that improves retrieval over time
- **Two-tier statements** - STATIC (provided at startup) and DYNAMIC (process-local and evictable)
- **User-aware chat** - Isolated conversation contexts with shared, attributed facts
- **Process memory** - Accepted responses, conversations, and receipts have one in-memory owner
- **Multiple interfaces** - Python API, CLI, MCP tools, and a single-instance gRPC service over one shared core

## Architecture

```text
Python API --\
CLI ----------+--> EngramCore ---> Engram, pipeline, sessions
MCP stdio ----+
gRPC --------/
```

`EngramCore` in `engram/service.py` is the transport-neutral application
facade. It owns the shared `Engram` instance, per-user conversation runtimes,
the sole accepted-response artifact collection, and regulated-cache proposal
state. Python callers and the MCP and gRPC adapters invoke that core. The lower-level
`Engram`, `pipeline`, and `sessions` Python APIs remain available. When graph
access is enabled, every interface uses the same graph-aware resolution and
conversation behavior; adapters cannot select a graph-bypassing mode.

A new process loads its current provided STATIC data once, before serving, with
`Engram.load_static_data`. Static data is startup input, not recovered Engram
state. Restart begins with no dynamic accepted responses, learned conversational
statements or facts, sessions, proposals, mutation receipts, reports, turn
diagnostics, or counters inherited from the previous process.

## Integration guides

- [Python API](documentation/python-api.md) — unified resolution inputs,
  results, feedback, cancellation, rollout, and telemetry.
- [MCP integration](documentation/mcp-integration.md) — installation,
  process ownership, all tool contracts, host configuration, and
  the implemented two-phase regulated-cache interface.
- [gRPC integration](documentation/grpc-integration.md) — protobuf contract,
  launch configuration, RPCs, health, errors, TLS, and shutdown.

## System documentation

- Accepted responses — one authoritative in-process artifact collection with
  lifecycle, eligibility, and mutation coordination.
- [Graph retrieval](documentation/graph-retrieval.md) — contextual, temporal,
  one-hop, and composed Proposition retrieval.
- [Local resolvers](documentation/local-resolvers.md) — symbolic rewrites,
  sparse and semantic retrieval, reranking, and deterministic utilities.

## Contributor documentation

- [Python code style](documentation/code-style.md) — the Engram import
  convention and the Google Python Style Guide baseline used elsewhere.
- [Query identity contracts](documentation/identity/contracts-v1.md) — the
  versioned scope, identity, normalization, retrieval-key, representation, and
  authoritative-input foundation used by exact retrieval work.

## Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Download NLTK data into the local, gitignored data/nltk_data directory.
# Provision runtime datasets once after installation.
python -m engram.nltk_data

# Download the spaCy English model (parsing, NER, lemmas).
python -m spacy download en_core_web_sm
```

ENGRAM uses several NLTK datasets (punkt, averaged_perceptron_tagger,
maxent_ne_chunker, words, wordnet, omw-1.4, vader_lexicon). They are managed
centrally by `engram/nltk_data.py`, which stores them in the Engram checkout's
`data/nltk_data` directory (gitignored) and puts that absolute directory first
on NLTK's search path regardless of the process working directory. Startup
preflight fails with a bounded readiness error when a required dataset is
missing. The setup command above provisions serving data.

## Quick Start

```python
from engram.core import Engram

# Create an instance and load the static data provided for this startup.
engram = Engram()
engram.load_static_data(
    [
        {"response": "Paris is the capital of France"},
        {"response": "France has a population of 67 million"},
    ]
)

# Query - returns a dict with "matches" (a list of (statement, score)) and "keywords"
result = engram.query("What is the capital of France?")
stmt, score = result["matches"][0]
print(stmt["text"])  # "Paris is the capital of France"

# Record successful retrieval. Passing the statement id credits the statement
# itself, which feeds least-recently-used eviction.
engram.record_hit(result["keywords"], statement_id=stmt["id"])
```

## User-aware chatbot and shared facts

`pipeline.chat` is the in-process chatbot entry point. The caller owns the
user label: it is an arbitrary, case-sensitive string that Engram preserves
as an opaque value. A missing or empty label becomes `"0"`.

```python
from engram import pipeline

# A catch-all supplies the chatbot's default conversational behavior.
engram.store("Go on.", pattern="*", tier=Tier.STATIC)

pipeline.chat(engram, "Sushi is good.", user_id="Alice")
result = pipeline.chat(engram, "What's good?", user_id="Carol")
print(result["response"])  # "Sushi is good."
```

Alice and Carol have separate histories, predicates, active topics, referenced
entities, dialogue-act histories, and pronoun context. Facts learned from either
conversation enter the shared statement store with `introduced_by_user_id` and
remain globally retrievable. Each learned fact occupies
one statement, with alternate question phrasings stored as matcher aliases.

Research and tool output enter the same shared store with source provenance:

```python
statement_id = engram.add_fact(
    "Tokyo is the capital of Japan.",
    source_label="research-tool",
)
```

`add_fact` ingests one fact, optionally records an opaque source label, and
leaves user context unchanged. Conversational facts and external
facts are both globally retrievable. The API accepts one fact per call and has no
batch-ingestion operation.

## Sessions

Sessions enable context expansion for follow-up queries:

```python
from engram import sessions

# Create a session
session_id = sessions.start_session(engram)

# First query
result = engram.query("What is the capital of France?", context_id=session_id)
stmt, score = result["matches"][0]
sessions.update_session_context(engram, session_id, stmt["text"])

# Follow-up query - context expands "its" to include France/Paris
result = engram.query("What is its population?", context_id=session_id)
```

Expansion only fires when the query carries a referring pronoun ("its",
"they", "that", ...); a self-contained follow-up keeps its own keywords
undiluted.

## Configuration

```python
from engram.config import engram_config, reranker_config, semantic_config, sparse_config
from engram.constants import SessionOverflow
from engram.core import Engram
from engram.utilities import utility_config

config = engram_config(
    capacity=10000,              # Max DYNAMIC statements
    max_sessions=10000,          # Max concurrent sessions
    session_ttl_seconds=86400,   # 24 hour session TTL
    weight_base=0.5,             # Scoring weight: base
    weight_recency=0.3,          # Scoring weight: recency
    weight_hit_rate=0.2,         # Scoring weight: hit rate
    recency_half_life_seconds=604800.0,  # Recency decay half-life (7 days)
    session_overflow=SessionOverflow.LRU,  # LRU eviction when at limit
    retrieval_rewrites_enabled=False,  # Opt-in retrieval-only symbolic reductions
    sparse=sparse_config(enabled=False),  # Opt-in local fielded BM25
    semantic=semantic_config(enabled=False),  # Requires an explicitly provisioned artifact when enabled
    reranker=reranker_config(enabled=False),  # Optional bounded shortlist scorer
    utility=utility_config(enabled=False),  # Optional deterministic operations
    learn_user_facts=True,       # Learn shared facts with user attribution
    use_stemming=True,           # Porter-stemmed fallback matching
    use_lemmatization=True,      # WordNet-lemmatized fallback matching (precise)
    use_synonyms=True,           # WordNet synonym expansion on keyword queries
    use_spell_correction=True,   # Correct input typos toward the store vocabulary
    polish_responses=True,       # Repair casing in pattern-path responses
)

engram = Engram(config=config)
```

Retrieval rewrites, sparse and semantic retrieval, reranking, and deterministic
utilities are optional and disabled by default. Their configuration, artifact
provisioning, readiness, and result behavior are documented in the
[local resolver guide](documentation/local-resolvers.md).

## Scoring Algorithm

Statements are scored on a calibrated 0.0 to 1.0 scale, so confidence
thresholds mean the same thing for every query:

```
score = overlap * (weight_base + weight_recency * recency + weight_hit_rate * hit_rate)
        / (weight_base + weight_recency + weight_hit_rate)
        + priority
```

Where:

- `overlap` - IDF-weighted fraction of query keywords present in the statement
  (0.0 to 1.0). Rare keywords count for more than common ones, and a keyword
  matched only through a WordNet synonym earns half credit.
- `recency` - Exponential time decay of the statement's last activity
  (`last_hit`, falling back to `created_at`), with half-life
  `recency_half_life_seconds` (default 7 days). Recency depends only on the
  statement's own timestamps, so it is stable under eviction.
- `hit_rate` - Average hit rate of the matched keywords (0.0 to 1.0).
- `priority` - The statement's priority field, added on top. Calibrated scores
  have a maximum of 1.0, so priority 1 or greater wins among keyword matches.
  Priority also breaks ties between statements sharing the same pattern.

A full-overlap, fresh, unproven statement scores 0.9 with the default weights;
the recommended direct-answer threshold is 0.7.

## Eviction and Hit Tracking

DYNAMIC statements are evicted when `capacity` is reached. Eviction is
least-recently-used: `last_hit` is the activity time when present, otherwise
`created_at` is used. STATIC statements do not consume dynamic capacity.

The activity and hit statistics used by retrieval and LRU eviction accumulate
through normal use:

- `query()` counts each returned match as a candidacy on that statement
- `record_hit(keywords, statement_id=...)` credits the statement that answered
- a `pattern_query()` selection records a candidacy and a hit in one step

Hit statistics can be aged so old evidence loses standing:
`metrics.decay_statistics(engram, factor=0.5)` multiplies every hit/query count
by the factor. Rates are preserved while historical volume decays. Run it
periodically alongside `expire_sessions` when the embedding application wants
older observations to carry less weight.

STATIC statements are excluded from capacity and eviction. Eviction or retirement
removes a pattern when its final statement owner is removed.

## API Reference

The data model is plain dicts. Interfaces normally use `EngramCore`; embedded
callers use `Engram` methods and module-level functions
(`engram.sessions`, `engram.metrics`) directly.

### EngramCore (`from engram.service import EngramCore`)

`open_engram_core(config=...)` creates an empty shared application runtime. Its
primary operations are:

- `start_conversation`, `chat`, `inspect_conversation`,
  `finish_conversation`, and `stop_conversation`;
- `add_fact`, `set_predicate`, and `get_predicate`;
- `propose`, `resolve`, `learn_response`, `supersede_response`, and `retire_response`;
- `status` for transport-neutral readiness information; and
- `close` for lifecycle ownership.

The regulated-cache lifecycle is explicit: `resolve(...,
outcome="rejected_stale")` records the verdict and excludes that observed
generation without mutating the response artifact. The caller that established
global staleness then uses `retire_response` as the separate, auditable
lifecycle operation. Contextual rejection never retires a response.

One core retains multiple isolated user conversations and shared knowledge.
Non-empty conversation identifiers retain their user context. An empty
conversation identifier remains empty at the service boundary, receives a
unique non-attributed ephemeral session at each start, never aliases explicit
user `"0"`, and is deleted on stop. That behavior serves gRPC's anonymous
conversation contract; MCP canonicalizes an omitted or empty label to the
unknown user `"0"` before starting its single conversation. Restarting Engram
loads only the STATIC data provided for that new process; without provided
STATIC data, it starts empty.
See the [Python API contract](documentation/python-api.md) for unified resolution,
feedback, lifecycle, and error behavior.

### Engram methods

| Method                                                           | Description                                                                                                          |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `store(text, tier, statement_id, pattern, pattern_aliases, that, topic, template, priority, keyword_source, introduced_by_user_id, source_label)` | Add a statement with optional matcher aliases, context constraints, identity, and provenance |
| `query(text, context_id, limit, statement_filter, record_candidates)` | Keyword retrieval in an optional conversation context with optional filtering and candidacy accounting              |
| `pattern_query(text, context_id, user_id, include_graph, evaluation_time)` | AIML-style match with separate turn context, learned-fact attribution, and optional timestamped graph fallback; returns `(statement, captured, response)` or `()` |
| `record_hit(keywords, statement_id)`                             | Update hit statistics after a successful retrieval; the optional `statement_id` credits the answering statement      |
| `retire_statement(statement_id)`                                 | Remove a statement and its pattern by id                                                                             |
| `learn_fact(fact, introduced_by_user_id, source_label, tier)`    | Learn an extracted fact with optional provenance                                                                     |
| `add_fact(text, source_label, tier)`                             | Add one globally shared, unattributed fact and preserve user context                                                  |
| `get_statement(statement_id)`                                    | Fetch a statement dict by id (`{}` if absent)                                                                        |
| `load_corpus(statements, tier)`                                  | Bulk-add statements                                                                                                  |
| `load_static_data(pairs)`                                       | Load one provided structured STATIC corpus into a fresh Engram                                                       |

### Sessions (`from engram import sessions`)

| Function                                                        | Description                      |
| --------------------------------------------------------------- | -------------------------------- |
| `start_session(engram, session_id, metadata)`                   | Start a session, returns its id  |
| `get_session(engram, session_id, create_if_missing)`            | Retrieve a session dict          |
| `update_session_context(engram, session_id, previous_response)` | Update session context           |
| `delete_session(engram, session_id)`                            | Remove a session                 |
| `expire_sessions(engram, inactive_threshold)`                   | Remove inactive sessions         |
| `list_sessions(engram, active_since)`                           | List sessions                    |

### Pipeline (`from engram import pipeline`)

`pipeline.chat(engram, text, user_id, llm_fn, high_confidence, context_limit)`
is the user-aware entry point. It defaults to user `"0"` and returns the
normalized `user_id` in its result. The lower-level
`pipeline.respond(engram, text, context_id, llm_fn, high_confidence, context_limit, user_id)`
packages the tiered strategy: scripted pattern match first, then a
high-confidence conversational statement, then the caller's LLM with retrieved
context. Generated responses are recorded only in the conversation session and
are never copied into matcher statements. Returns a dict with `response`,
`source` (`pattern` / `statement` / `llm` / `none`), `score`, `matches`, and
`keywords`. User-aware results also expose the selected `dialogue_act`,
`active_topic`, recent canonical `entities`, and `fact_admissions`. Each fact
admission records whether an inferred conversational fact was stored and, when
it was rejected, a stable reason such as `hedged`, `transient`, or
`meta_subject`.

For multi-sentence input, both `pipeline.chat` and `pattern_query` produce one conversational reply
while still processing every sentence for learning and context. It normally
uses the final substantive sentence while preserving an earlier question,
command, fact, self-introduction, or topic change before a trailing courtesy.
Topic state is evidence-driven: explicit shifts and recalled facts promote a
topic, unrelated substantive turns replace or clear stale topics, and topic
labels discard conversational filler such as "for a while". Repeated responses
are checked across all conversational routes, including exact and broad
patterns.

```python
from engram import pipeline

def my_llm(text, context_statements):
    prompt = "\n".join(context_statements) + "\n\n" + text
    return call_llm(prompt)

result = pipeline.respond(engram, "What are the support hours?",
                          context_id=session_id, llm_fn=my_llm)
print(result["source"], result["response"])
```

The LLM result is not admitted as accepted knowledge. Accepted responses enter
only through `EngramCore.learn_response` and exist only as response artifacts.

A catch-all (pure-wildcard) question match is held as a fallback while retrieval and the
LLM speak first, and returns it only when neither does.

### Metrics (`from engram import metrics`)

`metrics.get_metrics(engram)` returns a dict with these keys:

| Key               | Description           |
| ----------------- | --------------------- |
| `statement_count` | Total statements      |
| `static_count`    | STATIC tier count     |
| `dynamic_count`   | DYNAMIC tier count    |
| `keyword_count`   | Distinct keywords     |
| `session_count`   | Active sessions       |
| `query_count`     | Queries performed     |
| `hit_count`       | Hits recorded         |
| `eviction_count`  | Evictions             |
| `hit_rate`        | Hit rate (0.0 to 1.0) |

`metrics.decay_statistics(engram, factor=0.5)` ages every hit/query count by
the factor (see Eviction and Hit Tracking above).

## Service interfaces

### Command-line interface

The one-shot CLI query uses the same unified resolver as Python, MCP, and gRPC:

```bash
python scripts/cli.py --config config.yml query "What evidence is available?" \
  --request-id cli-query-1
```

Interactive CLI turns use the same shared conversation path as MCP and gRPC.
Configured graph retrieval therefore has the same precedence, temporal filtering,
deduplication, and fail-soft behavior at each interface.

### MCP Agent Interface

The MCP stdio server retains one core and its conversations between tool calls:

```bash
python -m engram.mcp_server
# Or, after installation:
engram-mcp
```

Both commands load Engram's packaged conversational corpus by default. Use
`--static-data /trusted/path/conversation.json` to select another host-owned
corpus, or `--no-static-data` for an intentionally empty cache-only process.
Conversational corpora must contain a `*` catch-all.

MCP owns one active conversation. An omitted or empty `user_id` starts that
conversation as the unknown user `"0"`; send, inspect, finish, and stop all use
and report the same canonical identifier.

The eleven tools cover conversation lifecycle (`engram_start`, `engram_send`,
`engram_inspect`, `engram_finish`, `engram_stop`), shared facts
(`engram_add_fact`), unified resolution (`engram_query`), and regulated-cache use (`engram_propose`,
`engram_resolve`, `engram_learn_response`, `engram_retire_response`). See
[MCP integration](documentation/mcp-integration.md) for host configuration,
tool schemas, process-memory ownership, and retry behavior.

### gRPC Service Interface

The gRPC server exposes the same shared core, isolated user conversations, and
regulated-cache workflow. An empty `user_id` across Start, Chat, Inspect,
Finish, and Stop addresses the currently active anonymous conversation; each
new empty-identifier Start receives fresh session context:

```bash
engram-grpc --bind 127.0.0.1:50051 --config-path config.yml
# Or from a checkout:
python -m engram.grpc_server --bind 127.0.0.1:50051
```

One unversioned `engram` protobuf package supplies conversation, cache, and
unified evidence-resolution services from the same library contract. See
[gRPC integration](documentation/grpc-integration.md) for RPCs, client examples,
health, TLS, cancellation, process-memory behavior, and shutdown.

## NLP Features

ENGRAM uses NLTK to make pattern matching and responses more robust.

### Flexible matching

When an exact pattern match fails, the matcher retries against normalized forms
of the input, in order of precision:

1. **Lemmatization** (`use_lemmatization`, default on) - WordNet lemmas map
   irregular forms to their base, e.g. `mice` -> `MOUSE`, `went` -> `GO`.
2. **Stemming** (`use_stemming`, default on) - Porter stemming handles common
   inflections, e.g. `running` -> `RUN`, `cats` -> `CAT`.

Exact matches always win; lemmatization is tried before the more aggressive
stemming fallback.

Contraction expansion also normalizes input before matching, including
apostrophe-less forms (`whats` -> `what is`, `im` -> `i am`).

### Input spelling correction

With `use_spell_correction` (default on), an out-of-vocabulary token of at least
four characters is corrected to a unique nearby word in the store vocabulary.
The maximum Damerau-Levenshtein distance is 1, or 2 for tokens of at least six
characters; known English words are unchanged.

### Response polish

With `polish_responses` (default on), pattern responses adjust capitalization
only.
`{clause:...}` keeps the first clause of a capture, and `{name:...}` extracts a
name from a self-introduction capture.

### Question-aware responses

`engram.nlp.input_kind` classifies input as `question`, `command`, or
`statement`, and templates can branch with `{qtype:...}`. A question matched
only by the catch-all consults retrieval and the configured LLM before using
the catch-all response.

### Sentiment-aware responses

`{sentiment:...}` uses NLTK VADER and returns `positive`, `negative`, or
`neutral`, allowing one template to branch by tone. Predicate names beginning
with `_` remain scoped to the current template.

### Relational fact extraction (spaCy)

The built-in NLTK extractor handles plain copula sentences (`X is/are Y`).
Conversational learning applies hedged, transient, vague, and chat-meta filters;
explicit `add_fact` calls ingest directly. Existing facts
produce confirmation or contradiction responses. With spaCy,
`engram.facts_spacy.extract_facts` extracts subject-predicate-object triples:

| Sentence                                    | Triple                                      |
| ------------------------------------------- | ------------------------------------------- |
| Paris is the capital of France              | `(Paris, is, capital of France)`            |
| Paris is in France                          | `(Paris, in, France)`                       |
| Einstein developed the theory of relativity | `(Einstein, develop, theory of relativity)` |
| The book belongs to Mary                    | `(book, belong to, Mary)`                   |

Copulas retain their surface form, prepositions become relations, and action
verbs use their lemma. NER supplies `subject_type` and `obj_type` when available.
`learn_user_facts` controls conversational learning; `use_spacy_facts` selects
the relational extractor. Run `python eval/compare_facts.py` to compare both.

### Optional spaCy matching/retrieval enhancements

All off by default; each is a config flag.

- **`use_spacy_lemmatization`** - lemmatize matcher input with spaCy's
  context-aware lemmatizer, so `saw` (verb)
  resolves to `see` while `saw` (noun) stays `saw`.
- **`use_phrase_keywords`** - extract keywords with spaCy, keeping noun-chunk
  phrases (`machine learning`) as index terms alongside their component lemmas,
  for higher retrieval precision on compound terms.

A catch-all `*` pattern is considered only after the lemma and stem fallbacks
fail to find a more specific match.

## Knowledge Graph schema administration

ENGRAM can recall canonical facts from an optional MemGraph store. Runtime
graph operations are reads and do not issue writes. Graph readiness is reported
separately from local service readiness. During resolution, a graph connection,
query, or optional vector-index failure contributes no graph result. If no local
resolver supplies a result, Engram returns the same `MISS` it returns after a
successful graph query with no rows; component diagnostics may still report the
graph failure.

When `graph.enabled` is true, the graph participates in every graph-eligible
request through the shared core across Python, CLI, MCP, gRPC, every rollout
mode, and future adapters. Conversational graph hits precede scripted factual
fallbacks. Current surface reads apply valid-time bounds and suppress duplicate
semantic triples; Tapestry domain entity types normalize to Engram's coarse
`ENTITY` answer type. The shared core captures the evaluation time and schedules
configured graph resolution before local exact-answer short-circuiting.

For a standalone Engram-managed Memgraph, apply only Engram's independently
installable corrected recall schema:

```bash
python scripts/setup_schema.py --check
python scripts/setup_schema.py --apply
python scripts/setup_schema.py --verify
python scripts/verify_schema.py --deployment standalone
python scripts/reset_schema.py          # dry-run only; empty graph required
python scripts/reset_schema.py --apply
```

For a Tapestry-managed Memgraph, never run Engram's installer or reset command.
Tapestry owns that deployment's DDL. Verify it through Engram's catalog verifier:

```bash
python scripts/verify_schema.py --deployment tapestry_managed
```

Standalone mode requires Engram ownership and an exact catalog.
`tapestry_managed` requires Tapestry ownership, state `accepted`, matching
representation/support/scratch contracts, and every
Engram-required catalog definition while allowing the Tapestry superset. Crossed
owners, mixed metadata, partial catalogs, unavailable reads, and invalid
vector shapes fail closed. Static `--check` needs no configuration, Tapestry
checkout, service, or database.

Engram's graph-facing queries, decoders, and accepted-response support values
use the current Proposition/Assertion contracts. Managed startup requires the
configured Tapestry graph to be in its administrative `accepted` state and
fails closed otherwise. Local Engram operation without graph recall is
unaffected.

Configure the connection in `config.yml`:

```yaml
graph:
  host: localhost
  port: 7687
  username: ""
  password: ""
  enabled: true
  deployment_mode: tapestry_managed
  visibility_scope:
    kind: global
    company_id: {}
    customer_id: {}
    engagement_id: {}
  vector_enabled: true
  vector_index_name: proposition_embeddings
  vector_model: all-MiniLM-L6-v2
  vector_model_path: data/artifacts/models/all-MiniLM-L6-v2
  vector_dimension: 384
  vector_limit: 250
  vector_support_scan_limit: 100000
  vector_min_similarity: 0.45
  vector_weight: 0.75
```

Supply the configured database account through runtime configuration. It may be
the same write-capable account used by Tapestry; Engram's managed runtime simply
does not issue graph writes. The [graph retrieval guide](documentation/graph-retrieval.md)
defines canonical identity, relation paths, temporal/conflict handling, vector
support, availability, and timing behavior.

## Evaluation

`eval/run_eval.py` cycles a corpus of prompts (`eval/corpus.json`) through a
freshly seeded in-memory Engram instance and
reports how each prompt is answered:

```bash
python eval/run_eval.py            # human-readable report
python eval/run_eval.py --json report.json
```

Each prompt is classified as a **specific** match (a real, intentional
pattern), **catch-all** (only the `*` fallback matched - a coverage gap), or
**fallback** (empty retrieval). The matched pattern shown for each gap indicates
whether it needs new content or an engine fix.

## Contributing

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run linter
ruff check .

# Format code
black -l 132 -t py311 .
```

## License

Apache License 2.0
