"""Data models for ENGRAM.

Models are plain dictionaries. Creation paths assign the domain identity and
timestamps needed by live records; decoding paths validate external records.
Module-level functions implement the records' behavior.
"""

from datetime import UTC, datetime
from uuid import uuid4

from engram.constants import EARLIEST_UTC, Tier
from engram.contextual import compact_query_frame_from_dict, compact_query_frame_to_dict
from engram.substitutions import split_sentences

# =============================================================================
# Statement
# =============================================================================


def statement(
    text: str,
    tier: Tier = Tier.DYNAMIC,
    keywords=(),
    statement_id: str = "",
    pattern: str = "",
    pattern_aliases=(),
    that: str = "",
    topic: str = "",
    template=(),
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
        "keywords": list(keywords or ()),
        "pattern": pattern,  # AIML-style pattern for matching
        "pattern_aliases": list(pattern_aliases or ()),  # Alternate patterns resolving to this statement
        "that": that,  # Pattern to match bot's previous response
        "topic": topic,  # Topic scope constraint
        "template": dict(template or ()),  # Structured template (JSON), {} when absent
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
        result = 0.5
        return result
    rate = stmt.get("hit_count", 0) / stmt.get("query_count", 0)
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
        "id": stmt.get("id", ""),
        "text": stmt.get("text", ""),
        "tier": stmt.get("tier", Tier.DYNAMIC).value,
        "created_at": stmt.get("created_at", EARLIEST_UTC).isoformat(),
        "keywords": stmt.get("keywords", []),
        "pattern": stmt.get("pattern", ""),
    }
    # Only include absent-capable fields when they carry a concrete value.
    if stmt.get("that", ""):
        data["that"] = stmt.get("that", "")
    if stmt.get("pattern_aliases", []):
        data["pattern_aliases"] = stmt.get("pattern_aliases", [])
    if stmt.get("topic", ""):
        data["topic"] = stmt.get("topic", "")
    if stmt.get("template", {}):
        data["template"] = stmt.get("template", {})
    if stmt.get("priority", 0) != 0:
        data["priority"] = stmt.get("priority", 0)
    if stmt.get("introduced_by_user_id", ""):
        data["introduced_by_user_id"] = stmt.get("introduced_by_user_id", "")
    if stmt.get("source_label", ""):
        data["source_label"] = stmt.get("source_label", "")
    # Hit and query counters are always serialized; last_hit is included when present.
    data["hit_count"] = stmt.get("hit_count", 0)
    data["query_count"] = stmt.get("query_count", 0)
    last_hit = stmt.get("last_hit", "")
    if last_hit:
        data["last_hit"] = last_hit.isoformat()
    return data


def statement_from_dict(data: dict) -> dict:
    """Deserialize a statement from a dictionary."""
    if not isinstance(data, dict):
        raise ValueError("serialized statement must be an object")
    for name in (
        "keywords",
        "pattern",
        "pattern_aliases",
        "that",
        "topic",
        "template",
        "priority",
        "introduced_by_user_id",
        "source_label",
        "hit_count",
        "query_count",
        "last_hit",
    ):
        if name in data and data.get(name, "") is None:
            raise ValueError(f"serialized statement {name} must not be null")
    last_hit_raw = data.get("last_hit", "")
    stmt = {
        "id": data.get("id", ""),
        "text": data.get("text", ""),
        "tier": Tier(data.get("tier", Tier.DYNAMIC.value)),
        "created_at": datetime.fromisoformat(data.get("created_at", "")),
        "keywords": list(data.get("keywords", ())),
        "pattern": data.get("pattern", ""),
        "pattern_aliases": list(data.get("pattern_aliases", ())),
        "that": data.get("that", ""),
        "topic": data.get("topic", ""),
        "template": dict(data.get("template", {})),
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
    statement_ids=(),
    query_count: int = 0,
    hit_count: int = 0,
) -> dict:
    """Build a keyword index entry dict with retrieval statistics."""
    entry = {
        "keyword": keyword,
        "statement_ids": set(statement_ids or ()),
        "query_count": query_count,
        "hit_count": hit_count,
    }
    return entry


def keyword_entry_hit_rate(entry: dict) -> float:
    """Calculate hit rate, defaulting to 0.5 when undefined."""
    if entry.get("query_count", 0) == 0:
        result = 0.5
        return result
    rate = entry.get("hit_count", 0) / entry.get("query_count", 0)
    return rate


def keyword_entry_to_dict(entry: dict) -> dict:
    """Serialize a keyword entry to a dictionary."""
    data = {
        "statement_ids": list(entry.get("statement_ids", set())),
        "query_count": entry.get("query_count", 0),
        "hit_count": entry.get("hit_count", 0),
    }
    return data


def keyword_entry_from_dict(keyword: str, data: dict) -> dict:
    """Deserialize a keyword entry from a dictionary."""
    entry = {
        "keyword": keyword,
        "statement_ids": set(data.get("statement_ids", []) or ()),
        "query_count": data.get("query_count", 0) or 0,
        "hit_count": data.get("hit_count", 0) or 0,
    }
    return entry


# =============================================================================
# Session
# =============================================================================


def session(
    session_id: str = "",
    metadata=(),
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
        "metadata": dict(metadata or ()),
        "predicates": {},
        "active_topic": "",
        "entities": [],
        "dialogue_act_history": [],
        "last_fact_admissions": [],
        "previous_query_frame": {},
        "query_frame_turn": 0,
        "input_history": [],
        "response_history": [],
        "that_history": [],
        "history_size": history_size,  # Maximum history entries
    }
    return sess


def session_update_context(
    session: dict,
    previous_response: str,
    user_input: str = "",
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


def session_record_input(session: dict, user_input: str) -> bool:
    """Record an input without changing the previous bot response."""
    if not user_input:
        return False
    session["last_active"] = datetime.now(UTC)
    session.get("input_history", []).insert(0, user_input)
    if len(session.get("input_history", [])) > session.get("history_size", 0):
        session.get("input_history", []).pop()
    return True


def session_update_dialogue(
    session: dict,
    dialogue_act: str,
    active_topic: str = "",
    entities=(),
    fact_admissions=(),
) -> None:
    """Update per-user discourse state independently of response history."""
    session.setdefault("active_topic", "")
    session.setdefault("entities", [])
    session.setdefault("dialogue_act_history", [])
    session.setdefault("last_fact_admissions", [])

    session["active_topic"] = active_topic
    session["last_fact_admissions"] = list(fact_admissions or ())
    if dialogue_act:
        session.get("dialogue_act_history", []).insert(0, dialogue_act)
        if len(session.get("dialogue_act_history", [])) > session.get("history_size", 0):
            session.get("dialogue_act_history", []).pop()

    for entity in entities or ():
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


def session_touch(session: dict) -> None:
    """Update last_active timestamp."""
    session["last_active"] = datetime.now(UTC)


def session_clear_predicates(session: dict) -> None:
    """Clear all predicates except topic."""
    topic = session.get("predicates", {}).get("topic", "")
    session.get("predicates", {}).clear()
    if topic:
        session.get("predicates", {})["topic"] = topic


def session_to_dict(session: dict) -> dict:
    """Serialize a session to a JSON-ready dictionary."""
    previous_query_frame = session.get("previous_query_frame", {})
    if not isinstance(previous_query_frame, dict):
        raise ValueError("session previous_query_frame must be an object")
    query_frame_turn = session.get("query_frame_turn", 0)
    if isinstance(query_frame_turn, bool) or not isinstance(query_frame_turn, int) or not 0 <= query_frame_turn <= 1_000_000:
        raise ValueError("session query_frame_turn must be an integer from 0 through 1000000")
    serialized_query_frame = compact_query_frame_to_dict(previous_query_frame) if previous_query_frame else {}
    if previous_query_frame and previous_query_frame["source_turn"] != query_frame_turn:
        raise ValueError("session query frame source_turn must match query_frame_turn")
    data = {
        "session_id": session.get("session_id", ""),
        "previous_response": session.get("previous_response", ""),
        "created_at": session.get("created_at", EARLIEST_UTC).isoformat(),
        "last_active": session.get("last_active", EARLIEST_UTC).isoformat(),
        "metadata": session.get("metadata", {}),
        "predicates": session.get("predicates", {}),
        "active_topic": session.get("active_topic", ""),
        "entities": session.get("entities", []),
        "dialogue_act_history": session.get("dialogue_act_history", []),
        "last_fact_admissions": session.get("last_fact_admissions", []),
        "previous_query_frame": serialized_query_frame,
        "query_frame_turn": query_frame_turn,
        "input_history": session.get("input_history", []),
        "response_history": session.get("response_history", []),
        "that_history": session.get("that_history", []),
        "history_size": session.get("history_size", 0),
    }
    return data


def session_from_dict(data: dict) -> dict:
    """Deserialize a session from a dictionary."""
    if not isinstance(data, dict):
        raise ValueError("serialized session must be an object")
    for name in (
        "previous_response",
        "metadata",
        "predicates",
        "active_topic",
        "entities",
        "dialogue_act_history",
        "last_fact_admissions",
        "previous_query_frame",
        "query_frame_turn",
        "input_history",
        "response_history",
        "that_history",
        "history_size",
    ):
        if name in data and data.get(name, "") is None:
            raise ValueError(f"serialized session {name} must not be null")
    previous_query_frame = data.get("previous_query_frame", {})
    query_frame_turn = data.get("query_frame_turn", 0)
    if not isinstance(previous_query_frame, dict):
        raise ValueError("session previous_query_frame must be an object")
    if isinstance(query_frame_turn, bool) or not isinstance(query_frame_turn, int) or not 0 <= query_frame_turn <= 1_000_000:
        raise ValueError("session query_frame_turn must be an integer from 0 through 1000000")
    decoded_query_frame = compact_query_frame_from_dict(previous_query_frame) if previous_query_frame else {}
    if decoded_query_frame and decoded_query_frame["source_turn"] != query_frame_turn:
        raise ValueError("session query frame source_turn must match query_frame_turn")
    sess = {
        "session_id": data.get("session_id", ""),
        "previous_response": data.get("previous_response", ""),
        "created_at": datetime.fromisoformat(data.get("created_at", "")),
        "last_active": datetime.fromisoformat(data.get("last_active", "")),
        "metadata": dict(data.get("metadata", {})),
        "predicates": dict(data.get("predicates", {})),
        "active_topic": data.get("active_topic", ""),
        "entities": list(data.get("entities", ())),
        "dialogue_act_history": list(data.get("dialogue_act_history", ())),
        "last_fact_admissions": list(data.get("last_fact_admissions", ())),
        "previous_query_frame": decoded_query_frame,
        "query_frame_turn": query_frame_turn,
        "input_history": list(data.get("input_history", ())),
        "response_history": list(data.get("response_history", ())),
        "that_history": list(data.get("that_history", ())),
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
