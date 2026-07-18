"""Data models for ENGRAM.

Models are plain dicts. Each type has a factory function (named after the type)
that returns a dict, plus module-level helper functions for behaviors that used
to be methods.
"""

from datetime import UTC, datetime
from uuid import uuid4

from engram.constants import Tier
from engram.substitutions import split_sentences

# =============================================================================
# Statement
# =============================================================================


def statement(
    text: str,
    tier: Tier = Tier.DYNAMIC,
    keywords=None,
    statement_id=None,
    pattern: str = "",
    that: str = "",
    topic: str = "",
    template=None,
    priority: int = 0,
) -> dict:
    """Build a Statement dict (atomic unit of storage, a pattern-template pair).

    Generates an ID and creation timestamp. Eviction tracking fields start at zero.
    """
    stmt = {
        "id": statement_id or f"stmt_{uuid4().hex[:12]}",
        "text": text,  # Response text or template
        "tier": tier,
        "created_at": datetime.now(UTC),
        "keywords": keywords or [],
        "pattern": pattern,  # AIML-style pattern for matching
        "that": that,  # Pattern to match bot's previous response
        "topic": topic,  # Topic scope constraint
        "template": template or {},  # Structured template (JSON), {} if none
        "priority": priority,  # Override default priority (higher = preferred)
        "hit_count": 0,  # Number of times this statement was selected
        "query_count": 0,  # Number of times this statement was a candidate
        "last_hit": "",  # Timestamp of most recent hit ("" when never hit)
    }
    return stmt


def statement_hit_rate(stmt: dict) -> float:
    """Calculate hit rate (hits/queries), default 0.5 when undefined."""
    if stmt["query_count"] == 0:
        return 0.5
    rate = stmt["hit_count"] / stmt["query_count"]
    return rate


def record_statement_hit(stmt: dict) -> None:
    """Record a hit on this statement."""
    stmt["hit_count"] += 1
    stmt["last_hit"] = datetime.now(UTC)


def record_statement_query(stmt: dict) -> None:
    """Record that this statement was a candidate in a query."""
    stmt["query_count"] += 1


def statement_to_dict(stmt: dict) -> dict:
    """Serialize a statement to a JSON-ready dictionary."""
    data = {
        "id": stmt["id"],
        "text": stmt["text"],
        "tier": stmt["tier"].value,
        "created_at": stmt["created_at"].isoformat(),
        "keywords": stmt["keywords"],
        "pattern": stmt["pattern"],
    }
    # Only include optional fields if set
    if stmt["that"]:
        data["that"] = stmt["that"]
    if stmt["topic"]:
        data["topic"] = stmt["topic"]
    if stmt["template"]:
        data["template"] = stmt["template"]
    if stmt["priority"] != 0:
        data["priority"] = stmt["priority"]
    # Eviction tracking (always include for consistency)
    data["hit_count"] = stmt["hit_count"]
    data["query_count"] = stmt["query_count"]
    if stmt["last_hit"]:
        data["last_hit"] = stmt["last_hit"].isoformat()
    return data


def statement_from_dict(data: dict) -> dict:
    """Deserialize a statement from a dictionary."""
    last_hit_raw = data.get("last_hit", "")
    stmt = {
        "id": data["id"],
        "text": data["text"],
        "tier": Tier(data["tier"]),
        "created_at": datetime.fromisoformat(data["created_at"]),
        "keywords": data.get("keywords", []),
        "pattern": data.get("pattern", ""),
        "that": data.get("that", ""),
        "topic": data.get("topic", ""),
        "template": data.get("template", {}),
        "priority": data.get("priority", 0),
        "hit_count": data.get("hit_count", 0),
        "query_count": data.get("query_count", 0),
        "last_hit": datetime.fromisoformat(last_hit_raw) if last_hit_raw else "",
    }
    return stmt


# =============================================================================
# KeywordEntry
# =============================================================================


def keyword_entry(
    keyword: str,
    statement_ids=None,
    query_count: int = 0,
    hit_count: int = 0,
) -> dict:
    """Build a keyword index entry dict with retrieval statistics."""
    entry = {
        "keyword": keyword,
        "statement_ids": set(statement_ids) if statement_ids is not None else set(),
        "query_count": query_count,
        "hit_count": hit_count,
    }
    return entry


def keyword_entry_hit_rate(entry: dict) -> float:
    """Calculate hit rate, defaulting to 0.5 when undefined."""
    if entry["query_count"] == 0:
        return 0.5
    rate = entry["hit_count"] / entry["query_count"]
    return rate


def keyword_entry_to_dict(entry: dict) -> dict:
    """Serialize a keyword entry to a dictionary."""
    data = {
        "statement_ids": list(entry["statement_ids"]),
        "query_count": entry["query_count"],
        "hit_count": entry["hit_count"],
    }
    return data


def keyword_entry_from_dict(keyword: str, data: dict) -> dict:
    """Deserialize a keyword entry from a dictionary."""
    entry = {
        "keyword": keyword,
        "statement_ids": set(data.get("statement_ids", [])),
        "query_count": data.get("query_count", 0),
        "hit_count": data.get("hit_count", 0),
    }
    return entry


# =============================================================================
# Session
# =============================================================================


def session(
    session_id=None,
    metadata=None,
    history_size: int = 10,
) -> dict:
    """Build an independent conversation-context Session dict.

    Stores per-user state including predicates (variables), topic, and
    conversation history. Generates an ID and timestamps.
    """
    now = datetime.now(UTC)
    sess = {
        "session_id": session_id or f"sess_{uuid4().hex[:12]}",
        "previous_response": "",  # Bot's most recent response (for that-matching and query expansion)
        "created_at": now,
        "last_active": now,
        "metadata": metadata or {},
        "predicates": {},
        "input_history": [],
        "response_history": [],
        "that_history": [],
        "history_size": history_size,  # Maximum history entries
    }
    return sess


def session_update_context(
    session: dict,
    previous_response: str,
    user_input=None,
) -> None:
    """Update the session's context after a turn.

    Args:
        session: Session dict.
        previous_response: Bot's response text.
        user_input: User's input text (optional).
    """
    session["previous_response"] = previous_response
    session["last_active"] = datetime.now(UTC)

    # Update response history
    if previous_response:
        session["response_history"].insert(0, previous_response)
        if len(session["response_history"]) > session["history_size"]:
            session["response_history"].pop()

        # Update that_history (split into sentences)
        sentences = split_sentences(previous_response.upper())
        session["that_history"].insert(0, sentences)
        if len(session["that_history"]) > session["history_size"]:
            session["that_history"].pop()

    # Update input history
    if user_input:
        session["input_history"].insert(0, user_input)
        if len(session["input_history"]) > session["history_size"]:
            session["input_history"].pop()


def session_touch(session: dict) -> None:
    """Update last_active timestamp."""
    session["last_active"] = datetime.now(UTC)


def session_clear_predicates(session: dict) -> None:
    """Clear all predicates except topic."""
    topic = session["predicates"].get("topic", "")
    session["predicates"].clear()
    if topic:
        session["predicates"]["topic"] = topic


def session_to_dict(session: dict) -> dict:
    """Serialize a session to a JSON-ready dictionary."""
    data = {
        "session_id": session["session_id"],
        "previous_response": session["previous_response"],
        "created_at": session["created_at"].isoformat(),
        "last_active": session["last_active"].isoformat(),
        "metadata": session["metadata"],
        "predicates": session["predicates"],
        "input_history": session["input_history"],
        "response_history": session["response_history"],
        "that_history": session["that_history"],
        "history_size": session["history_size"],
    }
    return data


def session_from_dict(data: dict) -> dict:
    """Deserialize a session from a dictionary."""
    sess = {
        "session_id": data["session_id"],
        "previous_response": data.get("previous_response", ""),
        "created_at": datetime.fromisoformat(data["created_at"]),
        "last_active": datetime.fromisoformat(data["last_active"]),
        "metadata": data.get("metadata", {}),
        "predicates": data.get("predicates", {}),
        "input_history": data.get("input_history", []),
        "response_history": data.get("response_history", []),
        "that_history": data.get("that_history", []),
        "history_size": data.get("history_size", 10),
    }
    return sess


# =============================================================================
# QueryResult
# =============================================================================


def query_result(matches, keywords, resolved_query: str = "") -> dict:
    """Build a query result dict.

    matches: list of (statement, score) pairs. keywords: extracted query
    keywords. resolved_query: context-expanded text used for retrieval.
    """
    result = {
        "matches": matches,
        "keywords": keywords,
        "resolved_query": resolved_query,
    }
    return result
