"""Eviction policies for ENGRAM.

This module provides eviction functionality for managing the capacity
of the statement store by removing DYNAMIC tier statements based on
various policies (FIFO, LRU, LFU, HIT_RATE).
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engram.config import EngramConfig, EvictionPolicy
    from engram.models import KeywordEntry, Statement, Tier


class EvictionMixin:
    """Mixin class providing eviction methods.

    This mixin is designed to be used with the Engram class and expects
    the following attributes to be present:
    - _statements: list[Statement]
    - _statement_index: dict[str, int]
    - _keywords: dict[str, KeywordEntry]
    - _statement_lock, _keyword_lock: threading.RLock
    - _eviction_count: int
    - config: EngramConfig
    """

    # Type hints for expected attributes
    _statements: list["Statement"]
    _statement_index: dict[str, int]
    _keywords: dict[str, "KeywordEntry"]
    _statement_lock: threading.RLock
    _keyword_lock: threading.RLock
    _eviction_count: int
    config: "EngramConfig"

    def _get_eviction_candidates(self) -> list[tuple[int, "Statement"]]:
        """Get DYNAMIC statements eligible for eviction.

        Filters out statements that are protected by min_hit_rate threshold.

        Returns:
            List of (index, statement) tuples for eviction candidates.
        """
        from engram.models import Tier

        candidates = []
        for idx, stmt in enumerate(self._statements):
            if stmt.tier == Tier.DYNAMIC:
                # Check min_hit_rate protection
                if self.config.min_hit_rate > 0 and stmt.hit_rate >= self.config.min_hit_rate:
                    continue
                candidates.append((idx, stmt))
        return candidates

    def _evict_statement_at(self, idx: int) -> bool:
        """Evict statement at given index.

        Removes the statement from the store and updates all indices.

        Args:
            idx: Index in _statements list.

        Returns:
            True if evicted successfully, False if index invalid.
        """
        if idx < 0 or idx >= len(self._statements):
            return False

        stmt = self._statements[idx]

        # Remove from keyword indices
        with self._keyword_lock:
            for kw in stmt.keywords:
                if kw in self._keywords:
                    self._keywords[kw].remove_statement(stmt.id)
                    # Prune empty keyword entries
                    if not self._keywords[kw].statement_ids:
                        del self._keywords[kw]

        # Remove from statement list and update index
        del self._statement_index[stmt.id]
        self._statements.pop(idx)

        # Rebuild indices after removal
        for i, s in enumerate(self._statements):
            self._statement_index[s.id] = i

        self._eviction_count += 1
        return True

    def _evict_dynamic(self) -> bool:
        """Evict a DYNAMIC statement based on configured policy.

        Selects a candidate based on the eviction policy (FIFO, LRU, LFU, HIT_RATE)
        and removes it from the store.

        Returns:
            True if a statement was evicted, False if no candidates available.
        """
        from engram.config import EvictionPolicy

        candidates = self._get_eviction_candidates()
        if not candidates:
            return False

        policy = self.config.eviction_policy
        target_idx: int

        if policy == EvictionPolicy.FIFO:
            # First-in, first-out: evict oldest (first in list)
            target_idx = candidates[0][0]

        elif policy == EvictionPolicy.LRU:
            # Least recently used: evict statement with oldest last_hit
            # Statements never hit use created_at as fallback
            def lru_key(item: tuple[int, "Statement"]) -> datetime:
                _, stmt = item
                if stmt.last_hit is not None:
                    return stmt.last_hit
                return stmt.created_at

            target_idx = min(candidates, key=lru_key)[0]

        elif policy == EvictionPolicy.LFU:
            # Least frequently used: evict statement with lowest hit_count
            # Ties broken by oldest created_at
            def lfu_key(item: tuple[int, "Statement"]) -> tuple[int, datetime]:
                _, stmt = item
                return (stmt.hit_count, stmt.created_at)

            target_idx = min(candidates, key=lfu_key)[0]

        elif policy == EvictionPolicy.HIT_RATE:
            # Lowest hit rate: evict statement with lowest hit_rate
            # Ties broken by oldest created_at
            def hit_rate_key(item: tuple[int, "Statement"]) -> tuple[float, datetime]:
                _, stmt = item
                return (stmt.hit_rate, stmt.created_at)

            target_idx = min(candidates, key=hit_rate_key)[0]

        else:
            # Default to FIFO
            target_idx = candidates[0][0]

        return self._evict_statement_at(target_idx)

    def _evict_oldest_dynamic(self) -> bool:
        """Evict the oldest DYNAMIC statement (FIFO).

        Backward-compatible method that always uses FIFO eviction.

        Returns:
            True if a statement was evicted, False otherwise.
        """
        return self._evict_dynamic()

    def evict(self) -> bool:
        """Manually evict a DYNAMIC statement based on configured policy.

        Thread-safe wrapper around _evict_dynamic.

        Returns:
            True if a statement was evicted, False otherwise.
        """
        with self._statement_lock:
            return self._evict_dynamic()

    def clear_dynamic(self) -> int:
        """Remove all DYNAMIC statements.

        Useful for resetting the dynamic knowledge base while
        preserving static content.

        Returns:
            Number of statements removed.
        """
        count = 0
        with self._statement_lock:
            while True:
                if not self._evict_oldest_dynamic():
                    break
                count += 1
        return count
