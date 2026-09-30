"""Least-recently-used eviction for ENGRAM process memory."""

from heapq import heappop as heapq_heappop, heapreplace as heapq_heapreplace

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
    detach_statement(engram, stmt)

    # Positions come from the store-order sequence, so nothing is renumbered.
    # The statement's LRU heap entry is dropped when it reaches the top.
    statement_id = stmt.get("id", "")
    engram.statements.pop(idx)
    engram.statement_sequences.pop(idx)
    del engram.statement_by_id[statement_id]
    del engram.statement_sequence[statement_id]
    engram.dynamic_statement_ids.discard(statement_id)

    engram.eviction_count += 1
    result = True
    return result


def detach_statement(engram, stmt: dict) -> None:
    """Remove a statement's keywords and patterns, leaving the statement list alone."""
    # Remove from keyword indices
    with engram.keyword_lock:
        for kw in stmt.get("keywords", []):
            if kw in engram.keywords:
                engram.keywords[kw].get("statement_ids", set()).discard(stmt.get("id", ""))
                # Prune empty keyword entries
                if not engram.keywords[kw].get("statement_ids", []):
                    del engram.keywords[kw]

    # store() registered one matcher entry per pattern, so exactly one goes.
    # A surviving statement carrying the same pattern keeps its own entry and
    # becomes the map target. Keeping this entry for a survivor instead would
    # leave a dead entry behind once the last carrier is evicted.
    for pattern in [stmt.get("pattern", ""), *stmt.get("pattern_aliases", [])]:
        if not pattern:
            continue
        carriers = [
            engram.statement_by_id[statement_id]
            for statement_id in engram.pattern_statements.get(pattern, ())
            if statement_id != stmt.get("id", "")
        ]
        survivors = [
            s for s in carriers if s.get("that", False) == stmt.get("that", False) and s.get("topic", "") == stmt.get("topic", "")
        ]
        engram.pattern_matcher.remove_pattern(pattern, that=stmt.get("that", ""), topic=stmt.get("topic", ""))
        if engram.pattern_to_statement.get(pattern, False) == stmt.get("id", ""):
            del engram.pattern_to_statement[pattern]
            if survivors:
                engram.pattern_to_statement[pattern] = survivors[0].get("id", "")

    for pattern in [stmt.get("pattern", ""), *stmt.get("pattern_aliases", [])]:
        carriers = engram.pattern_statements.get(pattern)
        if carriers and stmt.get("id", "") in carriers:
            carriers.remove(stmt.get("id", ""))
            if not carriers:
                del engram.pattern_statements[pattern]


def lru_entry(statement: dict, sequence: int) -> tuple:
    """Return a DYNAMIC statement's LRU heap entry: its eviction key, then its id.

    The key is (last used, whether it was ever hit, store order). Equal
    timestamps keep store order, as list positions did.
    """
    last_hit = statement.get("last_hit", False)
    last_used = last_hit or statement.get("created_at", False)
    result = (last_used, 1 if last_hit else 0, sequence, statement.get("id", ""))
    return result


def evict_dynamic(engram) -> bool:
    """Evict the least-recently-used DYNAMIC statement.

    The heap holds one entry per DYNAMIC statement, recorded when it was
    stored or last checked. Hits only move a statement's key later, so an
    entry whose key is still current at the top of the heap is the true
    minimum; a stale entry is refreshed and pushed back, and an entry for a
    removed statement is dropped.

    Args:
        engram: Engram instance.

    Returns:
        True if a statement was evicted, False if no candidates available.
    """

    heap = engram.dynamic_lru
    while heap:
        entry = heap[0]
        statement_id = entry[3]
        sequence = entry[2]
        if statement_id not in engram.dynamic_statement_ids or engram.statement_sequence.get(statement_id) != sequence:
            heapq_heappop(heap)
            continue
        current = lru_entry(engram.statement_by_id[statement_id], sequence)
        if current != entry:
            heapq_heapreplace(heap, current)
            continue
        heapq_heappop(heap)
        evicted = evict_statement_at(engram, engram.statement_position(statement_id))
        return evicted
    result = False
    return result


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
    with engram.mutation_lock, engram.statement_lock:
        dynamic = [stmt for stmt in engram.statements if stmt.get("tier", "") == Tier.DYNAMIC]
        for stmt in dynamic:
            detach_statement(engram, stmt)
        # One pass over the list instead of one eviction scan per statement.
        kept = [
            (stmt, sequence)
            for stmt, sequence in zip(engram.statements, engram.statement_sequences, strict=True)
            if stmt.get("tier", "") != Tier.DYNAMIC
        ]
        engram.statements[:] = [stmt for stmt, _ in kept]
        engram.statement_sequences[:] = [sequence for _, sequence in kept]
        for stmt in dynamic:
            del engram.statement_by_id[stmt.get("id", "")]
            del engram.statement_sequence[stmt.get("id", "")]
        engram.dynamic_statement_ids.clear()
        engram.dynamic_lru.clear()
        engram.eviction_count += len(dynamic)
        count = len(dynamic)
    return count
