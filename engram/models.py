"""Data models for ENGRAM.

Models are plain dictionaries. Creation paths take the domain identity and
timestamps from the caller, which owns the identifier source and clock;
decoding paths validate external records. Module-level functions implement
the records' behavior.
"""

from datetime import datetime
from uuid import uuid4

from engram.constants import (
    DIALOGUE_ENTITY_LABEL_PRIORITY,
    EARLIEST_UTC,
    MAX_SESSION_ENTITIES,
    NEUTRAL_HIT_RATE,
    Tier,
)
from engram.substitutions import split_sentences


def random_identifier() -> str:
    """Return a fresh 12-hex-digit identifier, the default identifier source of ``Engram``."""
    result = uuid4().hex[:12]
    return result


# =============================================================================
# Statement
# =============================================================================


def statement(
    text: str,
    *,
    statement_id: str,
    created_at: datetime,
    tier: Tier = Tier.DYNAMIC,
    keywords=(),
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

    The caller supplies the ID and creation time. Eviction tracking fields start at
    zero; ``last_hit`` holds ``EARLIEST_UTC`` until ``last_hit_available`` records
    the first hit.
    """
    stmt = {
        "id": statement_id,
        "text": text,  # Response text or template
        "tier": tier,
        "created_at": created_at,
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
        "last_hit": EARLIEST_UTC,  # Time of the most recent hit, when last_hit_available
        "last_hit_available": False,
    }
    return stmt


def record_statement_hit(stmt: dict, moment: datetime) -> None:
    """Record a hit on this statement at ``moment``."""
    stmt["hit_count"] = stmt.get("hit_count", 0) + 1
    stmt["last_hit"] = moment
    stmt["last_hit_available"] = True


def record_statement_query(stmt: dict) -> None:
    """Record that this statement was a candidate in a query."""
    stmt["query_count"] = stmt.get("query_count", 0) + 1


# =============================================================================
# KeywordEntry
# =============================================================================


def keyword_entry_hit_rate(entry: dict) -> float:
    """Calculate hit rate, defaulting to NEUTRAL_HIT_RATE when undefined."""
    if entry.get("query_count", 0) == 0:
        result = NEUTRAL_HIT_RATE
        return result
    rate = entry.get("hit_count", 0) / entry.get("query_count", 0)
    return rate


# =============================================================================
# Session
# =============================================================================


def session(
    session_id: str,
    created_at: datetime,
    metadata=(),
    history_size: int = 10,
) -> dict:
    """Build an independent conversation-context Session dict.

    Stores per-user state including predicates (variables), topic, and
    conversation history. The caller supplies the ID and creation time.
    """
    sess = {
        "session_id": session_id,
        "previous_response": "",  # Bot's most recent response (for that-matching and query expansion)
        "created_at": created_at,
        "last_active": created_at,
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
    *,
    moment: datetime,
) -> None:
    """Update the session's context after a turn.

    Args:
        session: Session dict.
        previous_response: Bot's response text.
        user_input: User's input text (optional).
        moment: The caller's time of the turn.
    """
    session["previous_response"] = previous_response
    session["last_active"] = moment
    history_size = session.get("history_size", 0)

    # Update response history
    if previous_response:
        response_history = session.setdefault("response_history", [])
        response_history.insert(0, previous_response)
        if len(response_history) > history_size:
            response_history.pop()

        # Update that_history (split into sentences)
        sentences = split_sentences(previous_response.upper())
        that_history = session.setdefault("that_history", [])
        that_history.insert(0, sentences)
        if len(that_history) > history_size:
            that_history.pop()

    # Update input history
    if user_input:
        input_history = session.setdefault("input_history", [])
        input_history.insert(0, user_input)
        if len(input_history) > history_size:
            input_history.pop()


def session_record_input(session: dict, user_input: str, moment: datetime) -> bool:
    """Record an input at ``moment`` without changing the previous bot response."""
    recorded = bool(user_input)
    if recorded:
        session["last_active"] = moment
        input_history = session.setdefault("input_history", [])
        input_history.insert(0, user_input)
        if len(input_history) > session.get("history_size", 0):
            input_history.pop()
    return recorded


def session_update_dialogue(
    session: dict,
    dialogue_act: str,
    active_topic: str = "",
    entities=(),
    fact_admissions=(),
) -> None:
    """Update per-user discourse state independently of response history."""
    session.setdefault("entities", [])

    session["active_topic"] = active_topic
    session["last_fact_admissions"] = list(fact_admissions or ())
    dialogue_act_history = session.setdefault("dialogue_act_history", [])
    if dialogue_act:
        dialogue_act_history.insert(0, dialogue_act)
        if len(dialogue_act_history) > session.get("history_size", 0):
            dialogue_act_history.pop()

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
        for existing in matching:
            existing_priority = DIALOGUE_ENTITY_LABEL_PRIORITY.get(str(existing.get("label", "")), 0)
            if existing_priority > DIALOGUE_ENTITY_LABEL_PRIORITY.get(entity_label, 0):
                entity_label = str(existing.get("label", ""))
        retained_entities = [
            existing
            for existing in session.get("entities", [])
            if str(existing.get("text", "")).casefold() != entity_text.casefold()
        ]
        retained_entities.insert(0, {"text": entity_text, "label": entity_label})
        session["entities"] = retained_entities
    del session.setdefault("entities", [])[MAX_SESSION_ENTITIES:]


def session_touch(session: dict, moment: datetime) -> None:
    """Set the session's last_active time to ``moment``."""
    session["last_active"] = moment
