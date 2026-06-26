# Engram

A keyword-indexed statement store with hit-rate tracking, designed as a fast-path retrieval layer for conversational systems.

## Overview

ENGRAM sits between user queries and expensive computation (LLM inference, database queries, API calls). When a query matches stored statements with sufficient quality, the system returns cached knowledge without invoking downstream resources.

Key features:

- **Keyword matching** - Fast, predictable retrieval using keyword overlap
- **Hit-rate tracking** - Learning signal that improves retrieval over time
- **Two-tier storage** - STATIC (protected) and DYNAMIC (evictable) statements
- **Session support** - Multiple concurrent sessions with context expansion
- **Persistence** - JSON-based save/load with full state preservation

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

# Record successful retrieval
engram.record_hit(result["keywords"])
```

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

## Configuration

```python
from engram.config import engram_config
from engram.constants import SessionOverflow
from engram.core import Engram

config = engram_config(
    capacity=10000,              # Max DYNAMIC statements
    max_sessions=10000,          # Max concurrent sessions
    session_ttl_seconds=86400,   # 24 hour session TTL
    weight_base=0.5,             # Scoring weight: base
    weight_recency=0.3,          # Scoring weight: recency
    weight_hit_rate=0.2,         # Scoring weight: hit rate
    session_overflow=SessionOverflow.LRU,  # LRU eviction when at limit
    use_stemming=True,           # Porter-stemmed fallback matching
    use_lemmatization=True,      # WordNet-lemmatized fallback matching (precise)
    use_synonyms=True,           # WordNet synonym expansion on keyword queries
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

## Scoring Algorithm

Statements are scored using:

```
score = overlap * (weight_base + weight_recency * recency + weight_hit_rate * hit_rate)
```

Where:
- `overlap` - Count of query keywords present in statement
- `recency` - Normalized position (0.0 to 1.0, higher = more recent)
- `hit_rate` - Average hit rate of matched keywords

## API Reference

The data model is plain dicts, and behavior is split between `Engram` methods and
module-level functions (`engram.sessions`, `engram.persistence`, `engram.metrics`).

### Engram methods

| Method | Description |
|--------|-------------|
| `store(text, tier, pattern, template)` | Add a statement |
| `query(text, session_id, limit)` | Keyword retrieval; returns a dict with `matches` (list of `(statement, score)`) and `keywords` |
| `pattern_query(text, session_id)` | AIML-style match; returns `(statement, captured, response)` or `()` |
| `record_hit(keywords)` | Update hit statistics after a successful retrieval |
| `learn_fact(fact)` | Learn an extracted fact |
| `get_statement(statement_id)` | Fetch a statement dict by id (`{}` if absent) |
| `load_corpus(statements, tier)` | Bulk-add statements |
| `fork(...)` | Create a child instance sharing the knowledge base |

### Sessions (`from engram import sessions`)

| Function | Description |
|----------|-------------|
| `create_session(engram, session_id, metadata)` | Create a session, returns its id |
| `get_session(engram, session_id, create_if_missing)` | Retrieve a session dict |
| `update_session_context(engram, session_id, previous_response)` | Update session context |
| `delete_session(engram, session_id)` | Remove a session |
| `expire_sessions(engram, inactive_threshold)` | Remove inactive sessions |
| `list_sessions(engram, active_since)` | List sessions |

### Persistence (`from engram import persistence`)

| Function | Description |
|----------|-------------|
| `save(engram, path)` | Save state to JSON file |
| `load_engram(path)` | Load state from JSON file |
| `save_json(engram)` | Serialize to JSON string |
| `load_engram_json(json_str)` | Deserialize from JSON string |

### Metrics (`from engram import metrics`)

`metrics.get_metrics(engram)` returns a dict with these keys:

| Key | Description |
|-----|-------------|
| `statement_count` | Total statements |
| `static_count` | STATIC tier count |
| `dynamic_count` | DYNAMIC tier count |
| `keyword_count` | Distinct keywords |
| `session_count` | Active sessions |
| `query_count` | Queries performed |
| `hit_count` | Hits recorded |
| `eviction_count` | Evictions |
| `hit_rate` | Hit rate (0.0 to 1.0) |

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
```

### Export

```bash
engram export --static-only -o static_corpus.txt
engram export --dynamic-only
```

### Interactive Mode

```bash
engram interactive
```

Interactive mode is a chat loop (pattern matching, not keyword search). Type a
message to get a response, or use a slash command:
- `/debug` - Toggle debug output
- `/metrics` - Show metrics
- `/topic <name>` - Set the conversation topic
- `/set <name> <value>` - Set a session predicate
- `/get <name>` - Show a session predicate
- `/save` - Save to disk
- `/help` - List commands
- `/quit` - Exit (also `/exit`, `/q`)

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

### Sentiment-aware responses

Templates can branch on the sentiment of captured input using the
`{sentiment:...}` transform, which returns `positive`, `negative`, or `neutral`
(via NLTK VADER). This lets a single pattern respond with an appropriate tone
instead of enumerating every emotion word:

```json
{"pattern": "I AM *", "template": {"sequence": [
  {"set": {"name": "_mood", "value": "{sentiment:{star1}}"}},
  {"condition": {"name": "_mood", "branches": [
    {"value": "negative", "then": {"text": "I'm sorry to hear you're {star1}. Want to talk about it?"}},
    {"value": "positive", "then": {"text": "That's great that you're {star1}!"}},
    {"then": {"text": "Nice to know you're {star1}."}}
  ]}}
]}}
```

So `I am sad` is met with sympathy while `I am thrilled` is met with cheer,
with no per-emotion patterns.

### Relational fact extraction (spaCy)

NLTK has no dependency parser, so the built-in fact extractor
(`engram.nlp.extract_fact`) only handles copula sentences ("X is/are Y"). With
spaCy enabled, `engram.facts_spacy.extract_facts` uses the dependency parse to
pull subject-predicate-object triples from arbitrary declaratives:

| Sentence | Triple |
|----------|--------|
| Paris is the capital of France | `(Paris, is, capital of France)` |
| Paris is in France | `(Paris, in, France)` |
| Einstein developed the theory of relativity | `(Einstein, develop, theory of relativity)` |
| The book belongs to Mary | `(book, belong to, Mary)` |

Copulas keep their surface form, prepositional links use the preposition, and
action verbs are normalized to the verb lemma. Each fact also carries
`subject_type`/`obj_type` from NER (`PERSON`/`GPE`/`ORG`/`DATE`, `""` when not an
entity), so triples can populate typed graph nodes. This is opt-in
(`use_spacy_facts`, default off) and feeds the knowledge-graph triple layer.
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
ruff format .
```

## License

Apache License 2.0
