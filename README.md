# Engram

A keyword-indexed statement store with hit-rate tracking, designed as a fast-path retrieval layer for conversational systems.

## Overview

ENGRAM sits between user queries and expensive computation (LLM inference, database queries, API calls). When a query matches stored statements with sufficient quality, the system returns cached knowledge without invoking downstream resources.

Key features:

- **Keyword matching** - Fast, predictable retrieval using keyword overlap
- **Hit-rate tracking** - Learning signal that improves retrieval over time
- **Two-tier storage** - STATIC (protected) and DYNAMIC (evictable) statements
- **User-aware chat** - Isolated conversation contexts with shared, attributed facts
- **Persistence** - JSON-based save/load with full state preservation
- **Multiple interfaces** - Existing Python API, a human CLI, and persistent FastMCP tools

## Architecture

```text
CLI ---------\
              \
FastMCP -------> EngramCore ---> Engram, pipeline, sessions, persistence
              /
future -------/
```

`EngramCore` in `engram/service.py` is the transport-neutral application
facade. It owns the shared `Engram` instance, per-user conversation runtimes,
persistence lifecycle, and regulated-cache proposal state. The CLI and MCP
server translate their interface inputs into calls on that core; neither owns
an independent implementation of Engram behavior. The lower-level `Engram`,
`pipeline`, `sessions`, and `persistence` Python APIs remain available and
backward compatible.

## Integration guides

- [Tapestry–Engram integration](documentation/tapestry-integration.md) — the
  Regulator-controlled response-cache workflow, existing Python API mapping,
  learning rules, invalidation, failure behavior, metrics, and acceptance tests.
- [FastMCP integration](documentation/mcp-integration.md) — installation,
  process ownership, all tool contracts, persistence, host configuration, and
  the implemented two-phase Tapestry cache interface.

## Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Download NLTK data into the local, gitignored data/nltk_data directory.
# Run this once after install so datasets are not fetched during runtime.
python -m engram.nltk_data

# Download the spaCy English model (parsing, NER, lemmas).
python -m spacy download en_core_web_sm
```

ENGRAM uses several NLTK datasets (punkt, averaged_perceptron_tagger,
maxent_ne_chunker, words, wordnet, omw-1.4, vader_lexicon). They are managed
centrally by `engram/nltk_data.py`, which stores them in `data/nltk_data`
(gitignored) and puts that directory first on NLTK's search path. If a dataset
is missing at runtime it is fetched on demand as a fallback, but pre-fetching
keeps normal operation offline and fast.

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
without interpreting. A missing or empty label becomes `"0"`.

```python
from engram import pipeline

# A catch-all supplies the chatbot's default conversational behavior.
engram.store("Go on.", pattern="*", tier=Tier.STATIC)

pipeline.chat(engram, "Sushi is good.", user_id="Alice")
result = pipeline.chat(engram, "What's good?", user_id="Carol")
print(result["response"])  # "Sushi is good."
```

Alice and Carol have separate histories, predicates, active topics, referenced
entities, dialogue-act histories, and pronoun context. Facts learned from either conversation enter the shared statement
store and retain `introduced_by_user_id`, so another user can retrieve them
without inheriting the speaker's conversation state. Each learned fact occupies
one statement; alternate question phrasings are matcher aliases on that
statement rather than duplicate cache entries.

Research and tool output can enter the same shared store without pretending
to be a user:

```python
statement_id = engram.add_fact(
    "Tokyo is the capital of Japan.",
    source_label="research-tool",
)
```

`add_fact` ingests one fact, optionally records an opaque source label, and
does not create or modify any user context. Conversational facts and external
facts are both globally retrievable. Batch ingestion is intentionally deferred.

## Regulated response-cache integration

Tapestry can place its Regulator between Engram retrieval and the Actor:

```text
request -> Engram candidate -> Regulator
                               | accepted -> return candidate
                               | rejected/miss -> Actor
                                                   | IDK -> return without learning
                                                   | answer -> cache in Engram -> return
```

For the current in-process integration, use `Engram.query(..., limit=1)` to
obtain a speculative candidate, call `record_hit()` only after Regulator
acceptance, and use `learn_from_response()` for a cacheable Actor answer. A
rejected candidate receives no hit, so its candidacy naturally lowers its hit
rate. Do not send Actor answers through fact ingestion merely to cache them.

`pipeline.respond()` is intentionally more autonomous: it accepts qualifying
pattern and cache responses itself. Likewise, `pattern_query()` records a
selected pattern as successful immediately. Neither is the correct proposal
boundary when the Regulator must commit acceptance. See the complete
[Tapestry integration guide](documentation/tapestry-integration.md) for the
current API example, scoping constraints, replacement policy, and delivery
phases.

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
from engram.config import engram_config
from engram.constants import EvictionPolicy, SessionOverflow
from engram.core import Engram

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
    learn_user_facts=True,       # Learn shared facts with user attribution
    use_stemming=True,           # Porter-stemmed fallback matching
    use_lemmatization=True,      # WordNet-lemmatized fallback matching (precise)
    use_synonyms=True,           # WordNet synonym expansion on keyword queries
    use_spell_correction=True,   # Correct input typos toward the store vocabulary
    polish_responses=True,       # Repair casing in pattern-path responses
)

engram = Engram(config=config)
```

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
are never persisted. Loading restores the stored configuration unless a
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
- `priority` - The statement's priority field, added on top. Since calibrated
  scores never exceed 1.0, a priority of 1 or more is an absolute override
  among matching statements (it never applies without a keyword match).
  Priority also breaks ties between statements sharing the same pattern.

A full-overlap, fresh, unproven statement scores 0.9 with the default weights;
0.7 is a reasonable "answer directly without the LLM" threshold.

## Eviction and Hit Tracking

DYNAMIC statements are evicted when `capacity` is reached, ordered by the
configured `eviction_policy`:

- `FIFO` - oldest statement first (default)
- `LRU` - least recently hit first; never-hit statements go before hit ones
- `LFU` - lowest hit count first
- `HIT_RATE` - lowest hits/queries ratio first

The statistics behind LRU, LFU, and HIT_RATE accumulate through normal use:

- `query()` counts each returned match as a candidacy on that statement
- `record_hit(keywords, statement_id=...)` credits the statement that answered
- a `pattern_query()` selection records a candidacy and a hit in one step

`min_hit_rate` protects proven performers: a DYNAMIC statement with query
history and a hit rate at or above the threshold is skipped by eviction.
Statements with no query history are always evictable. If every DYNAMIC
statement is protected, a new statement is admitted over capacity rather than
dropped.

Hit statistics can be aged so old evidence loses standing:
`metrics.decay_statistics(engram, factor=0.5)` (or `engram decay` from the
CLI) multiplies every hit/query count by the factor. Rates are preserved while
confidence decays; an entry that stops re-earning its statistics eventually
returns to zero query history and loses `min_hit_rate` protection. Run it
periodically, like `expire_sessions`.

STATIC statements never count toward capacity and are never evicted. Evicting
or retiring a statement also removes its pattern from the matcher (unless
another statement still carries the same pattern), so dead patterns cannot
shadow live ones.

## API Reference

The data model is plain dicts. Interfaces normally use `EngramCore`; embedded
callers can continue using `Engram` methods and module-level functions
(`engram.sessions`, `engram.persistence`, `engram.metrics`) directly.

### EngramCore (`from engram.service import EngramCore`)

`EngramCore.open(config=..., store_path=..., seed_path=...)` loads or creates a
shared application runtime. Its public operations include:

- `start_conversation`, `chat`, `inspect_conversation`,
  `finish_conversation`, and `stop_conversation`;
- `add_fact`, `set_predicate`, and `get_predicate`;
- `propose`, `resolve`, `learn_response`, and `retire_response`;
- `flush` and `close` for persistence and lifecycle ownership.

One core can retain multiple user conversations while sharing learned
knowledge. Regulated-cache operations do not require a chatbot conversation.

### Engram methods

| Method                                                           | Description                                                                                                          |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `store(text, tier, pattern, pattern_aliases, template, priority, keyword_source, introduced_by_user_id, source_label)` | Add a statement with optional matcher aliases and provenance; `keyword_source` can index it under different text    |
| `query(text, session_id, limit, user_id)`                         | Keyword retrieval; `user_id` selects an isolated caller-owned context                                                  |
| `pattern_query(text, session_id, user_id, combine_sentences)`     | AIML-style match in a user context; returns `(statement, captured, response)` or `()`                                |
| `record_hit(keywords, statement_id)`                             | Update hit statistics after a successful retrieval; the optional `statement_id` credits the answering statement      |
| `learn_from_response(query, response, introduced_by_user_id, source_label)` | Cache a response with optional provenance; re-learning the same question replaces it in place                       |
| `retire_statement(statement_id)`                                 | Deliberately remove a statement (and its pattern) by id                                                              |
| `learn_fact(fact, introduced_by_user_id, source_label)`          | Learn an extracted fact with optional provenance                                                                     |
| `add_fact(text, source_label, tier)`                              | Add one globally shared, unattributed fact without changing user context                                              |
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
uses the final substantive sentence, but a trailing courtesy or acknowledgment
does not hide an earlier question, command, fact, self-introduction, or topic
change. Direct `pattern_query` calls retain the legacy AIML
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
from the cache (`source == "cache"`) without an LLM call.

A catch-all (pure-wildcard) pattern match answering a question is treated as
a shrug, not an answer: the pipeline holds it back, lets retrieval and the
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
never touched.

```bash
engram sync-seed
engram sync-seed --file custom_seed.json
engram sync-seed --prune   # also retire STATIC entries removed from the seed
```

Without `--prune`, sync is an upsert: entries deleted from the seed linger in
the store. With it, the store's STATIC tier mirrors the seed exactly -- only
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
matching first, with questions the patterns cannot answer consulting keyword
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

The agent interface is a FastMCP stdio server. The MCP host starts one process,
and that process retains the same Engram conversation between tool calls:

See [FastMCP integration](documentation/mcp-integration.md) for the complete
tool contract, lifecycle, host configuration, persistence and recovery rules,
and the implemented Regulator-controlled cache interface.

```bash
python -m engram.mcp_server
# After installing the project, this is equivalent:
engram-mcp
```

A typical MCP client entry, run from the repository root, is:

```json
{
  "mcpServers": {
    "engram": {
      "command": "python",
      "args": ["-m", "engram.mcp_server"],
      "cwd": "E:\\current\\engram"
    }
  }
}
```

The server exposes these tools:

- `engram_start` - Create or restore one persistent conversation. The
  caller-owned `user_id` defaults to `"0"`; `initial_bot_text` represents
  Engram's utterance immediately before the first turn.
- `engram_send` - Submit exactly one observed message and return Engram's
  response plus match, timing, context-change, and learning diagnostics.
- `engram_inspect` - Read the current user context, learned facts, provenance,
  and metrics.
- `engram_add_fact` - Add one shared, unattributed fact with an optional opaque
  source label, without changing the conversation context.
- `engram_finish` - Persist an optional store and write JSON and Markdown
  reports while leaving the conversation active.
- `engram_stop` - Persist an optional store and release the conversation. The
  MCP host, not this tool, owns the server process.
- `engram_propose` - Retrieve scoped keyword-cache candidates without recording
  a successful hit or changing response context.
- `engram_resolve` - Commit one accepted or rejected Regulator verdict;
  accepted candidates receive exactly one hit.
- `engram_learn_response` - Cache one non-`IDK` Actor response with namespace,
  context, provenance metadata, and retry-safe request identity.
- `engram_retire_response` - Explicitly remove one globally stale dynamic,
  patternless cache response.

Call `engram_start` once and then `engram_send` once per turn, after observing
the previous response. There is deliberately no batch-send tool. State is
persistent between tool calls while the MCP process lives; pass `store_path`
to `engram_start` when it must also survive process restarts.

FastMCP and the CLI are adapters over the same transport-neutral `EngramCore`.
They do not replace or alter the lower-level programmatic API. They are local
human/agent interfaces; future transports can reuse the core without importing
MCP or CLI code.

Use `engram_send` for completed chatbot turns. For Tapestry, use
`engram_propose` followed by `engram_resolve`; route misses and rejections to
the Actor, then pass eligible answers to `engram_learn_response`. The same
workflow remains available through the Python API when a process boundary is
unnecessary.

Scripted or adaptive soak runners can use `ConversationTurnPlanner` to reserve
the final turn for a farewell, preserve planned messages when adaptive replies
consume spare turns, and reject accidental normalized duplicate inputs. Any
intentional repeat, such as testing name recall twice, must be listed through
`allowed_repeats`.

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

Typos miss patterns and keywords (`abotu` matches nothing). With
`use_spell_correction` (default on), out-of-vocabulary input tokens are
corrected toward the store's own vocabulary before matching, using NLTK's
Damerau-Levenshtein distance (a transposition like `abotu` -> `about` is one
edit). Correction is deliberately timid: only tokens of four or more
characters that are neither store vocabulary nor real English words are
candidates, and only a unique nearest neighbor within distance 1 (2 for
tokens of six or more characters) replaces them. Correcting toward the store
rather than general English means a typo is only ever corrected into a word
that can actually match something.

### Response polish

Template substitution splices lowercase wildcard captures into authored text
("nice to know you're tired i have been working"). With `polish_responses`
(default on), pattern-path responses get mechanical casing repairs: each
sentence starts with a capital letter and the pronoun I (and its
contractions) is capitalized. No punctuation is inserted and no grammar is
rewritten.

Relatedly, the `{clause:...}` template transform trims a capture to its first
clause -- a personal pronoun or non-relative question word followed by a verb
marks the start of a new clause -- so a compound input ("I am tired, I have
been working really hard") echoes back as "tired" instead of the whole tail.
The seed's sentiment pattern uses `{clause:{star1}}` for exactly this, and
`{name:...}` similarly extracts the person name from a self-introduction
capture ("still jason by the way" -> "jason") for the name predicates.

### Question-aware responses

`engram.nlp.is_question` detects questions three ways (trailing `?`, a
question-word lead, an inverted copula) and `input_kind` classifies input as
`question` / `command` / `statement`. Templates branch on the classification
via the `{qtype:...}` transform -- the seed's catch-all uses it to give
questions an honest "I don't have an answer for that yet" instead of a
statement deflection like "Why do you say that?".

The pipeline routes on it too: a question that matches only the catch-all
pattern holds that shrug back, consults keyword retrieval (and the LLM, if
one is wired), and returns the deflection only when nothing better answers.
The interactive CLI chat runs through this routing, so questions consult the
knowledge base before the bot admits it does not know.

### Sentiment-aware responses

Templates can branch on the sentiment of captured input using the
`{sentiment:...}` transform, which returns `positive`, `negative`, or `neutral`
(via NLTK VADER). This lets a single pattern respond with an appropriate tone
instead of enumerating every emotion word:

```json
{
  "pattern": "I AM *",
  "template": {
    "sequence": [
      { "set": { "name": "_mood", "value": "{sentiment:{star1}}" } },
      {
        "condition": {
          "name": "_mood",
          "branches": [
            { "value": "negative", "then": { "text": "I'm sorry to hear you're {star1}. Want to talk about it?" } },
            { "value": "positive", "then": { "text": "That's great that you're {star1}!" } },
            { "then": { "text": "Nice to know you're {star1}." } }
          ]
        }
      }
    ]
  }
}
```

So `I am sad` is met with sympathy while `I am thrilled` is met with cheer,
with no per-emotion patterns.

Predicates with an underscore prefix (like `_mood` above) are template-local
scratch: they are readable within the template that set them but never
persist into the session.

### Relational fact extraction (spaCy)

NLTK has no dependency parser, so the built-in fact extractor
(`engram.nlp.extract_fact`) only handles copula sentences ("X is/are Y"). It is
deliberately conservative: the span before the copula must look like a plain
noun phrase, so subjects longer than four words, subjects containing a verb or
modal ("X should inform that Y is ..."), and subjects or objects carrying
pronouns or possessives are rejected rather than learned as junk facts. A
second conversational admission gate rejects hedged claims, transient claims
(`"Lunch is good today"`), vague subjects, and statements about the current
chat itself. This gate only applies to facts inferred from conversation;
`add_fact` remains an explicit, caller-authorized ingestion API and is not
filtered by conversational heuristics. A
learned fact is protected from overwrites; restating it earns a confirmation
("Yes - The sky is blue.") and contradicting it surfaces the stored belief
("Hmm, I have it differently: The sky is blue.") instead of a silent
deflection. With
spaCy enabled, `engram.facts_spacy.extract_facts` uses the dependency parse to
pull subject-predicate-object triples from arbitrary declaratives:

| Sentence                                    | Triple                                      |
| ------------------------------------------- | ------------------------------------------- |
| Paris is the capital of France              | `(Paris, is, capital of France)`            |
| Paris is in France                          | `(Paris, in, France)`                       |
| Einstein developed the theory of relativity | `(Einstein, develop, theory of relativity)` |
| The book belongs to Mary                    | `(book, belong to, Mary)`                   |

Copulas keep their surface form, prepositional links use the preposition, and
action verbs are normalized to the verb lemma. Each fact also carries
`subject_type`/`obj_type` from NER (`PERSON`/`GPE`/`ORG`/`DATE`, `""` when not an
entity). Automatic learning from conversational input is enabled by default
(`learn_user_facts`) because shared knowledge with explicit user attribution
is the chatbot's normal behavior. Set it to false when the calling application
wants conversation to remain read-only. When enabled, `use_spacy_facts` selects this relational
extractor instead of the conservative copula extractor.
Run `python eval/compare_facts.py` to see it next to the copula extractor.

This is the deliberate spaCy/NLTK split: spaCy for dependency parsing, NLTK for
tokenization, WordNet lemmas/synonyms, and VADER sentiment.

### Optional spaCy matching/retrieval enhancements

All off by default; each is a config flag.

- **`use_spacy_lemmatization`** - lemmatize matcher input with spaCy's
  context-aware lemmatizer instead of the WordNet heuristic, so `saw` (verb)
  resolves to `see` while `saw` (noun) stays `saw`.
- **`use_phrase_keywords`** - extract keywords with spaCy, keeping noun-chunk
  phrases (`machine learning`) as index terms alongside their component lemmas,
  for higher retrieval precision on compound terms.

Note: when a catch-all `*` pattern is present, a pure-wildcard match no longer
blocks the lemma/stem fallbacks - the matcher sets the catch-all aside, tries for
a more specific match, and restores it only if none is found.

(Vector-based semantic matching was prototyped and removed: measured against the
seed, `en_core_web_md` averaged word vectors are too coarse for short-utterance
intent matching - shared question/greeting frames dominate, so paraphrases
mis-route even at high thresholds. It would need sentence embeddings, not word
vectors, to be worthwhile.)

## Knowledge Graph (optional)

ENGRAM can recall facts from a MemGraph knowledge graph in addition to its
statement store. The graph layer is off by default (`graph.enabled: false`) and
uses the pymgclient driver over a host/port. Runtime graph access is strictly
read-only: `execute`, `execute_read`, `Engram.graph_query`, and template
queries all reject mutating Cypher before opening a connection. There is no
runtime writer method or authoring template. Reads degrade gracefully when the
host is unreachable and use a reconnect cooldown.

Facts use the canonical-first model shared with Tapestry: a `Claim` node links
by edge to canonical `Entity` and `Predicate` nodes (`HAS_SUBJECT` /
`USES_PREDICATE` / `HAS_OBJECT`), and the surface triple is also kept as a
denormalized projection on the Claim so a reader sees it without joining edges.
Lookups resolve through the canonical edges (by `primary_label`, `aliases`, or
the edge `surface_form`), never by matching a stored string.

Recalled facts are phrased into natural sentences rather than the wooden
`subject slug object` projection (`engram/phrasing.py`). The connector — copula,
passive, possessive, or bare active — is a grammatical property of each
predicate, and spaCy's morphology picks it (`VerbForm=Fin` → active `owns`,
`Part` behind a preposition → stative `is located in`, a nominal head → the noun
role `'s performer is`), with an article inserted where a noun head needs one
(`is a member of`). So `located_in` reads as "is located in" and `owned_by` as
"was owned by". A small override map keyed by predicate slug corrects spaCy's
few single-token misreads and gives the temporal predicates an idiom
(`date_of_birth` → "was born on"). The frame is derived once per predicate and
memoized; if the spaCy model is unavailable, phrasing degrades to a bare active
frame rather than breaking recall.

Apply the sample schema (`schema.cypher`) before enabling the graph:

```bash
python scripts/setup_schema.py            # uses config.yml graph.host / graph.port
python scripts/setup_schema.py --check    # print the statements without running them
```

`scripts/setup_schema.py` is the explicit administrative schema utility and
is intentionally separate from runtime graph access.

Configure the connection in `config.yml`:

```yaml
graph:
  host: localhost
  port: 7687
  username: ""
  password: ""
  enabled: true
```

Use a database account that is restricted to reads. The password is supplied by
external runtime configuration and is never written into persisted cache state.

The two supported template graph operations are read-only:

- `<triple_query>` — resolve the unknown slot of a triple (`"?"` for subject or
  object). Read; active.
- `<graph_query>` — run a supplied read Cypher. A query carrying
  a write clause (`CREATE` / `MERGE` / `DELETE` / `SET` / `REMOVE` / …) is
  refused, as are `CALL` (stored procedures can mutate) and `LOAD` (data
  import) — the blocklist is conservative, so read-only procedures are refused
  too.

The former `<triple_add>`, `<graph_write>`, and `<graph_delete>` operations
are not supported.

`schema.cypher` is the recall-relevant subset of the Tapestry canonical schema;
the full store (Passage, Document, Event, Proof, Source, Inquiry nodes and the
vector indexes) is a superset ENGRAM does not own.

## Evaluation

`eval/run_eval.py` cycles a corpus of prompts (`eval/corpus.json`) through a
freshly seeded engram instance (in memory; it never touches `engram.json`) and
reports how each prompt is answered:

```bash
python eval/run_eval.py            # human-readable report
python eval/run_eval.py --json report.json
```

Each prompt is classified as a **specific** match (a real, intentional
pattern), **catch-all** (only the `*` fallback matched - a coverage gap), or
**fallback** (no statement). The matched pattern shown for each gap indicates
whether it needs new content or an engine fix.

## Development

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
