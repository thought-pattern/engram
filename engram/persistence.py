"""Persistence functionality for ENGRAM.

This module provides save/load functionality for serializing and
deserializing ENGRAM state to/from JSON files and strings.
"""

from contextlib import suppress as contextlib_suppress
from json import dump as json_dump, dumps as json_dumps, load as json_load, loads as json_loads
from os import chmod as os_chmod, replace as os_replace

from engram.config import config_from_dict, config_to_dict, engram_config
from engram.constants import PERSISTENCE_VERSION
from engram.core import Engram
from engram.models import (
    keyword_entry,
    keyword_entry_from_dict,
    keyword_entry_to_dict,
    session_from_dict,
    session_to_dict,
    statement_from_dict,
    statement_to_dict,
)


def _write_json_atomic(path, state: dict) -> bool:
    """Write JSON to path atomically via a temp file and rename.

    A crash mid-write leaves the previous file intact instead of a truncated
    store; os.replace is atomic on POSIX and Windows.
    """
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json_dump(state, f, indent=2)
    with contextlib_suppress(OSError):
        os_chmod(tmp_path, 0o600)
    os_replace(tmp_path, path)
    with contextlib_suppress(OSError):
        os_chmod(path, 0o600)
    return False


def save(engram, path) -> bool:
    """Save complete state to JSON file (atomically).

    Args:
        engram: Engram instance.
        path: File path to write.
    """
    state = to_dict(engram)
    _write_json_atomic(path, state)
    return False


def save_json(engram) -> str:
    """Serialize complete state to JSON string.

    Args:
        engram: Engram instance.

    Returns:
        JSON string representation of the complete state.
    """
    json_str = json_dumps(to_dict(engram), indent=2)
    return json_str


def to_dict(engram) -> dict:
    """Serialize complete state to dictionary.

    Args:
        engram: Engram instance.

    Returns:
        Dictionary containing all persistent state.
    """

    with engram.statement_lock, engram.keyword_lock, engram.session_lock:
        state = {
            "version": PERSISTENCE_VERSION,
            # Full configuration, so weights, eviction policy, and feature
            # flags survive a save/load cycle. The top-level "capacity" key is
            # kept alongside for files read by older loaders.
            "config": config_to_dict(engram.config),
            "capacity": engram.config.get("capacity", False),
            "query_count": engram.query_count,
            "hit_count": engram.hit_count,
            "eviction_count": engram.eviction_count,
            "bot": engram.bot_properties.copy(),
            "sets": {k: list(v) for k, v in engram.sets.items()},
            "maps": {k: dict(v) for k, v in engram.maps.items()},
            "substitutions": {
                "contractions": dict(engram.substitution_maps.get("contractions", {})),
                "person": dict(engram.substitution_maps.get("person", {})),
                "person2": dict(engram.substitution_maps.get("person2", {})),
                "gender": dict(engram.substitution_maps.get("gender", {})),
                "custom": dict(engram.substitution_maps.get("custom", {})),
            },
            "statements": [statement_to_dict(s) for s in engram.statements],
            "keywords": {kw: keyword_entry_to_dict(entry) for kw, entry in engram.keywords.items()},
            "sessions": [session_to_dict(s) for s in engram.sessions.values()],
        }
        return state
    return {}


def save_sessions(engram, path) -> bool:
    """Save sessions only to JSON file.

    Useful for persisting session state separately from the knowledge base.

    Args:
        engram: Engram instance.
        path: File path to write.
    """

    with engram.session_lock:
        data = {
            "version": PERSISTENCE_VERSION,
            "sessions": [session_to_dict(s) for s in engram.sessions.values()],
        }
    _write_json_atomic(path, data)
    return False


def load_sessions(engram, path) -> int:
    """Load sessions from JSON file.

    Adds sessions to the current instance without clearing existing sessions.

    Args:
        engram: Engram instance.
        path: File path to read.

    Returns:
        Number of sessions loaded.
    """

    with open(path, encoding="utf-8") as f:
        data = json_load(f)

    with engram.session_lock:
        for sess_data in data.get("sessions", []):
            sess = session_from_dict(sess_data)
            engram.sessions[sess.get("session_id", "")] = sess
        loaded_count = len(data.get("sessions", []))
        return loaded_count
    return 0


def rebuild_index(engram) -> bool:
    """Rebuild keyword index from statements.

    Warning: This loses keyword statistics. Use for recovery only.

    Args:
        engram: Engram instance.
    """

    with engram.statement_lock, engram.keyword_lock:
        engram.keywords.clear()
        for stmt in engram.statements:
            for kw in stmt.get("keywords", []):
                if kw not in engram.keywords:
                    engram.keywords[kw] = keyword_entry(keyword=kw)
                engram.keywords[kw].get("statement_ids", set()).add(stmt.get("id", ""))
    return False


def load_engram(path, config=False, engram_class=False):
    """Load ENGRAM state from JSON file.

    Args:
        path: File path to read.
        config: Optional configuration override.
        engram_class: The Engram class to instantiate (default: imported from core).

    Returns:
        Engram instance with restored state.
    """
    with open(path, encoding="utf-8") as f:
        data = json_load(f)
    instance = load_engram_from_dict(data, config, engram_class)
    return instance


def load_engram_json(json_str: str, config=False, engram_class=False):
    """Load ENGRAM state from JSON string.

    Args:
        json_str: JSON string.
        config: Optional configuration override.
        engram_class: The Engram class to instantiate (default: imported from core).

    Returns:
        Engram instance with restored state.
    """
    data = json_loads(json_str)
    instance = load_engram_from_dict(data, config, engram_class)
    return instance


def load_engram_from_dict(data: dict, config=False, engram_class=False):
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

    if config is None:
        config = False
    if engram_class is None:
        engram_class = False
    if engram_class is False:
        engram_class = Engram

    version = data.get("version", 1)
    if version != PERSISTENCE_VERSION:
        raise ValueError(f"Unsupported persistence version: {version}")

    # Create instance with config: an explicit override wins, then the config
    # stored with the state, then defaults (older files carried only capacity).
    if config is False and "config" in data:
        config = config_from_dict(data.get("config", {}))
    if config is False:
        config = engram_config(capacity=data.get("capacity", 10000))
    instance = engram_class(config=config)

    # Restore global counters
    instance.query_count = data.get("query_count", 0)
    instance.hit_count = data.get("hit_count", 0)
    instance.eviction_count = data.get("eviction_count", 0)

    # Restore bot properties
    if "bot" in data:
        instance.bot_properties.update(data.get("bot", False))

    # Restore sets
    if "sets" in data:
        for name, words in data.get("sets", {}).items():
            instance.sets[name] = list(words)

    # Restore maps
    if "maps" in data:
        for name, mapping in data.get("maps", {}).items():
            instance.maps[name] = dict(mapping)

    # Restore substitutions
    if "substitutions" in data:
        subs = data.get("substitutions", [])
        if "contractions" in subs:
            instance.substitution_maps.get("contractions", {}).update(subs.get("contractions", []))
        if "person" in subs:
            instance.substitution_maps.get("person", {}).update(subs.get("person", False))
        if "person2" in subs:
            instance.substitution_maps.get("person2", {}).update(subs.get("person2", False))
        if "gender" in subs:
            instance.substitution_maps.get("gender", {}).update(subs.get("gender", False))
        if "custom" in subs:
            instance.substitution_maps.get("custom", {}).update(subs.get("custom", False))

    # Restore statements
    for stmt_data in data.get("statements", []):
        stmt = statement_from_dict(stmt_data)
        if stmt.get("id", "") in instance.statement_index:
            raise ValueError(f"duplicate statement id in persisted data: {stmt.get('id', '')}")
        instance.statements.append(stmt)
        instance.statement_index[stmt.get("id", "")] = len(instance.statements) - 1
        # Rebuild pattern matcher with context
        if stmt.get("pattern", ""):
            for registered_pattern in [stmt.get("pattern", ""), *stmt.get("pattern_aliases", [])]:
                instance.pattern_matcher.add_pattern(
                    registered_pattern,
                    stmt.get("text", ""),
                    that=stmt.get("that", False),
                    topic=stmt.get("topic", ""),
                )
                instance.pattern_to_statement[registered_pattern] = stmt.get("id", "")

    # Restore keyword index
    for kw, entry_data in data.get("keywords", {}).items():
        instance.keywords[kw] = keyword_entry_from_dict(kw, entry_data)

    # Restore sessions
    for sess_data in data.get("sessions", []):
        sess = session_from_dict(sess_data)
        instance.sessions[sess.get("session_id", "")] = sess

    return instance
