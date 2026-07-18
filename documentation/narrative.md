# Engram: A Narrative Guide

## Enriching LLM Prompt Pipelines with Keyword-Indexed Knowledge Retrieval

---

## The Problem: LLMs Are Expensive and Stateless

Large Language Models have transformed what's possible in conversational AI, but they come with significant costs:

**Financial cost** - Every API call to GPT-4, Claude, or similar models costs money. A busy application making thousands of calls per hour accumulates substantial bills.

**Latency cost** - LLM inference takes time. Users wait 2-10 seconds for responses that could be instantaneous if the answer were cached.

**Context cost** - LLMs have finite context windows. Stuffing every possible piece of information into the prompt wastes tokens and degrades performance.

**Statelessness** - Each LLM call starts fresh. The model doesn't remember that it answered the same question five minutes ago, or that this user prefers concise responses.

Engram addresses these problems by providing a **fast-path retrieval layer** that sits between user queries and expensive LLM inference.

---

## What Engram Is

Engram is a keyword-indexed statement store with hit-rate tracking. In practical terms:

- It stores text statements (facts, responses, knowledge snippets)
- It indexes them by extracted keywords
- It retrieves relevant statements when queries share keywords
- It tracks which retrievals succeed, learning over time
- It maintains session state across conversation turns

The name "engram" comes from neuroscience, where it refers to a physical trace of memory in the brain. This system serves a similar purpose: it's where your application's memories live between expensive cognitive operations.

---

## The Architecture of a Modern LLM Pipeline

Before understanding where Engram fits, consider a typical LLM application pipeline:

```
User Query
    |
    v
[Preprocessing] --> Normalize, extract intent, validate
    |
    v
[Context Assembly] --> Gather relevant information
    |
    v
[Prompt Construction] --> Build the prompt with context
    |
    v
[LLM Inference] --> Call the model (expensive!)
    |
    v
[Postprocessing] --> Parse, validate, format response
    |
    v
Response to User
```

The **Context Assembly** stage is where most applications struggle. You need to provide the LLM with relevant information, but you can't include everything. RAG (Retrieval-Augmented Generation) systems use vector databases and embeddings to find semantically similar content. This works well but adds complexity and its own costs.

Engram offers an alternative: **keyword-based retrieval with learning**. It's simpler, faster, and for many use cases, equally effective.

---

## Where Engram Fits in the Pipeline

Engram can serve multiple roles in an LLM pipeline:

### Role 1: Fast-Path Cache (Before LLM)

```
User Query
    |
    v
[Engram Query] --> Check for cached response
    |
    +--> Cache Hit (high confidence) --> Return immediately
    |
    +--> Cache Miss --> Continue to LLM
    |
    v
[LLM Inference]
    |
    v
[Engram Learn] --> Cache the response for future queries
    |
    v
Response to User
```

When Engram finds a high-confidence match, the LLM is never called. The response is instantaneous and free. When it doesn't, the LLM response gets cached for next time.

### Role 2: Context Enrichment (Prompt Assembly)

```
User Query
    |
    v
[Engram Query] --> Retrieve relevant knowledge
    |
    v
[Prompt Construction]
    |
    +-- System prompt (static)
    +-- Retrieved context from Engram <-- Relevant facts injected here
    +-- Conversation history
    +-- User query
    |
    v
[LLM Inference]
```

Even when you always call the LLM, Engram can provide relevant context to include in the prompt. Instead of embedding your entire knowledge base, you retrieve just the statements relevant to this query.

### Role 3: Session State Manager

```
User: "Tell me about Paris"
    |
    v
[Engram Session] --> Create/retrieve session
    |
    v
[LLM Response] --> "Paris is the capital of France..."
    |
    v
[Engram Update] --> Store response in session context
    |
    v
User: "What's its population?"
    |
    v
[Engram Expand] --> "its" expanded using session context
    |
    v
[Query becomes] --> "What's Paris/France population?"
```

Engram sessions track conversation state, enabling context expansion for pronouns and references. The LLM doesn't need to carry the full conversation history when Engram can resolve references locally.

### Role 4: Response Validator

```
[LLM Response]
    |
    v
[Engram Query] --> Check if response contradicts known facts
    |
    +--> Conflict detected --> Flag for review or correction
    |
    +--> No conflict --> Pass through
    |
    v
Response to User
```

Your STATIC tier can contain authoritative facts. Before returning an LLM response, query Engram to see if the response contradicts known truths.

---

## The Keyword Approach: Why Not Embeddings?

Vector databases with neural embeddings are powerful, but they have drawbacks:

| Concern       | Embeddings                         | Keywords (Engram)        |
| ------------- | ---------------------------------- | ------------------------ |
| Latency       | Embedding computation + ANN search | Simple index lookup      |
| Debuggability | Opaque similarity scores           | Visible keyword overlap  |
| Determinism   | Varies with model versions         | Consistent results       |
| Cost          | Embedding API calls                | No external calls        |
| Learning      | Requires retraining                | Hit-rate tracking adapts |

Engram's keyword approach is **intentionally simple**. When a user asks "What is the capital of France?", you don't need semantic understanding to know that a statement containing "capital" and "France" is probably relevant.

The hit-rate tracking adds a learning signal. Keywords that consistently lead to successful retrievals get weighted more heavily. The system adapts to your actual usage patterns without any neural network.

---

## Core Concepts in Detail

### Statements

A statement is the atomic unit of storage. It contains:

- **Text**: The content (a fact, response, or knowledge snippet)
- **Keywords**: Automatically extracted from the text
- **Tier**: STATIC (protected) or DYNAMIC (evictable)
- **Pattern**: Optional AIML-style pattern for exact matching
- **Template**: Optional dynamic response template
- **Metrics**: Hit count, query count, timestamps

Statements can be plain text for simple caching, or they can include patterns and templates for scripted conversational flows.

### Keywords and Indexing

When you store a statement, Engram:

1. Normalizes the text (lowercase, remove punctuation)
2. Tokenizes into words
3. Filters out stopwords ("the", "is", "a", etc.)
4. Optionally applies stemming or lemmatization
5. Builds a reverse index: keyword -> statement IDs

Querying reverses this: extract keywords from the query, find statements sharing those keywords, score and rank them.

### The Scoring Algorithm

Candidates are scored on a calibrated 0.0 - 1.0 scale:

```
score = overlap * (weight_base + weight_recency * recency + weight_hit_rate * hit_rate)
        / (weight_base + weight_recency + weight_hit_rate)
        + priority
```

- **Overlap**: The IDF-weighted fraction of query keywords the statement
  carries. Rare keywords count for more than common ones, and a keyword
  matched only through a WordNet synonym earns half credit.
- **Recency**: Exponential time decay of the statement's last activity (its
  last hit, falling back to its creation time), with a configurable half-life
  (default 7 days). Being hit refreshes a statement's recency.
- **Hit rate**: Average success rate of the matched keywords.
- **Priority**: A per-statement override added on top; since calibrated scores
  never exceed 1.0, priority 1 or more outranks every unprioritized match.

The weights are configurable. By default: base=0.5, recency=0.3, hit_rate=0.2.

Because scores are calibrated, thresholds are portable: a full-overlap, fresh,
unproven statement scores 0.9, so 0.7 works as a "answer directly without the
LLM" confidence threshold regardless of query length.

### Hit-Rate Tracking

The learning signal comes from `record_hit()`. When a retrieval successfully answers a query:

```python
result = engram.query("capital of France")
if result["matches"]:
    statement, score = result["matches"][0]
    if user_satisfied(statement):
        engram.record_hit(result["keywords"], statement_id=statement["id"])
```

This increments hit counts on the keywords that led to success, and credits the
statement itself when its id is passed. Over time, high-hit-rate keywords
contribute more to scoring, and the statement-level statistics drive the
hit-rate-aware eviction policies.

Statistics can also be aged: `metrics.decay_statistics(engram, factor=0.5)`
multiplies every hit/query count by the factor, preserving rates while
shrinking confidence. Run it periodically so entries that stop earning their
statistics eventually lose protection and standing, instead of coasting
forever on old evidence.

### Two-Tier Storage

**STATIC tier**: Protected knowledge that defines your domain.

- Product documentation
- FAQ responses
- Authoritative facts
- Never evicted, regardless of capacity

**DYNAMIC tier**: Learned and cached content.

- LLM responses cached for reuse
- User-contributed facts
- Patterns discovered through usage
- Evicted when capacity is reached

Eviction policies for DYNAMIC content:

- **FIFO**: Oldest statement evicted first
- **LRU**: Least recently hit evicted (never-hit statements go first)
- **LFU**: Least frequently hit evicted
- **HIT_RATE**: Lowest success rate (hits/queries) evicted

A `min_hit_rate` threshold can protect proven performers: a DYNAMIC statement
with query history and a hit rate at or above the threshold is skipped by
eviction. Statements with no query history are always evictable.

### Sessions

Sessions enable multi-turn conversations with state:

```python
from engram import sessions

session_id = sessions.create_session(engram)

# First turn
result = engram.query("Tell me about machine learning", session_id=session_id)
statement, score = result["matches"][0]
sessions.update_session_context(engram, session_id, statement["text"])

# Second turn - "it" refers to machine learning
result = engram.query("What are its applications?", session_id=session_id)
```

Sessions track:

- Previous bot responses (for context expansion)
- Predicates (variables like topic, user name, preferences)
- Input/output history
- TTL for automatic expiration

---

## Practical Integration Patterns

The patterns below show the moving parts explicitly. If you just want the
standard tiered flow -- scripted pattern, then confident cache hit, then LLM
with retrieved context -- `engram.pipeline.respond(engram, text, session_id,
llm_fn)` packages Patterns 1, 2, and 5 in one call.

### Pattern 1: The Cache-Aside Pattern

The simplest integration. Query Engram first; if no good match, call the LLM and cache the result.

```python
from engram.core import Engram

engram = Engram()
CONFIDENCE_THRESHOLD = 0.7

def answer_question(query: str) -> str:
    # Check cache first
    result = engram.query(query)

    if result["matches"]:
        statement, score = result["matches"][0]
        if score >= CONFIDENCE_THRESHOLD:
            engram.record_hit(result["keywords"], statement_id=statement["id"])
            return statement["text"]

    # Cache miss - call LLM
    response = call_llm(query)

    # Cache for future queries
    engram.learn_from_response(query, response)

    return response
```

**Benefits**: Immediate cost savings on repeated queries. The cache warms up organically through usage.

`learn_from_response` indexes the response under the _query's_ keywords, so
future phrasings of the same question retrieve it even when the answer shares
no words with the question. Re-learning a question with the same keyword set
replaces the cached entry in place (with fresh, unproven statistics) instead
of accumulating duplicates.

**Tuning**: Adjust `CONFIDENCE_THRESHOLD` based on your tolerance for incorrect cache hits vs. LLM calls. Scores are calibrated 0.0 - 1.0; 0.7 is a sound default.

### Pattern 2: Context Injection

Use Engram to assemble relevant context for the LLM prompt.

```python
def build_prompt(query: str, session_id: str = "") -> str:
    # Retrieve relevant knowledge
    result = engram.query(query, session_id=session_id, limit=5)

    context_statements = [
        stmt["text"] for stmt, score in result["matches"]
        if score > 0.3  # Include moderately relevant content
    ]

    context_block = "\n".join(context_statements)

    prompt = f"""You are a helpful assistant. Use the following context to answer the user's question.

Context:
{context_block}

User question: {query}

Answer:"""

    return prompt
```

**Benefits**: The LLM gets focused, relevant context rather than everything or nothing. Token usage stays bounded.

**Tuning**: Adjust `limit` and the score threshold to control how much context gets included.

### Pattern 3: Hybrid Scripted + Generative

Use pattern matching for predictable queries, LLM for everything else.

```python
from engram import sessions

def respond(user_input: str, session_id: str) -> str:
    # Try pattern matching first (scripted responses). Returns () when nothing
    # matched; on a match it updates the session context itself.
    result = engram.pattern_query(user_input, session_id=session_id)

    if result:
        statement, captured, response = result
        return response

    # No pattern match - use LLM with context
    context = engram.query(user_input, session_id=session_id, limit=3)
    prompt = build_contextual_prompt(user_input, context)

    response = call_llm(prompt)

    # Update session with the LLM response
    sessions.update_session_context(engram, session_id, response)

    return response
```

**Benefits**: Predictable, instant responses for common patterns. LLM handles the long tail. Best of both worlds.

### Pattern 4: Fact Learning and Validation

Automatically extract and store facts from conversations.

```python
from engram.nlp import extract_fact

def process_user_statement(user_input: str, session_id: str) -> str:
    # Check if user is stating a fact ({} when no fact was extracted)
    fact = extract_fact(user_input)

    if fact:
        # User said something like "The sky is blue"
        learned = engram.learn_fact(fact)
        if learned:
            return "I'll remember that."
        else:
            return "Yes, I know."

    # Not a fact statement - process as query
    return respond(user_input, session_id)

def validate_llm_response(response: str) -> tuple[bool, str]:
    """Check if LLM response contradicts known facts."""
    # Extract claims from the response
    fact = extract_fact(response)

    if fact:
        # Query for existing knowledge about this subject
        result = engram.query(fact["subject"], limit=3)

        for stmt, score in result["matches"]:
            if contradicts(stmt["text"], response):
                return False, f"Conflicts with known fact: {stmt['text']}"

    return True, ""
```

**Benefits**: The system accumulates knowledge over time. LLM confabulations can be caught when they contradict the knowledge base.

### Pattern 5: Tiered Response Strategy

Different confidence levels trigger different strategies.

```python
def tiered_response(query: str, session_id: str) -> str:
    result = engram.query(query, session_id=session_id)

    if result["matches"]:
        statement, score = result["matches"][0]

        if score >= 0.9:
            # Very high confidence - return directly
            engram.record_hit(result["keywords"], statement_id=statement["id"])
            return statement["text"]

        elif score >= 0.6:
            # Medium confidence - use as context, let LLM refine
            prompt = f"""Based on this information: "{statement['text']}"

Answer the following question: {query}

Provide a clear, direct answer."""
            return call_llm(prompt)

        elif score >= 0.3:
            # Low confidence - include as one of several context items
            return call_llm_with_context(query, result["matches"])

    # No relevant context - pure LLM
    return call_llm(query)
```

**Benefits**: High-confidence matches avoid LLM entirely. Medium confidence uses cached knowledge as a starting point. Low confidence still benefits from available context.

---

## The AIML Heritage: History and Application

### A Brief History of AIML

AIML (Artificial Intelligence Markup Language) emerged in the late 1990s from Dr. Richard S. Wallace's work on ALICE (Artificial Linguistic Internet Computer Entity). Wallace, building on the legacy of Joseph Weizenbaum's ELIZA (1966), sought to create a more sophisticated and maintainable approach to pattern-based conversational agents.

**The ELIZA Legacy**

ELIZA, created at MIT, demonstrated that simple pattern matching could create surprisingly engaging conversations. Its most famous script, DOCTOR, simulated a Rogerian psychotherapist by recognizing keywords and transforming user input into questions. Despite its simplicity, users often attributed understanding and empathy to the program, a phenomenon now called the "ELIZA effect."

ELIZA's limitation was its ad-hoc implementation. Patterns were hard-coded, making the system difficult to extend or modify.

**ALICE and the Birth of AIML**

Dr. Wallace began developing ALICE in 1995, with AIML formalized around 1998-2001. The key innovation was separating the pattern-matching engine from the knowledge base. AIML provided an XML-based language for defining stimulus-response pairs:

```xml
<category>
    <pattern>WHAT IS YOUR NAME</pattern>
    <template>My name is ALICE.</template>
</category>
```

This separation enabled:

- **Portability**: Knowledge bases could be shared across implementations
- **Maintainability**: Non-programmers could author conversational content
- **Scalability**: ALICE accumulated over 40,000 categories through community contribution

ALICE won the Loebner Prize (an annual Turing test competition) in 2000, 2001, and 2004, demonstrating the effectiveness of the pattern-matching approach.

**The AIML Specification**

AIML 1.0 introduced core concepts still relevant today:

- **Categories**: Pattern-template pairs forming the knowledge base
- **Wildcards**: `*` and `_` for flexible matching
- **Context**: `<that>` for matching based on bot's previous response
- **Topics**: Scoped conversation contexts
- **Recursion**: `<srai>` for symbolic reduction (redirecting to other patterns)
- **Variables**: `<get>` and `<set>` for session state

AIML 2.0 (2014) added:

- **Zero-or-more wildcards**: `#` and `^`
- **Sets and maps**: For vocabulary and lookup tables
- **Rich media**: Support for modern interfaces

**Why Pattern Matching Persists**

Despite advances in neural language models, pattern-based approaches remain valuable:

1. **Determinism**: The same input always produces the same output
2. **Debuggability**: You can trace exactly why a response was given
3. **Control**: Critical responses can be guaranteed, not probabilistic
4. **Speed**: Pattern matching is orders of magnitude faster than inference
5. **Cost**: No API calls, no GPU requirements
6. **Compliance**: Regulated industries can audit and approve responses

Many production chatbots use hybrid architectures: patterns handle known cases deterministically while neural models handle the long tail.

### How Engram Adapts AIML Concepts

Engram inherits AIML's pattern-template architecture but extends it for modern LLM pipelines:

| AIML Concept       | Engram Implementation                              |
| ------------------ | -------------------------------------------------- |
| Categories         | Statements with patterns and templates             |
| Wildcards          | Full support: `*`, `_`, `#`, `^`                   |
| `<that>` context   | Statement `that` field for response-based matching |
| Topics             | Statement `topic` field for scoped matching        |
| `<srai>` recursion | Template redirect with depth limiting              |
| Predicates         | Session predicates with persistence                |
| Sets               | Named sets for vocabulary matching                 |
| Maps               | Named maps for value lookup                        |

**Extensions Beyond AIML:**

- **Hit-rate tracking**: Learning signal absent in traditional AIML
- **Two-tier storage**: STATIC/DYNAMIC distinction for cache management
- **Keyword indexing**: Fast retrieval without pattern enumeration
- **Session expansion**: Context-aware query enhancement
- **Input cleanup**: Typos corrected toward the store's own vocabulary before matching
- **Output polish**: Casing repair and clause trimming of echoed wildcard captures
- **Intent-aware routing**: Question/statement classification (`{qtype:...}`) drives
  the catch-all's tone, and unanswered questions consult keyword retrieval
  before falling back
- **Semantic transforms**: `{sentiment:...}` (VADER tone), `{clause:...}` (first
  clause of a capture), and `{qtype:...}` (intent) let one pattern respond
  appropriately to many inputs
- **LLM integration**: Designed as a complement to, not replacement for, neural models
- **JSON persistence**: Modern serialization replacing XML

### Pattern Matching in Engram

Engram's pattern matching system draws from AIML's mature standard, providing powerful capabilities for scripted interactions.

### Pattern Syntax

Patterns match user input using wildcards:

| Wildcard | Meaning            | Priority |
| -------- | ------------------ | -------- |
| `*`      | One or more words  | Low      |
| `_`      | One or more words  | High     |
| `#`      | Zero or more words | Low      |
| `^`      | Zero or more words | High     |

High-priority wildcards match before low-priority ones, enabling catch-all patterns that defer to more specific matches.

**Examples:**

```python
# Exact match
engram.store("Hello!", pattern="HELLO")

# Capture everything after "MY NAME IS"
engram.store("Nice to meet you!", pattern="MY NAME IS *")

# High-priority emergency pattern
engram.store("Transferring to human agent...", pattern="_ EMERGENCY")

# Match with or without prefix
engram.store("I can help with that.", pattern="# HELP ME")
```

### Template Processing

Templates enable dynamic responses:

```python
# Simple wildcard reference
engram.store(
    text="Nice to meet you, {star1}!",
    pattern="MY NAME IS *"
)
# Input: "My name is Alice" -> Output: "Nice to meet you, alice!"

# Random selection
engram.store(
    text="",
    pattern="HELLO",
    template={
        "random": [
            "Hi there!",
            "Hello!",
            "Greetings!",
            "Hey!"
        ]
    }
)

# Conditional response
engram.store(
    text="",
    pattern="HOW ARE YOU",
    template={
        "condition": {
            "name": "mood",
            "branches": [
                {"value": "happy", "then": {"text": "I'm great, thanks!"}},
                {"value": "sad", "then": {"text": "I've been better."}},
                {"then": {"text": "I'm doing well."}}  # default
            ]
        }
    }
)

# Redirect to another pattern
engram.store(
    text="",
    pattern="BONJOUR",
    template={"redirect": "HELLO"}  # Redirect French greeting to English handler
)
```

### Context Matching: That and Topic

Patterns can match based on conversational context:

```python
# Only match if bot just asked "What is your name?"
engram.store(
    text="Hello, {star1}!",
    pattern="*",
    that="WHAT IS YOUR NAME"
)

# Only match within the "cooking" topic
engram.store(
    text="Preheat to 350 degrees.",
    pattern="WHAT TEMPERATURE",
    topic="cooking"
)
```

This enables sophisticated dialogue flows where the same user input produces different responses based on context.

---

## Persistence and State Management

Engram persists its complete state to JSON, enabling:

- **Restart recovery**: Load the accumulated knowledge after application restart
- **State transfer**: Move trained stores between environments
- **Backup**: Preserve learning progress
- **Analysis**: Inspect the store contents for debugging

```python
from engram import persistence
from engram.core import Engram

# Save to file
persistence.save(engram, "knowledge_base.json")

# Load from file
engram = persistence.load_engram("knowledge_base.json")

# Save/load as JSON strings (for database storage)
json_str = persistence.save_json(engram)
engram = persistence.load_engram_json(json_str)
```

The persisted state includes:

- All statements (STATIC and DYNAMIC)
- Keyword index with hit statistics
- Active sessions
- Bot properties and configuration
- Substitution maps

Connection credentials are not state: graph passwords are deliberately omitted
from persisted cache data and must come from external runtime configuration.

---

## Operational Considerations

### Capacity Planning

Configure capacity based on your expected knowledge base size:

```python
from engram.config import engram_config
from engram.constants import EvictionPolicy
from engram.core import Engram

config = engram_config(
    capacity=50000,  # Max DYNAMIC statements
    eviction_policy=EvictionPolicy.HIT_RATE,  # Evict lowest-performing content
    max_sessions=10000,  # Concurrent user sessions
    session_ttl_seconds=3600,  # 1 hour session timeout
)

engram = Engram(config=config)
```

STATIC statements don't count toward capacity and are never evicted.

### Monitoring

Track key metrics to understand system behavior:

```python
from engram import metrics

data = metrics.get_metrics(engram)
print(f"Statements: {data['statement_count']}")
print(f"  Static: {data['static_count']}")
print(f"  Dynamic: {data['dynamic_count']}")
print(f"Keywords indexed: {data['keyword_count']}")
print(f"Active sessions: {data['session_count']}")
print(f"Total queries: {data['query_count']}")
print(f"Cache hits: {data['hit_count']}")
print(f"Hit rate: {data['hit_rate']:.1%}")
print(f"Evictions: {data['eviction_count']}")
```

A healthy system shows:

- Growing hit rate over time
- Eviction count stabilizing (equilibrium reached)
- Session count within limits

### Thread Safety

Engram guards its core structures with locks:

- Statement storage, indexing, the pattern matcher, and the pattern map
  (one lock, so matching never sees a half-updated matcher)
- Keyword index and statistics
- Session registry
- Top-level metrics counters

The documented flows -- store, query, pattern_query, record_hit, retire,
session operations -- are safe to call from multiple threads; a concurrency
test hammers them in parallel and asserts the index and counter invariants.
Heavy write concurrency serializes on the statement lock rather than running
in parallel.

---

## When Engram Excels

**High-repetition domains**: Customer support, FAQ systems, product information. Users ask similar questions repeatedly.

**Cost-sensitive applications**: When LLM API costs matter, caching common responses provides immediate ROI.

**Latency-sensitive applications**: When users expect instant responses, cached retrieval beats LLM inference.

**Debuggable systems**: When you need to understand why a response was given, keyword matching is transparent.

**Hybrid architectures**: When you want scripted responses for common cases and LLM for the long tail.

**Learning systems**: When you want the system to improve through usage without retraining models.

---

## When to Consider Alternatives

**Semantic search**: If queries require deep understanding beyond keyword overlap, vector databases with embeddings may be more appropriate.

**Highly dynamic content**: If the knowledge base changes constantly and keyword patterns don't stabilize, the learning signal may not help.

**Open-domain Q&A**: If every query is unique and unpredictable, caching provides little benefit.

**Large-scale retrieval**: If you need to search millions of documents, specialized search infrastructure (Elasticsearch, vector databases) may be more appropriate.

---

## Getting Started

### Installation

```bash
git clone https://github.com/thought-pattern/engram.git
cd engram
pip install -r requirements.txt

# One-time data setup (local, gitignored data/nltk_data)
python -m engram.nltk_data
python -m spacy download en_core_web_sm
```

### Minimal Example

```python
from engram import persistence
from engram.constants import Tier
from engram.core import Engram

# Create instance
engram = Engram()

# Load foundational knowledge
engram.store("Paris is the capital of France.", tier=Tier.STATIC)
engram.store("The Eiffel Tower is in Paris.", tier=Tier.STATIC)
engram.store("France is a country in Europe.", tier=Tier.STATIC)

# Query
result = engram.query("What is the capital of France?")
statement, score = result["matches"][0]
print(statement["text"])
# Output: "Paris is the capital of France."

# Record successful retrieval
engram.record_hit(result["keywords"], statement_id=statement["id"])

# Save state
persistence.save(engram, "my_knowledge.json")
```

### LLM Integration Example

```python
from engram.constants import Tier
from engram.core import Engram

engram = Engram()
engram.load_corpus(load_faq_from_file("faq.txt"), tier=Tier.STATIC)

def smart_respond(query: str) -> str:
    result = engram.query(query)

    if result["matches"]:
        statement, score = result["matches"][0]
        if score > 0.7:
            engram.record_hit(result["keywords"], statement_id=statement["id"])
            return statement["text"]

    # Assemble context for LLM
    context = "\n".join(s["text"] for s, _ in result["matches"][:3])

    response = call_your_llm(
        system="Use this context to help answer: " + context,
        user=query
    )

    engram.learn_from_response(query, response)
    return response
```

---

## Summary

Engram provides a practical middle ground between hard-coded responses and expensive neural retrieval. By combining keyword indexing with hit-rate learning, it offers:

- **Speed**: Instant retrieval without embedding computation
- **Economy**: Reduced LLM API calls through effective caching
- **Learning**: Adaptation to usage patterns without retraining
- **Transparency**: Debuggable keyword-based matching
- **Flexibility**: From simple caching to sophisticated dialogue management

For LLM applications where cost, latency, and debuggability matter, Engram provides a robust foundation for the retrieval layer of your prompt pipeline.

---

## Further Reading

- [README.md](README.md) - Quick start guide and API reference
- [engram/core.py](engram/core.py) - Core implementation
- [engram/pattern.py](engram/pattern.py) - AIML-style pattern matching
- [engram/template.py](engram/template.py) - Dynamic template processing
- [engram/scoring.py](engram/scoring.py) - Scoring algorithm details
