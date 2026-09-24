"""Least-recently-used eviction for ENGRAM process memory."""

from datetime import datetime

from engram.constants import Tier


def get_eviction_candidates(engram) -> list[tuple[int, dict]]:
    """Get DYNAMIC statements eligible for eviction.

    Args:
        engram: Engram instance.

    Returns:
        List of (index, statement) tuples for eviction candidates.
    """

    candidates = []
    for idx, stmt in enumerate(engram.statements):
        if stmt.get("tier", "") == Tier.DYNAMIC:
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

    engram.eviction_count += 1
    result = True
    return result


def evict_dynamic(engram) -> bool:
    """Evict the least-recently-used DYNAMIC statement.

    Args:
        engram: Engram instance.

    Returns:
        True if a statement was evicted, False if no candidates available.
    """

    candidates = get_eviction_candidates(engram)
    if not candidates:
        result = False
        return result

    def lru_key(item: tuple[int, dict]) -> tuple[datetime, int, str]:
        _, statement = item
        last_hit = statement.get("last_hit", False)
        last_used = last_hit or statement.get("created_at", False)
        key = (last_used, 1 if last_hit else 0, str(statement.get("id", "")))
        return key

    target_idx = min(candidates, key=lru_key)[0]

    evicted = evict_statement_at(engram, target_idx)
    return evicted


def evict(engram) -> bool:
    """Manually evict the least-recently-used DYNAMIC statement.

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
