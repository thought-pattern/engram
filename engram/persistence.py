"""Persistence functionality for ENGRAM.

This module provides save/load functionality for serializing and
deserializing ENGRAM state to/from JSON files and strings.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engram.config import EngramConfig
    from engram.models import KeywordEntry, Session, Statement


class PersistenceMixin:
    """Mixin class providing persistence methods.

    This mixin is designed to be used with the Engram class and expects
    the following attributes to be present:
    - _statements: list[Statement]
    - _statement_index: dict[str, int]
    - _keywords: dict[str, KeywordEntry]
    - _sessions: dict[str, Session]
    - _bot_properties: dict[str, str]
    - _sets: dict[str, list[str]]
    - _maps: dict[str, dict[str, str]]
    - _substitution_maps: SubstitutionMaps
    - _pattern_matcher: PatternMatcher
    - _pattern_to_statement: dict[str, str]
    - _statement_lock, _keyword_lock, _session_lock: threading.RLock
    - config: EngramConfig
    - PERSISTENCE_VERSION: int
    """

    # Type hints for expected attributes
    _statements: list["Statement"]
    _statement_index: dict[str, int]
    _keywords: dict[str, "KeywordEntry"]
    _sessions: dict[str, "Session"]
    _statement_lock: threading.RLock
    _keyword_lock: threading.RLock
    _session_lock: threading.RLock
    config: "EngramConfig"
    PERSISTENCE_VERSION: int

    def save(self, path: str | Path) -> None:
        """Save complete state to JSON file.

        Args:
            path: File path to write.
        """
        state = self.to_dict()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    def save_json(self) -> str:
        """Serialize complete state to JSON string.

        Returns:
            JSON string representation of the complete state.
        """
        return json.dumps(self.to_dict(), indent=2)

    def to_dict(self) -> dict[str, Any]:
        """Serialize complete state to dictionary.

        Returns:
            Dictionary containing all persistent state.
        """
        with self._statement_lock, self._keyword_lock, self._session_lock:
            return {
                "version": self.PERSISTENCE_VERSION,
                "capacity": self.config.capacity,
                "bot": self._bot_properties.copy(),  # type: ignore
                "sets": {k: list(v) for k, v in self._sets.items()},  # type: ignore
                "maps": {k: dict(v) for k, v in self._maps.items()},  # type: ignore
                "substitutions": {
                    "contractions": dict(self._substitution_maps.contractions),  # type: ignore
                    "person": dict(self._substitution_maps.person),  # type: ignore
                    "person2": dict(self._substitution_maps.person2),  # type: ignore
                    "gender": dict(self._substitution_maps.gender),  # type: ignore
                    "custom": dict(self._substitution_maps.custom),  # type: ignore
                },
                "statements": [s.to_dict() for s in self._statements],
                "keywords": {kw: entry.to_dict() for kw, entry in self._keywords.items()},
                "sessions": [s.to_dict() for s in self._sessions.values()],
            }

    def save_sessions(self, path: str | Path) -> None:
        """Save sessions only to JSON file.

        Useful for persisting session state separately from the knowledge base.

        Args:
            path: File path to write.
        """
        with self._session_lock:
            data = {
                "version": self.PERSISTENCE_VERSION,
                "sessions": [s.to_dict() for s in self._sessions.values()],
            }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load_sessions(self, path: str | Path) -> int:
        """Load sessions from JSON file.

        Adds sessions to the current instance without clearing existing sessions.

        Args:
            path: File path to read.

        Returns:
            Number of sessions loaded.
        """
        from engram.models import Session

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        with self._session_lock:
            for sess_data in data.get("sessions", []):
                sess = Session.from_dict(sess_data)
                self._sessions[sess.session_id] = sess
            return len(data.get("sessions", []))

    def rebuild_index(self) -> None:
        """Rebuild keyword index from statements.

        Warning: This loses keyword statistics. Use for recovery only.
        """
        from engram.models import KeywordEntry

        with self._statement_lock, self._keyword_lock:
            self._keywords.clear()
            for stmt in self._statements:
                for kw in stmt.keywords:
                    if kw not in self._keywords:
                        self._keywords[kw] = KeywordEntry(keyword=kw)
                    self._keywords[kw].add_statement(stmt.id)


def load_engram(
    path: str,
    config=None,
    engram_class=None,
) -> Any:
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


def load_engram_json(
    json_str: str,
    config=None,
    engram_class=None,
) -> Any:
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


def load_engram_from_dict(
    data: dict[str, Any],
    config=None,
    engram_class=None,
) -> Any:
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
    if version != engram_class.PERSISTENCE_VERSION:
        raise ValueError(f"Unsupported persistence version: {version}")

    # Create instance with config
    if config is None:
        config = EngramConfig(capacity=data.get("capacity", 10000))
    instance = engram_class(config=config)

    # Restore bot properties
    if "bot" in data:
        instance._bot_properties.update(data["bot"])

    # Restore sets
    if "sets" in data:
        for name, words in data["sets"].items():
            instance._sets[name] = list(words)

    # Restore maps
    if "maps" in data:
        for name, mapping in data["maps"].items():
            instance._maps[name] = dict(mapping)

    # Restore substitutions
    if "substitutions" in data:
        subs = data["substitutions"]
        if "contractions" in subs:
            instance._substitution_maps.contractions.update(subs["contractions"])
        if "person" in subs:
            instance._substitution_maps.person.update(subs["person"])
        if "person2" in subs:
            instance._substitution_maps.person2.update(subs["person2"])
        if "gender" in subs:
            instance._substitution_maps.gender.update(subs["gender"])
        if "custom" in subs:
            instance._substitution_maps.custom.update(subs["custom"])

    # Restore statements
    for stmt_data in data.get("statements", []):
        stmt = Statement.from_dict(stmt_data)
        instance._statements.append(stmt)
        instance._statement_index[stmt.id] = len(instance._statements) - 1
        # Rebuild pattern matcher with context
        if stmt.pattern:
            instance._pattern_matcher.add_pattern(
                stmt.pattern, stmt.text, that=stmt.that, topic=stmt.topic
            )
            instance._pattern_to_statement[stmt.pattern] = stmt.id

    # Restore keyword index
    for kw, entry_data in data.get("keywords", {}).items():
        instance._keywords[kw] = KeywordEntry.from_dict(kw, entry_data)

    # Restore sessions
    for sess_data in data.get("sessions", []):
        sess = Session.from_dict(sess_data)
        instance._sessions[sess.session_id] = sess

    return instance
