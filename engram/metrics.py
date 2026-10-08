"""Counters and statistics maintenance over one ``Engram`` instance.

These functions read the instance's statements, keyword index, sessions and
counters, and ``decay_statistics`` ages the hit statistics under the instance's
own locks.
"""

from engram.constants import Tier


def get_statement_count(engram) -> int:
    """Number of statements stored."""
    count = len(engram.statements)
    return count


def get_static_count(engram) -> int:
    """Number of STATIC tier statements."""

    count = sum(1 for s in engram.statements if s.get("tier", "") == Tier.STATIC)
    return count


def get_dynamic_count(engram) -> int:
    """Number of DYNAMIC tier statements."""

    count = sum(1 for s in engram.statements if s.get("tier", "") == Tier.DYNAMIC)
    return count


def get_keyword_count(engram) -> int:
    """Number of unique keywords indexed."""
    count = len(engram.keywords)
    return count


def get_session_count(engram) -> int:
    """Number of active sessions."""
    count = len(engram.sessions)
    return count


def get_overall_hit_rate(engram) -> float:
    """Overall hit rate percentage."""
    if engram.query_count == 0:
        result = 0.0
        return result
    rate = engram.hit_count / engram.query_count
    return rate


def get_metrics(engram) -> dict:
    """Get all metrics as a dictionary.

    Args:
        engram: Engram instance.

    Returns:
        Dict with counts and rates for statements, keywords, sessions, etc.
    """
    metrics = {
        "statement_count": get_statement_count(engram),
        "static_count": get_static_count(engram),
        "dynamic_count": get_dynamic_count(engram),
        "keyword_count": get_keyword_count(engram),
        "session_count": get_session_count(engram),
        "query_count": engram.query_count,
        "hit_count": engram.hit_count,
        "eviction_count": engram.eviction_count,
        "hit_rate": get_overall_hit_rate(engram),
    }
    return metrics


def decay_statistics(engram, factor: float = 0.5) -> int:
    """Age keyword and statement hit statistics by a multiplicative factor.

    Calling this periodically ages aggregate evidence counts. LRU eviction uses
    timestamps and is therefore independent of this statistical maintenance.

    Args:
        engram: Engram instance.
        factor: Multiplier applied to every count, 0.0 <= factor < 1.0
            (0.5 halves everything; 0.0 resets all statistics).

    Returns:
        Number of records (keyword entries plus statements) whose counts changed.
    """
    # Validated before any count changes, so a refused factor rewrites nothing.
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not 0.0 <= factor < 1.0:
        raise ValueError("factor must be a number at least 0.0 and below 1.0")

    changed = 0
    with engram.keyword_lock:
        for entry in engram.keywords.values():
            queries = entry.get("query_count", 0)
            hits = entry.get("hit_count", 0)
            new_queries = int(queries * factor)
            new_hits = int(hits * factor)
            if new_queries != queries or new_hits != hits:
                entry["query_count"] = new_queries
                entry["hit_count"] = new_hits
                changed += 1
    with engram.statement_lock:
        for stmt in engram.statements:
            queries = stmt.get("query_count", 0)
            hits = stmt.get("hit_count", 0)
            new_queries = int(queries * factor)
            new_hits = int(hits * factor)
            if new_queries != queries or new_hits != hits:
                stmt["query_count"] = new_queries
                stmt["hit_count"] = new_hits
                changed += 1
    return changed
