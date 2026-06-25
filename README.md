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
```

ENGRAM uses several NLTK datasets (punkt, averaged_perceptron_tagger,
maxent_ne_chunker, words, wordnet, omw-1.4, vader_lexicon). They are managed
centrally by `engram/nltk_data.py`, which stores them in `data/nltk_data`
(gitignored) and puts that directory first on NLTK's search path. If a dataset
is missing at runtime it is fetched on demand as a fallback, but pre-fetching
keeps normal operation offline and fast.

## Quick Start

```python
from engram.core import Engram
from engram.models import Tier

# Create an instance
engram = Engram()

# Store statements
engram.store("Paris is the capital of France", tier=Tier.STATIC)
engram.store("France has a population of 67 million", tier=Tier.STATIC)

# Query
result = engram.query("What is the capital of France?")
print(result.top_match.text)  # "Paris is the capital of France"

# Record successful retrieval
engram.record_hit(result.keywords)
```

## Sessions

Sessions enable context expansion for follow-up queries:

```python
# Create a session
session_id = engram.create_session()

# First query
result = engram.query("What is the capital of France?", session_id=session_id)
engram.update_session_context(session_id, result.top_match.text)

# Follow-up query - context expands "its" to include France/Paris
result = engram.query("What is its population?", session_id=session_id)
```

## Configuration

```python
from engram import Engram, EngramConfig, SessionOverflow

config = EngramConfig(
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
# Save to file
engram.save("engram_state.json")

# Load from file
engram = Engram.load("engram_state.json")

# Or use JSON strings
json_str = engram.save_json()
engram = Engram.load_json(json_str)
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

### Statement Operations

| Method | Description |
|--------|-------------|
| `store(text, tier)` | Add a statement |
| `query(text, session_id, limit)` | Retrieve matching statements |
| `record_hit(keywords)` | Update statistics after successful retrieval |
| `evict()` | Remove oldest DYNAMIC statement |
| `clear_dynamic()` | Remove all DYNAMIC statements |

### Session Operations

| Method | Description |
|--------|-------------|
| `create_session(session_id, metadata)` | Create a new session |
| `get_session(session_id, create_if_missing)` | Retrieve a session |
| `update_session_context(session_id, previous_response)` | Update session context |
| `delete_session(session_id)` | Remove a session |
| `expire_sessions(inactive_threshold)` | Remove inactive sessions |
| `list_sessions(active_since)` | List sessions |

### Persistence

| Method | Description |
|--------|-------------|
| `save(path)` | Save state to JSON file |
| `load(path)` | Load state from JSON file |
| `save_json()` | Serialize to JSON string |
| `load_json(json_str)` | Deserialize from JSON string |

### Metrics

| Property | Description |
|----------|-------------|
| `statement_count` | Total statements |
| `static_count` | STATIC tier count |
| `dynamic_count` | DYNAMIC tier count |
| `keyword_count` | Distinct keywords |
| `session_count` | Active sessions |
| `total_queries` | Queries performed |
| `total_hits` | Hits recorded |
| `overall_hit_rate` | Hit percentage |

## Command Line Interface

ENGRAM includes a CLI for managing stores from the terminal.

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
# Load statements from a file (one per line)
engram load corpus.txt --static
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

In interactive mode:
- Type queries directly to search
- `/store <text>` - Store a new statement
- `/hit` - Record last query as a hit
- `/context [text]` - View or set session context
- `/metrics` - Show metrics
- `/save` - Save to disk
- `/quit` - Exit

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
