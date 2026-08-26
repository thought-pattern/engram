"""Eviction policies for ENGRAM.

This module provides eviction functionality for managing the capacity
of the statement store by removing DYNAMIC tier statements based on
various policies (FIFO, LRU, LFU, HIT_RATE).
"""

from datetime import datetime

from engram.constants import EvictionPolicy, Tier
from engram.models import statement_hit_rate


def get_eviction_candidates(engram) -> list[tuple[int, dict]]:
    """Get DYNAMIC statements eligible for eviction.

    Filters out statements that are protected by min_hit_rate threshold.

    Args:
        engram: Engram instance.

    Returns:
        List of (index, statement) tuples for eviction candidates.
    """

    candidates = []
    for idx, stmt in enumerate(engram.statements):
        if stmt.get("tier", "") == Tier.DYNAMIC:
            # min_hit_rate protects proven performers only. A statement with no
            # query history has no evidence either way (its hit rate defaults to
            # 0.5) and stays evictable -- otherwise any threshold below 0.5
            # would protect every untouched statement and disable eviction.
            if (
                engram.config.get("min_hit_rate", 0) > 0
                and stmt.get("query_count", 0) > 0
                and statement_hit_rate(stmt) >= engram.config.get("min_hit_rate", 0.0)
            ):
                continue
            candidates.append((idx, stmt))
    return candidates


def evict_statement_at(engram, idx: int) -> bool:
    """Evict statement at given index.

    Removes the statement from the store and updates all indices.

    Args:
        engram: Engram instance.
        idx: Index in _statements list.

    Returns:
        True if evicted successfully, False if index invalid.
    """
    if idx < 0 or idx >= len(engram.statements):
        result = False
        return result

    stmt = engram.statements[idx]

    # Remove from keyword indices
    with engram.keyword_lock:
        for kw in stmt.get("keywords", []):
            if kw in engram.keywords:
                engram.keywords[kw].get("statement_ids", set()).discard(stmt.get("id", ""))
                # Prune empty keyword entries
                if not engram.keywords[kw].get("statement_ids", []):
                    del engram.keywords[kw]

    # Remove primary and alias patterns from the matcher. A surviving statement
    # carrying the same pattern keeps it registered and becomes the map target.
    for pattern in [stmt.get("pattern", ""), *stmt.get("pattern_aliases", [])]:
        if not pattern:
            continue
        survivors = [
            s
            for s in engram.statements
            if s.get("id", "") != stmt.get("id", "")
            and (s.get("pattern", "") == pattern or pattern in s.get("pattern_aliases", []))
            and s.get("that", False) == stmt.get("that", False)
            and s.get("topic", "") == stmt.get("topic", "")
        ]
        if not survivors:
            engram.pattern_matcher.remove_pattern(pattern, that=stmt.get("that", False), topic=stmt.get("topic", ""))
        if engram.pattern_to_statement.get(pattern, False) == stmt.get("id", ""):
            del engram.pattern_to_statement[pattern]
            if survivors:
                engram.pattern_to_statement[pattern] = survivors[0].get("id", "")

    # Remove from statement list and update index
    del engram.statement_index[stmt.get("id", "")]
    engram.statements.pop(idx)

    # Rebuild indices after removal
    for i, s in enumerate(engram.statements):
        engram.statement_index[s.get("id", "")] = i

    engram._remove_index_projection_if_present(stmt["id"])
    engram.eviction_count += 1
    result = True
    return result


def evict_dynamic(engram) -> bool:
    """Evict a DYNAMIC statement based on configured policy.

    Selects a candidate based on the eviction policy (FIFO, LRU, LFU, HIT_RATE)
    and removes it from the store.

    Args:
        engram: Engram instance.

    Returns:
        True if a statement was evicted, False if no candidates available.
    """

    candidates = get_eviction_candidates(engram)
    if not candidates:
        result = False
        return result

    policy = engram.config.get("eviction_policy", False)
    target_idx: int

    if policy == EvictionPolicy.FIFO:
        # First-in, first-out: evict oldest (first in list)
        target_idx = candidates[0][0]

    elif policy == EvictionPolicy.LRU:
        # Least recently used: evict statement with oldest last_hit.
        # Statements never hit use created_at as fallback, and lose timestamp
        # ties to statements that were actually used -- clock resolution can
        # make a hit land in the same tick as another statement's creation.
        def lru_key(item: tuple[int, dict]) -> tuple[datetime, int]:
            _, stmt = item
            last_used = stmt.get("last_hit", False) or stmt.get("created_at", False)
            was_hit = 1 if stmt.get("last_hit", False) else 0
            key = (last_used, was_hit)
            return key

        target_idx = min(candidates, key=lru_key)[0]

    elif policy == EvictionPolicy.LFU:
        # Least frequently used: evict statement with lowest hit_count
        # Ties broken by oldest created_at
        def lfu_key(item: tuple[int, dict]) -> tuple[int, datetime]:
            _, stmt = item
            key = (stmt.get("hit_count", 0), stmt.get("created_at", False))
            return key

        target_idx = min(candidates, key=lfu_key)[0]

    elif policy == EvictionPolicy.HIT_RATE:
        # Lowest hit rate: evict statement with lowest hit_rate
        # Ties broken by oldest created_at
        def hit_rate_key(item: tuple[int, dict]) -> tuple[float, datetime]:
            _, stmt = item
            key = (statement_hit_rate(stmt), stmt.get("created_at", False))
            return key

        target_idx = min(candidates, key=hit_rate_key)[0]

    else:
        # Default to FIFO
        target_idx = candidates[0][0]

    evicted = evict_statement_at(engram, target_idx)
    return evicted


def evict(engram) -> bool:
    """Manually evict a DYNAMIC statement based on configured policy.

    Thread-safe wrapper around evict_dynamic.

    Args:
        engram: Engram instance.

    Returns:
        True if a statement was evicted, False otherwise.
    """
    with engram.mutation_lock, engram.statement_lock:
        evicted = evict_dynamic(engram)
        return evicted


def clear_dynamic(engram) -> int:
    """Remove all DYNAMIC statements.

    Useful for resetting the dynamic knowledge base while
    preserving static content.

    Args:
        engram: Engram instance.

    Returns:
        Number of statements removed.
    """
    count = 0
    with engram.mutation_lock, engram.statement_lock:
        while True:
            if not evict_dynamic(engram):
                break
            count += 1
    return count
