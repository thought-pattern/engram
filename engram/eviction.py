"""Eviction policies for ENGRAM.

This module provides eviction functionality for managing the capacity
of the statement store by removing DYNAMIC tier statements based on
various policies (FIFO, LRU, LFU, HIT_RATE).
"""

from datetime import datetime

from .constants import EvictionPolicy, Tier
from .models import statement_hit_rate


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
        if stmt["tier"] == Tier.DYNAMIC:
            # min_hit_rate protects proven performers only. A statement with no
            # query history has no evidence either way (its hit rate defaults to
            # 0.5) and stays evictable -- otherwise any threshold below 0.5
            # would protect every untouched statement and disable eviction.
            if (
                engram.config["min_hit_rate"] > 0
                and stmt["query_count"] > 0
                and statement_hit_rate(stmt) >= engram.config["min_hit_rate"]
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
        return False

    stmt = engram.statements[idx]

    # Remove from keyword indices
    with engram.keyword_lock:
        for kw in stmt["keywords"]:
            if kw in engram.keywords:
                engram.keywords[kw]["statement_ids"].discard(stmt["id"])
                # Prune empty keyword entries
                if not engram.keywords[kw]["statement_ids"]:
                    del engram.keywords[kw]

    # Remove the statement's pattern from the matcher, unless another statement
    # still carries the same (pattern, that, topic) -- a dead pattern that keeps
    # matching would shadow live patterns and silently produce no response.
    pattern = stmt["pattern"]
    if pattern:
        shared = any(
            s["id"] != stmt["id"] and s["pattern"] == pattern and s["that"] == stmt["that"] and s["topic"] == stmt["topic"]
            for s in engram.statements
        )
        if not shared:
            engram.pattern_matcher.remove_pattern(pattern, that=stmt["that"], topic=stmt["topic"])
        if engram.pattern_to_statement.get(pattern) == stmt["id"]:
            del engram.pattern_to_statement[pattern]
            # Remap to a surviving statement with the same pattern, if any
            for s in engram.statements:
                if s["id"] != stmt["id"] and s["pattern"] == pattern:
                    engram.pattern_to_statement[pattern] = s["id"]
                    break

    # Remove from statement list and update index
    del engram.statement_index[stmt["id"]]
    engram.statements.pop(idx)

    # Rebuild indices after removal
    for i, s in enumerate(engram.statements):
        engram.statement_index[s["id"]] = i

    engram.eviction_count += 1
    return True


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
        return False

    policy = engram.config["eviction_policy"]
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
            last_used = stmt["last_hit"] or stmt["created_at"]
            was_hit = 1 if stmt["last_hit"] else 0
            key = (last_used, was_hit)
            return key

        target_idx = min(candidates, key=lru_key)[0]

    elif policy == EvictionPolicy.LFU:
        # Least frequently used: evict statement with lowest hit_count
        # Ties broken by oldest created_at
        def lfu_key(item: tuple[int, dict]) -> tuple[int, datetime]:
            _, stmt = item
            key = (stmt["hit_count"], stmt["created_at"])
            return key

        target_idx = min(candidates, key=lfu_key)[0]

    elif policy == EvictionPolicy.HIT_RATE:
        # Lowest hit rate: evict statement with lowest hit_rate
        # Ties broken by oldest created_at
        def hit_rate_key(item: tuple[int, dict]) -> tuple[float, datetime]:
            _, stmt = item
            key = (statement_hit_rate(stmt), stmt["created_at"])
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
    with engram.statement_lock:
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
    with engram.statement_lock:
        while True:
            if not evict_dynamic(engram):
                break
            count += 1
    return count
