# Engram

A keyword-indexed statement store with hit-rate tracking, designed as a fast-path retrieval layer for conversational systems.

## Overview

ENGRAM sits between user queries and expensive computation (LLM inference,
database queries, API calls), serving qualified cached knowledge first.

Key features:

- **Keyword matching** - Fast, predictable retrieval using keyword overlap
- **Optional sparse retrieval** - Fielded BM25 with phrase and technical-identifier signals
- **Optional semantic retrieval and reranking** - Offline local request embeddings and a bounded transparent shortlist scorer
- **Hit-rate tracking** - Learning signal that improves retrieval over time
- **Two-tier storage** - STATIC (protected) and DYNAMIC (evictable) statements
- **User-aware chat** - Isolated conversation contexts with shared, attributed facts
- **Persistence** - JSON-based save/load with full state preservation
- **Multiple interfaces** - Python API, human CLI, persistent MCP tools, and a single-instance gRPC service

## Architecture

```text
CLI ---------\
              \
MCPServer -----> EngramCore ---> Engram, pipeline, sessions, persistence
              /
gRPC ---------/
```

`EngramCore` in `engram/service.py` is the transport-neutral application
facade. It owns the shared `Engram` instance, per-user conversation runtimes,
persistence lifecycle, and regulated-cache proposal state. The CLI, MCP, and
gRPC servers translate their interface inputs into calls on that core. The lower-level
`Engram`, `pipeline`, `sessions`, and `persistence` Python APIs remain available
and backward compatible.

## Integration guides

- [Python API](documentation/python-api.md) — unified resolution inputs,
  results, feedback, cancellation, rollout, and telemetry.
- [MCP integration](documentation/mcp-integration.md) — installation,
  process ownership, all tool contracts, persistence, host configuration, and
  the implemented two-phase regulated-cache interface.
- [gRPC integration](documentation/grpc-integration.md) — protobuf contract,
  launch configuration, RPCs, health, errors, durability, TLS, and shutdown.

## System documentation

- [Accepted responses](documentation/artifacts/contracts-v1.md) — authority,
  lifecycle, eligibility, mutation coordination, persistence, and compatibility.
- [Resolution results](documentation/evidence/resolution-result-v1.md) — unified
  result, Claim evidence, budgets, truncation, and adapter mapping.
- [Graph retrieval](documentation/graph-retrieval.md) — contextual, temporal,
  one-hop, and composed Claim retrieval.
- [Local resolvers](documentation/local-resolvers.md) — symbolic rewrites,
  sparse and semantic retrieval, reranking, and deterministic utilities.
- [Deployment and rollback](documentation/operations/deployment-and-rollback-v1.md)
  — readiness, authorization, operation, incidents, and rollback.

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
from engram.constants import Tier
from engram.core import Engram

# Create an instance
engram = Engram()

# Store statements
engram.store("Paris is the capital of France", tier=Tier.STATIC)
engram.store("France has a population of 67 million", tier=Tier.STATIC)

# Query - returns a dict with "matches" (a list of (statement, score)) and "keywords"
result = engram.query("What is the capital of France?")
stmt, score = result["matches"][0]
print(stmt["text"])  # "Paris is the capital of France"

# Record successful retrieval. Passing the statement id credits the statement
# itself, which feeds the hit-rate-aware eviction policies.
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
session_id = sessions.create_session(engram)

# First query
result = engram.query("What is the capital of France?", session_id=session_id)
stmt, score = result["matches"][0]
sessions.update_session_context(engram, session_id, stmt["text"])

# Follow-up query - context expands "its" to include France/Paris
result = engram.query("What is its population?", session_id=session_id)
```

Expansion only fires when the query carries a referring pronoun ("its",
"they", "that", ...); a self-contained follow-up keeps its own keywords
undiluted.

## Configuration

```python
from engram.config import engram_config, reranker_config, semantic_config, sparse_config
from engram.constants import EvictionPolicy, SessionOverflow
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
    eviction_policy=EvictionPolicy.FIFO,   # FIFO | LRU | LFU | HIT_RATE
    min_hit_rate=0.0,            # Protect proven statements above this hit rate
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

## Persistence

```python
from engram import persistence

# Save to file
persistence.save(engram, "engram_state.json")

# Load from file
engram = persistence.load_engram("engram_state.json")

# Or use JSON strings
json_str = persistence.save_json(engram)
engram = persistence.load_engram_json(json_str)
```

The saved state includes statements (including `introduced_by_user_id` and
`source_label` provenance), the keyword index with its statistics, user
contexts, bot properties, substitution maps, and non-secret configuration
(weights, eviction policy, feature flags). Graph passwords are runtime-only and
stay in runtime configuration. Loading restores the stored configuration unless a
`config` override is passed to the loader. Files written by older versions
(which stored only `capacity`) still load, with defaults for the rest.

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

DYNAMIC statements are evicted when `capacity` is reached, ordered by the
configured `eviction_policy`:

- `FIFO` - oldest statement first (default)
- `LRU` - least recently hit first; unhit statements precede hit statements
- `LFU` - lowest hit count first
- `HIT_RATE` - lowest hits/queries ratio first

The statistics behind LRU, LFU, and HIT_RATE accumulate through normal use:

- `query()` counts each returned match as a candidacy on that statement
- `record_hit(keywords, statement_id=...)` credits the statement that answered
- a `pattern_query()` selection records a candidacy and a hit in one step

`min_hit_rate` protects proven performers: a DYNAMIC statement with query
history and a hit rate at or above the threshold is skipped by eviction.
Statements with zero query history are always evictable. If every DYNAMIC
statement is protected, a new statement is admitted over capacity.

Hit statistics can be aged so old evidence loses standing:
`metrics.decay_statistics(engram, factor=0.5)` (or `engram decay` from the
CLI) multiplies every hit/query count by the factor. Rates are preserved while
confidence decays; an entry that stops re-earning its statistics eventually
returns to zero query history and loses `min_hit_rate` protection. Run it
periodically, like `expire_sessions`.

STATIC statements are excluded from capacity and eviction. Eviction or retirement
removes a pattern when its final statement owner is removed.

## API Reference

The data model is plain dicts. Interfaces normally use `EngramCore`; embedded
callers can continue using `Engram` methods and module-level functions
(`engram.sessions`, `engram.persistence`, `engram.metrics`) directly.

### EngramCore (`from engram.service import EngramCore`)

`EngramCore.open(config=..., store_path=..., seed_path=...)` loads or creates a
shared application runtime. Its primary operations are:

- `start_conversation`, `chat`, `inspect_conversation`,
  `finish_conversation`, and `stop_conversation`;
- `add_fact`, `set_predicate`, and `get_predicate`;
- `propose`, `resolve`, `learn_response`, and `retire_response`;
- `status` for transport-neutral readiness and durability information; and
- `flush` and `close` for persistence and lifecycle ownership.

One core retains multiple isolated user conversations and shared knowledge.
Non-empty conversation identifiers retain their user context. An empty
conversation identifier remains empty at the service boundary, receives a
unique non-attributed ephemeral session at each start, never aliases explicit
user `"0"`, and is deleted and checkpointed on stop. Configured stores
checkpoint successful mutations and flush again on `close()`.
See the [Python API contract](documentation/python-api.md) for unified resolution,
feedback, lifecycle, and error behavior.

### Engram methods

| Method                                                           | Description                                                                                                          |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `store(text, tier, pattern, pattern_aliases, template, priority, keyword_source, introduced_by_user_id, source_label)` | Add a statement with optional matcher aliases and provenance; `keyword_source` can index it under different text    |
| `query(text, session_id, limit, user_id)`                         | Keyword retrieval; `user_id` selects an isolated caller-owned context                                                  |
| `pattern_query(text, session_id, user_id, combine_sentences)`     | AIML-style match in a user context; returns `(statement, captured, response)` or `()`                                |
| `record_hit(keywords, statement_id)`                             | Update hit statistics after a successful retrieval; the optional `statement_id` credits the answering statement      |
| `learn_from_response(query, response, introduced_by_user_id, source_label)` | Cache a response with optional provenance; re-learning the same question replaces it in place                       |
| `retire_statement(statement_id)`                                 | Remove a statement and its pattern by id                                                                             |
| `learn_fact(fact, introduced_by_user_id, source_label)`          | Learn an extracted fact with optional provenance                                                                     |
| `add_fact(text, source_label, tier)`                              | Add one globally shared, unattributed fact and preserve user context                                                  |
| `get_statement(statement_id)`                                    | Fetch a statement dict by id (`{}` if absent)                                                                        |
| `load_corpus(statements, tier)`                                  | Bulk-add statements                                                                                                  |
| `fork(...)`                                                      | Create a child instance sharing the knowledge base                                                                   |

### Sessions (`from engram import sessions`)

| Function                                                        | Description                      |
| --------------------------------------------------------------- | -------------------------------- |
| `create_session(engram, session_id, metadata)`                  | Create a session, returns its id |
| `get_session(engram, session_id, create_if_missing)`            | Retrieve a session dict          |
| `update_session_context(engram, session_id, previous_response)` | Update session context           |
| `delete_session(engram, session_id)`                            | Remove a session                 |
| `expire_sessions(engram, inactive_threshold)`                   | Remove inactive sessions         |
| `list_sessions(engram, active_since)`                           | List sessions                    |

### Persistence (`from engram import persistence`)

| Function                     | Description                  |
| ---------------------------- | ---------------------------- |
| `save(engram, path)`         | Save state to JSON file      |
| `load_engram(path)`          | Load state from JSON file    |
| `save_json(engram)`          | Serialize to JSON string     |
| `load_engram_json(json_str)` | Deserialize from JSON string |

### Pipeline (`from engram import pipeline`)

`pipeline.chat(engram, text, user_id, llm_fn, high_confidence, context_limit, learn)`
is the user-aware entry point. It defaults to user `"0"` and returns the
normalized `user_id` in its result. The lower-level
`pipeline.respond(engram, text, session_id, llm_fn, high_confidence, context_limit, learn, user_id)`
packages the tiered strategy: scripted pattern match first, then a
high-confidence cached answer, then the caller's LLM with retrieved context
(whose response is learned for next time). Returns a dict with `response`,
`source` (`pattern` / `cache` / `llm` / `none`), `score`, `matches`, and
`keywords`. User-aware results also expose the selected `dialogue_act`,
`active_topic`, recent canonical `entities`, and `fact_admissions`. Each fact
admission records whether an inferred conversational fact was stored and, when
it was rejected, a stable reason such as `hedged`, `transient`, or
`meta_subject`.

For multi-sentence input, `pipeline.chat` produces one conversational reply
while still processing every sentence for learning and context. It normally
uses the final substantive sentence while preserving an earlier question,
command, fact, self-introduction, or topic change before a trailing courtesy.
Direct `pattern_query` calls retain the legacy AIML
behavior of combining every matched sentence response; pass
`combine_sentences=False` to request the conversational behavior explicitly.
Topic state is evidence-driven: explicit shifts and recalled facts promote a
topic, unrelated substantive turns replace or clear stale topics, and topic
labels discard conversational filler such as "for a while". Repeated responses
are checked across all conversational routes, including exact and broad
patterns, while direct lower-level pattern queries retain their legacy output.

```python
from engram import pipeline

def my_llm(text, context_statements):
    prompt = "\n".join(context_statements) + "\n\n" + text
    return call_llm(prompt)

result = pipeline.respond(engram, "What are the support hours?",
                          session_id=session_id, llm_fn=my_llm)
print(result["source"], result["response"])
```

The first ask goes to the LLM and is cached; the same question later answers
directly from the cache (`source == "cache"`).

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

## Command Line Interface

ENGRAM includes a CLI for managing stores from the terminal, at `scripts/cli.py`:

```bash
python scripts/cli.py --help
```

The examples below write `engram` as shorthand for `python scripts/cli.py` (set a
shell alias if you like: `alias engram='python scripts/cli.py'`).

### Initialize a Store

```bash
engram init
engram -s mystore.json init
```

`init` seeds the new store from the bundled corpus (`data/seed.json`), as does
the automatic initialization that runs when the store file is missing.

### Keep a Store in Sync with the Seed

A store is seeded once at creation; when `data/seed.json` improves afterward,
existing stores keep their old templates. `sync-seed` upserts the current seed
into the store: stale STATIC entries are updated in place (ids and hit
statistics preserved), new entries are added, and learned DYNAMIC content is
preserved.

```bash
engram sync-seed
engram sync-seed --file custom_seed.json
engram sync-seed --prune   # also retire STATIC entries removed from the seed
```

The default sync is an upsert, so entries deleted from the seed remain in the
store. With `--prune`, the STATIC tier mirrors the seed exactly -- only
use it when syncing the complete corpus, since anything the file omits is
retired.

### Store Statements

```bash
engram store "Paris is the capital of France" --static
engram store "Dynamic statement that can be evicted"
```

### Load from File

```bash
# Load statements from a JSON file of patterns/templates
engram load corpus.json --static
```

### Query

```bash
engram query "What is the capital of France?"
engram query "population" --limit 10
engram query "follow up question" --session user123
engram query "successful query" --hit  # Record as hit
```

### Session Management

```bash
engram session create --id user123
engram session list
engram session get user123
engram session update user123 "Previous response text"
engram session delete user123
engram session expire --hours 24
```

### Metrics and Analysis

```bash
engram metrics
engram keywords --low-hit --min-queries 10
engram keywords --zero-hit
engram decay --factor 0.5   # Age hit statistics (run periodically)
```

### Export

```bash
engram export --static-only -o static_corpus.txt
engram export --dynamic-only
```

### Interactive Mode

```bash
engram interactive --user-id Robin --initial-bot-text "." \
    --transcript conversation-recovery.json
```

Interactive mode is a chat loop routed through the tiered pipeline: pattern
matching first, with unanswered questions consulting keyword
retrieval before falling back. `--user-id` is an arbitrary caller-owned label
and defaults to a generated session for the human CLI. `--transcript` updates a
JSON recovery transcript after each turn. Type a message to get a response, or
use a slash command:

- `/debug` - Toggle debug output
- `/inspect` - Show the active context, learned facts, provenance, and metrics
- `/metrics` - Show metrics
- `/finish [path]` - Write JSON and Markdown conversation reports
- `/topic <name>` - Set the conversation topic
- `/set <name> <value>` - Set a session predicate
- `/get <name>` - Show a session predicate
- `/save` - Save to disk
- `/help` - List commands
- `/quit` - Exit (also `/exit`, `/q`)

### MCP Agent Interface

The MCP stdio server retains one core and its conversations between tool calls:

```bash
python -m engram.mcp_server
# Or, after installation:
engram-mcp
```

The ten tools cover conversation lifecycle (`engram_start`, `engram_send`,
`engram_inspect`, `engram_finish`, `engram_stop`), shared facts
(`engram_add_fact`), and regulated-cache use (`engram_propose`,
`engram_resolve`, `engram_learn_response`, `engram_retire_response`). See
[MCP integration](documentation/mcp-integration.md) for host configuration,
tool schemas, persistence, and recovery.

### gRPC Service Interface

The gRPC server exposes the same shared core, isolated user conversations, and
regulated-cache workflow. An empty `user_id` across Start, Chat, Inspect,
Finish, and Stop addresses the currently active anonymous conversation; each
new empty-identifier Start receives fresh session context:

```bash
engram-grpc --bind 127.0.0.1:50051 --store-path state/engram.json
# Or from a checkout:
python -m engram.grpc_server --bind 127.0.0.1:50051
```

Version 1 supplies conversation and cache RPCs; version 2 supplies unified
evidence resolution alongside v1. See [gRPC integration](documentation/grpc-integration.md)
for RPCs, client examples, health, TLS, retries, persistence, and shutdown.

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

## Knowledge Graph (optional)

ENGRAM can recall canonical facts from an optional MemGraph store. Runtime
access is read-only, and graph readiness is reported separately from local
service readiness.

Apply the sample schema (`schema.cypher`) before enabling the graph:

```bash
python scripts/setup_schema.py            # uses config.yml graph.host / graph.port
python scripts/setup_schema.py --check    # preview the statements
```

Configure the connection in `config.yml`:

```yaml
graph:
  host: localhost
  port: 7687
  username: ""
  password: ""
  enabled: true
  vector_enabled: true
  vector_index_name: claim_premise_embeddings
  vector_model: all-MiniLM-L6-v2
  vector_model_path: data/artifacts/models/all-MiniLM-L6-v2
  vector_dimension: 384
  vector_limit: 250
  vector_support_scan_limit: 100000
  vector_min_similarity: 0.45
  vector_weight: 0.75
```

Use a read-only database account and supply credentials through runtime
configuration. The [graph retrieval guide](documentation/graph-retrieval.md)
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
