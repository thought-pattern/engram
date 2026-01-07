"""Metrics and coverage analysis for ENGRAM.

This module provides functions for analyzing keyword performance,
coverage gaps, and generating recommendations for improving the knowledge base.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engram.models import KeywordEntry


class MetricsMixin:
    """Mixin class providing metrics and coverage analysis methods.

    This mixin is designed to be used with the Engram class and expects
    the following attributes to be present:
    - _keywords: dict[str, KeywordEntry]
    - _keyword_lock: threading.RLock
    - _query_count: int
    - _hit_count: int
    - _eviction_count: int
    - _statements: list[Statement]
    - _sessions: dict[str, Session]
    """

    # Type hints for expected attributes (defined in Engram)
    _keywords: dict[str, "KeywordEntry"]
    _keyword_lock: threading.RLock
    _query_count: int
    _hit_count: int
    _eviction_count: int

    @property
    def statement_count(self) -> int:
        """Number of statements stored."""
        return len(self._statements)  # type: ignore

    @property
    def static_count(self) -> int:
        """Number of STATIC tier statements."""
        from engram.models import Tier
        return sum(1 for s in self._statements if s.tier == Tier.STATIC)  # type: ignore

    @property
    def dynamic_count(self) -> int:
        """Number of DYNAMIC tier statements."""
        from engram.models import Tier
        return sum(1 for s in self._statements if s.tier == Tier.DYNAMIC)  # type: ignore

    @property
    def keyword_count(self) -> int:
        """Number of unique keywords indexed."""
        return len(self._keywords)

    @property
    def session_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)  # type: ignore

    @property
    def total_queries(self) -> int:
        """Total number of queries processed."""
        return self._query_count

    @property
    def total_hits(self) -> int:
        """Total number of query hits."""
        return self._hit_count

    @property
    def total_evictions(self) -> int:
        """Total number of evictions performed."""
        return self._eviction_count

    @property
    def overall_hit_rate(self) -> float:
        """Overall hit rate percentage."""
        if self._query_count == 0:
            return 0.0
        return self._hit_count / self._query_count

    def get_metrics(self) -> dict[str, Any]:
        """Get all metrics as a dictionary.

        Returns:
            Dict with counts and rates for statements, keywords, sessions, etc.
        """
        return {
            "statement_count": self.statement_count,
            "static_count": self.static_count,
            "dynamic_count": self.dynamic_count,
            "keyword_count": self.keyword_count,
            "session_count": self.session_count,
            "query_count": self._query_count,
            "hit_count": self._hit_count,
            "eviction_count": self._eviction_count,
            "hit_rate": self.overall_hit_rate,
        }

    def get_low_hit_keywords(
        self,
        min_queries: int = 10,
        max_hit_rate: float = 0.2,
    ) -> list[tuple[str, int, float]]:
        """Get keywords with high traffic but low hit rate.

        These keywords represent topics users frequently ask about but
        the knowledge base doesn't adequately address.

        Args:
            min_queries: Minimum query count threshold.
            max_hit_rate: Maximum hit rate threshold.

        Returns:
            List of (keyword, query_count, hit_rate) tuples, sorted by query count.
        """
        results: list[tuple[str, int, float]] = []
        with self._keyword_lock:
            for kw, entry in self._keywords.items():
                if entry.query_count >= min_queries and entry.hit_rate <= max_hit_rate:
                    results.append((kw, entry.query_count, entry.hit_rate))
        return sorted(results, key=lambda x: x[1], reverse=True)

    def get_zero_hit_keywords(self, min_queries: int = 10) -> list[tuple[str, int]]:
        """Get keywords with queries but zero hits.

        These are the most critical coverage gaps - keywords that users
        search for but never find any matching content.

        Args:
            min_queries: Minimum query count threshold.

        Returns:
            List of (keyword, query_count) tuples, sorted by query count.
        """
        results: list[tuple[str, int]] = []
        with self._keyword_lock:
            for kw, entry in self._keywords.items():
                if entry.query_count >= min_queries and entry.hit_count == 0:
                    results.append((kw, entry.query_count))
        return sorted(results, key=lambda x: x[1], reverse=True)

    def get_coverage_gaps(
        self,
        min_queries: int = 10,
        max_hit_rate: float = 0.2,
    ) -> list[dict]:
        """Get keywords with high query volume but low hit rates.

        These represent topics users ask about but the knowledge base
        doesn't adequately cover.

        Args:
            min_queries: Minimum query count threshold.
            max_hit_rate: Maximum hit rate threshold.

        Returns:
            List of dicts with keyword, queries, hits, hit_rate.
        """
        results: list[dict] = []
        with self._keyword_lock:
            for kw, entry in self._keywords.items():
                if entry.query_count >= min_queries and entry.hit_rate <= max_hit_rate:
                    results.append({
                        "keyword": kw,
                        "queries": entry.query_count,
                        "hits": entry.hit_count,
                        "hit_rate": round(entry.hit_rate, 3),
                    })
        return sorted(results, key=lambda x: x["queries"], reverse=True)

    def get_coverage_report(self) -> dict:
        """Get comprehensive coverage analysis report.

        Provides statistics on keyword coverage, identifies gaps, highlights
        top performers, and generates actionable recommendations.

        Returns:
            Dict with coverage statistics and recommendations.
        """
        with self._keyword_lock:
            total_keywords = len(self._keywords)
            keywords_with_hits = sum(
                1 for e in self._keywords.values() if e.hit_count > 0
            )
            keywords_zero_hits = total_keywords - keywords_with_hits

            # Get coverage gaps (high traffic, low hit rate)
            coverage_gaps = self.get_coverage_gaps(min_queries=10, max_hit_rate=0.2)

            # Get top performing keywords (high hit rate with significant traffic)
            top_performing: list[dict] = []
            for kw, entry in self._keywords.items():
                if entry.query_count >= 10 and entry.hit_rate >= 0.5:
                    top_performing.append({
                        "keyword": kw,
                        "queries": entry.query_count,
                        "hits": entry.hit_count,
                        "hit_rate": round(entry.hit_rate, 3),
                    })
            top_performing.sort(key=lambda x: x["hit_rate"], reverse=True)
            top_performing = top_performing[:10]  # Top 10

            # Generate recommendations
            recommendations: list[str] = []

            # Recommend adding categories for zero-hit keywords
            zero_hits = self.get_zero_hit_keywords(min_queries=10)
            if zero_hits:
                top_zero = [kw for kw, _ in zero_hits[:5]]
                recommendations.append(f"Add categories for: {', '.join(top_zero)}")

            # Recommend reviewing low-performing keywords
            for gap in coverage_gaps[:3]:
                if gap["hit_rate"] < 0.1:
                    recommendations.append(
                        f"Review low-performing: {gap['keyword']} "
                        f"({gap['hit_rate']*100:.1f}% hit rate)"
                    )

            return {
                "total_keywords": total_keywords,
                "keywords_with_hits": keywords_with_hits,
                "keywords_zero_hits": keywords_zero_hits,
                "overall_hit_rate": round(self.overall_hit_rate, 3),
                "coverage_gaps": coverage_gaps[:10],  # Top 10 gaps
                "top_performing": top_performing,
                "recommendations": recommendations,
            }
