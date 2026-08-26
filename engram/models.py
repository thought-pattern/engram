"""Data models for ENGRAM.

Models are plain dicts. Each type has a factory function (named after the type)
that returns a dict, plus module-level helper functions for behaviors that used
to be methods.
"""

from datetime import UTC, datetime
from uuid import uuid4

from engram.constants import NULL_DATETIME, Tier
from engram.substitutions import split_sentences

# =============================================================================
# Statement
# =============================================================================


_DEFAULT_ARGUMENT_LIST = []


def statement(
    text: str,
    tier: Tier = Tier.DYNAMIC,
    keywords=False,
    statement_id=False,
    pattern: str = "",
    pattern_aliases=False,
    that: str = "",
    topic: str = "",
    template=False,
    priority: int = 0,
    introduced_by_user_id: str = "",
    source_label: str = "",
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
        "pattern_aliases": list(pattern_aliases or []),  # Alternate patterns resolving to this statement
        "that": that,  # Pattern to match bot's previous response
        "topic": topic,  # Topic scope constraint
        "template": template or {},  # Structured template (JSON), {} if none
        "priority": priority,  # Override default priority (higher = preferred)
        "introduced_by_user_id": introduced_by_user_id,
        "source_label": source_label,
        "hit_count": 0,  # Number of times this statement was selected
        "query_count": 0,  # Number of times this statement was a candidate
        "last_hit": "",  # Timestamp of most recent hit ("" when never hit)
    }
    return stmt


def statement_hit_rate(stmt: dict) -> float:
    """Calculate hit rate (hits/queries), default 0.5 when undefined."""
    if stmt.get("query_count", 0) == 0:
        return 0.5
    rate = stmt.get("hit_count", 0) / stmt.get("query_count", 0)
    return rate


def record_statement_hit(stmt: dict) -> bool:
    """Record a hit on this statement."""
    stmt["hit_count"] += 1
    stmt["last_hit"] = datetime.now(UTC)
    return False


def record_statement_query(stmt: dict) -> bool:
    """Record that this statement was a candidate in a query."""
    stmt["query_count"] += 1
    return False


def statement_to_dict(stmt: dict) -> dict:
    """Serialize a statement to a JSON-ready dictionary."""
    data = {
        "id": stmt.get("id", ""),
        "text": stmt.get("text", ""),
        "tier": stmt.get("tier", Tier.DYNAMIC).value,
        "created_at": stmt.get("created_at", NULL_DATETIME).isoformat(),
        "keywords": stmt.get("keywords", []),
        "pattern": stmt.get("pattern", ""),
    }
    # Only include optional fields if set
    if stmt.get("that", False):
        data["that"] = stmt.get("that", False)
    if stmt.get("pattern_aliases", []):
        data["pattern_aliases"] = stmt.get("pattern_aliases", [])
    if stmt.get("topic", ""):
        data["topic"] = stmt.get("topic", "")
    if stmt.get("template", ""):
        data["template"] = stmt.get("template", "")
    if stmt.get("priority", 0) != 0:
        data["priority"] = stmt.get("priority", False)
    if stmt.get("introduced_by_user_id", "") != "":
        data["introduced_by_user_id"] = stmt.get("introduced_by_user_id", "")
    if stmt.get("source_label", ""):
        data["source_label"] = stmt.get("source_label", "")
    # Eviction tracking (always include for consistency)
    data["hit_count"] = stmt.get("hit_count", 0)
    data["query_count"] = stmt.get("query_count", 0)
    if stmt.get("last_hit", False):
        data["last_hit"] = stmt.get("last_hit", NULL_DATETIME).isoformat()
    return data


def statement_from_dict(data: dict) -> dict:
    """Deserialize a statement from a dictionary."""
    last_hit_raw = data.get("last_hit", "")
    stmt = {
        "id": data.get("id", ""),
        "text": data.get("text", ""),
        "tier": Tier(data.get("tier", "")),
        "created_at": datetime.fromisoformat(data.get("created_at", False)),
        "keywords": data.get("keywords", []),
        "pattern": data.get("pattern", ""),
        "pattern_aliases": data.get("pattern_aliases", []),
        "that": data.get("that", ""),
        "topic": data.get("topic", ""),
        "template": data.get("template", {}),
        "priority": data.get("priority", 0),
        "introduced_by_user_id": data.get("introduced_by_user_id", ""),
        "source_label": data.get("source_label", ""),
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
    statement_ids=False,
    query_count: int = 0,
    hit_count: int = 0,
) -> dict:
    """Build a keyword index entry dict with retrieval statistics."""
    if statement_ids is None:
        statement_ids = False
    entry = {
        "keyword": keyword,
        "statement_ids": set(statement_ids) if statement_ids is not False else set(),
        "query_count": query_count,
        "hit_count": hit_count,
    }
    return entry


def keyword_entry_hit_rate(entry: dict) -> float:
    """Calculate hit rate, defaulting to 0.5 when undefined."""
    if entry.get("query_count", 0) == 0:
        return 0.5
    rate = entry.get("hit_count", 0) / entry.get("query_count", 0)
    return rate


def keyword_entry_to_dict(entry: dict) -> dict:
    """Serialize a keyword entry to a dictionary."""
    data = {
        "statement_ids": list(entry.get("statement_ids", [])),
        "query_count": entry.get("query_count", 0),
        "hit_count": entry.get("hit_count", 0),
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
    session_id=False,
    metadata=False,
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
        "active_topic": "",
        "entities": [],
        "dialogue_act_history": [],
        "last_fact_admissions": [],
        "input_history": [],
        "response_history": [],
        "that_history": [],
        "history_size": history_size,  # Maximum history entries
    }
    return sess


def session_update_context(
    session: dict,
    previous_response: str,
    user_input=False,
) -> bool:
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
        session.get("response_history", []).insert(0, previous_response)
        if len(session.get("response_history", [])) > session.get("history_size", 0):
            session.get("response_history", []).pop()

        # Update that_history (split into sentences)
        sentences = split_sentences(previous_response.upper())
        session.get("that_history", []).insert(0, sentences)
        if len(session.get("that_history", [])) > session.get("history_size", 0):
            session.get("that_history", []).pop()

    # Update input history
    if user_input:
        session.get("input_history", []).insert(0, user_input)
        if len(session.get("input_history", [])) > session.get("history_size", 0):
            session.get("input_history", []).pop()
    return False


def session_record_input(session: dict, user_input: str) -> bool:
    """Record an input without changing the previous bot response."""
    if not user_input:
        return False
    session["last_active"] = datetime.now(UTC)
    session.get("input_history", []).insert(0, user_input)
    if len(session.get("input_history", [])) > session.get("history_size", 0):
        session.get("input_history", []).pop()
    return False


def session_update_dialogue(
    session: dict,
    dialogue_act: str,
    active_topic: str = "",
    entities: list[dict] = _DEFAULT_ARGUMENT_LIST,
    fact_admissions: list[dict] = _DEFAULT_ARGUMENT_LIST,
) -> bool:
    """Update per-user discourse state independently of response history."""
    if entities is _DEFAULT_ARGUMENT_LIST:
        entities = _DEFAULT_ARGUMENT_LIST.copy()
    if fact_admissions is _DEFAULT_ARGUMENT_LIST:
        fact_admissions = _DEFAULT_ARGUMENT_LIST.copy()
    session.setdefault("active_topic", "")
    session.setdefault("entities", [])
    session.setdefault("dialogue_act_history", [])
    session.setdefault("last_fact_admissions", [])

    session["active_topic"] = active_topic
    session["last_fact_admissions"] = list(fact_admissions or [])
    if dialogue_act:
        session.get("dialogue_act_history", []).insert(0, dialogue_act)
        if len(session.get("dialogue_act_history", [])) > session.get("history_size", 0):
            session.get("dialogue_act_history", []).pop()

    for entity in entities or []:
        entity_text = str(entity.get("text", "")).strip()
        if not entity_text:
            continue
        entity_label = str(entity.get("label", ""))
        matching = [
            existing
            for existing in session.get("entities", [])
            if str(existing.get("text", "")).casefold() == entity_text.casefold()
        ]
        label_priority = {"PROPER_NOUN": 1, "TOPIC": 2, "SUBJECT": 3}
        for existing in matching:
            if label_priority.get(str(existing.get("label", "")), 0) > label_priority.get(entity_label, 0):
                entity_label = str(existing.get("label", ""))
        session["entities"] = [
            existing
            for existing in session.get("entities", [])
            if str(existing.get("text", "")).casefold() != entity_text.casefold()
        ]
        session.get("entities", []).insert(0, {"text": entity_text, "label": entity_label})
    del session.get("entities", [])[20:]
    return False


def session_touch(session: dict) -> bool:
    """Update last_active timestamp."""
    session["last_active"] = datetime.now(UTC)
    return False


def session_clear_predicates(session: dict) -> bool:
    """Clear all predicates except topic."""
    topic = session.get("predicates", {}).get("topic", "")
    session.get("predicates", []).clear()
    if topic:
        session.get("predicates", {})["topic"] = topic
    return False


def session_to_dict(session: dict) -> dict:
    """Serialize a session to a JSON-ready dictionary."""
    data = {
        "session_id": session.get("session_id", ""),
        "previous_response": session.get("previous_response", ""),
        "created_at": session.get("created_at", NULL_DATETIME).isoformat(),
        "last_active": session.get("last_active", NULL_DATETIME).isoformat(),
        "metadata": session.get("metadata", {}),
        "predicates": session.get("predicates", {}),
        "active_topic": session.get("active_topic", ""),
        "entities": session.get("entities", []),
        "dialogue_act_history": session.get("dialogue_act_history", []),
        "last_fact_admissions": session.get("last_fact_admissions", []),
        "input_history": session.get("input_history", []),
        "response_history": session.get("response_history", []),
        "that_history": session.get("that_history", []),
        "history_size": session.get("history_size", 0),
    }
    return data


def session_from_dict(data: dict) -> dict:
    """Deserialize a session from a dictionary."""
    sess = {
        "session_id": data.get("session_id", ""),
        "previous_response": data.get("previous_response", ""),
        "created_at": datetime.fromisoformat(data.get("created_at", False)),
        "last_active": datetime.fromisoformat(data.get("last_active", False)),
        "metadata": data.get("metadata", {}),
        "predicates": data.get("predicates", {}),
        "active_topic": data.get("active_topic", ""),
        "entities": data.get("entities", []),
        "dialogue_act_history": data.get("dialogue_act_history", []),
        "last_fact_admissions": data.get("last_fact_admissions", []),
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
