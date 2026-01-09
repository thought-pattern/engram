"""Persistence functionality for ENGRAM.

This module provides save/load functionality for serializing and
deserializing ENGRAM state to/from JSON files and strings.
"""

from __future__ import annotations

import json
from pathlib import Path

# Version constant for persistence format
PERSISTENCE_VERSION = 1


def save(engram, path: str | Path) -> None:
    """Save complete state to JSON file.

    Args:
        engram: Engram instance.
        path: File path to write.
    """
    state = to_dict(engram)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def save_json(engram) -> str:
    """Serialize complete state to JSON string.

    Args:
        engram: Engram instance.

    Returns:
        JSON string representation of the complete state.
    """
    return json.dumps(to_dict(engram), indent=2)


def to_dict(engram) -> dict:
    """Serialize complete state to dictionary.

    Args:
        engram: Engram instance.

    Returns:
        Dictionary containing all persistent state.
    """
    with engram.statement_lock, engram.keyword_lock, engram.session_lock:
        return {
            "version": PERSISTENCE_VERSION,
            "capacity": engram.config.capacity,
            "bot": engram.bot_properties.copy(),
            "sets": {k: list(v) for k, v in engram.sets.items()},
            "maps": {k: dict(v) for k, v in engram.maps.items()},
            "substitutions": {
                "contractions": dict(engram.substitution_maps.contractions),
                "person": dict(engram.substitution_maps.person),
                "person2": dict(engram.substitution_maps.person2),
                "gender": dict(engram.substitution_maps.gender),
                "custom": dict(engram.substitution_maps.custom),
            },
            "statements": [s.to_dict() for s in engram.statements],
            "keywords": {kw: entry.to_dict() for kw, entry in engram.keywords.items()},
            "sessions": [s.to_dict() for s in engram.sessions.values()],
        }


def save_sessions(engram, path: str | Path) -> None:
    """Save sessions only to JSON file.

    Useful for persisting session state separately from the knowledge base.

    Args:
        engram: Engram instance.
        path: File path to write.
    """
    with engram.session_lock:
        data = {
            "version": PERSISTENCE_VERSION,
            "sessions": [s.to_dict() for s in engram.sessions.values()],
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_sessions(engram, path: str | Path) -> int:
    """Load sessions from JSON file.

    Adds sessions to the current instance without clearing existing sessions.

    Args:
        engram: Engram instance.
        path: File path to read.

    Returns:
        Number of sessions loaded.
    """
    from engram.models import Session

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    with engram.session_lock:
        for sess_data in data.get("sessions", []):
            sess = Session.from_dict(sess_data)
            engram.sessions[sess.session_id] = sess
        return len(data.get("sessions", []))


def rebuild_index(engram) -> None:
    """Rebuild keyword index from statements.

    Warning: This loses keyword statistics. Use for recovery only.

    Args:
        engram: Engram instance.
    """
    from engram.models import KeywordEntry

    with engram.statement_lock, engram.keyword_lock:
        engram.keywords.clear()
        for stmt in engram.statements:
            for kw in stmt.keywords:
                if kw not in engram.keywords:
                    engram.keywords[kw] = KeywordEntry(keyword=kw)
                engram.keywords[kw].statement_ids.add(stmt.id)


def load_engram(path: str, config=None, engram_class=None):
    """Load ENGRAM state from JSON file.

    Args:
        path: File path to read.
        config: Optional configuration override.
        engram_class: The Engram class to instantiate (default: imported from core).

    Returns:
        Engram instance with restored state.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return load_engram_from_dict(data, config, engram_class)


def load_engram_json(json_str: str, config=None, engram_class=None):
    """Load ENGRAM state from JSON string.

    Args:
        json_str: JSON string.
        config: Optional configuration override.
        engram_class: The Engram class to instantiate (default: imported from core).

    Returns:
        Engram instance with restored state.
    """
    data = json.loads(json_str)
    return load_engram_from_dict(data, config, engram_class)


def load_engram_from_dict(data: dict, config=None, engram_class=None):
    """Deserialize ENGRAM state from dictionary.

    Args:
        data: Dictionary containing serialized state.
        config: Optional configuration override.
        engram_class: The Engram class to instantiate (default: imported from core).

    Returns:
        Engram instance with restored state.

    Raises:
        ValueError: If persistence version is unsupported.
    """
    from engram.config import EngramConfig
    from engram.models import KeywordEntry, Session, Statement

    if engram_class is None:
        from engram.core import Engram
        engram_class = Engram

    version = data.get("version", 1)
    if version != PERSISTENCE_VERSION:
        raise ValueError(f"Unsupported persistence version: {version}")

    # Create instance with config
    if config is None:
        config = EngramConfig(capacity=data.get("capacity", 10000))
    instance = engram_class(config=config)

    # Restore bot properties
    if "bot" in data:
        instance.bot_properties.update(data["bot"])

    # Restore sets
    if "sets" in data:
        for name, words in data["sets"].items():
            instance.sets[name] = list(words)

    # Restore maps
    if "maps" in data:
        for name, mapping in data["maps"].items():
            instance.maps[name] = dict(mapping)

    # Restore substitutions
    if "substitutions" in data:
        subs = data["substitutions"]
        if "contractions" in subs:
            instance.substitution_maps.contractions.update(subs["contractions"])
        if "person" in subs:
            instance.substitution_maps.person.update(subs["person"])
        if "person2" in subs:
            instance.substitution_maps.person2.update(subs["person2"])
        if "gender" in subs:
            instance.substitution_maps.gender.update(subs["gender"])
        if "custom" in subs:
            instance.substitution_maps.custom.update(subs["custom"])

    # Restore statements
    for stmt_data in data.get("statements", []):
        stmt = Statement.from_dict(stmt_data)
        instance.statements.append(stmt)
        instance.statement_index[stmt.id] = len(instance.statements) - 1
        # Rebuild pattern matcher with context
        if stmt.pattern:
            instance.pattern_matcher.add_pattern(
                stmt.pattern, stmt.text, that=stmt.that, topic=stmt.topic
            )
            instance.pattern_to_statement[stmt.pattern] = stmt.id

    # Restore keyword index
    for kw, entry_data in data.get("keywords", {}).items():
        instance.keywords[kw] = KeywordEntry.from_dict(kw, entry_data)

    # Restore sessions
    for sess_data in data.get("sessions", []):
        sess = Session.from_dict(sess_data)
        instance.sessions[sess.session_id] = sess

    return instance
