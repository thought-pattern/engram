"""Metrics and coverage analysis for ENGRAM.

This module provides functions for analyzing keyword performance,
coverage gaps, and generating recommendations for improving the knowledge base.
"""

from __future__ import annotations


def get_statement_count(engram) -> int:
    """Number of statements stored."""
    return len(engram.statements)


def get_static_count(engram) -> int:
    """Number of STATIC tier statements."""
    from engram.models import Tier
    return sum(1 for s in engram.statements if s.tier == Tier.STATIC)


def get_dynamic_count(engram) -> int:
    """Number of DYNAMIC tier statements."""
    from engram.models import Tier
    return sum(1 for s in engram.statements if s.tier == Tier.DYNAMIC)


def get_keyword_count(engram) -> int:
    """Number of unique keywords indexed."""
    return len(engram.keywords)


def get_session_count(engram) -> int:
    """Number of active sessions."""
    return len(engram.sessions)


def get_overall_hit_rate(engram) -> float:
    """Overall hit rate percentage."""
    if engram.query_count == 0:
        return 0.0
    return engram.hit_count / engram.query_count


def get_metrics(engram) -> dict:
    """Get all metrics as a dictionary.

    Args:
        engram: Engram instance.

    Returns:
        Dict with counts and rates for statements, keywords, sessions, etc.
    """
    return {
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


def get_low_hit_keywords(
    engram,
    min_queries: int = 10,
    max_hit_rate: float = 0.2,
) -> list[tuple[str, int, float]]:
    """Get keywords with high traffic but low hit rate.

    These keywords represent topics users frequently ask about but
    the knowledge base doesn't adequately address.

    Args:
        engram: Engram instance.
        min_queries: Minimum query count threshold.
        max_hit_rate: Maximum hit rate threshold.

    Returns:
        List of (keyword, query_count, hit_rate) tuples, sorted by query count.
    """
    results: list[tuple[str, int, float]] = []
    with engram.keyword_lock:
        for kw, entry in engram.keywords.items():
            if entry.query_count >= min_queries and entry.hit_rate <= max_hit_rate:
                results.append((kw, entry.query_count, entry.hit_rate))
    return sorted(results, key=lambda x: x[1], reverse=True)


def get_zero_hit_keywords(engram, min_queries: int = 10) -> list[tuple[str, int]]:
    """Get keywords with queries but zero hits.

    These are the most critical coverage gaps - keywords that users
    search for but never find any matching content.

    Args:
        engram: Engram instance.
        min_queries: Minimum query count threshold.

    Returns:
        List of (keyword, query_count) tuples, sorted by query count.
    """
    results: list[tuple[str, int]] = []
    with engram.keyword_lock:
        for kw, entry in engram.keywords.items():
            if entry.query_count >= min_queries and entry.hit_count == 0:
                results.append((kw, entry.query_count))
    return sorted(results, key=lambda x: x[1], reverse=True)


def get_coverage_gaps(
    engram,
    min_queries: int = 10,
    max_hit_rate: float = 0.2,
) -> list[dict]:
    """Get keywords with high query volume but low hit rates.

    These represent topics users ask about but the knowledge base
    doesn't adequately cover.

    Args:
        engram: Engram instance.
        min_queries: Minimum query count threshold.
        max_hit_rate: Maximum hit rate threshold.

    Returns:
        List of dicts with keyword, queries, hits, hit_rate.
    """
    results: list[dict] = []
    with engram.keyword_lock:
        for kw, entry in engram.keywords.items():
            if entry.query_count >= min_queries and entry.hit_rate <= max_hit_rate:
                results.append({
                    "keyword": kw,
                    "queries": entry.query_count,
                    "hits": entry.hit_count,
                    "hit_rate": round(entry.hit_rate, 3),
                })
    return sorted(results, key=lambda x: x["queries"], reverse=True)


def get_coverage_report(engram) -> dict:
    """Get comprehensive coverage analysis report.

    Provides statistics on keyword coverage, identifies gaps, highlights
    top performers, and generates actionable recommendations.

    Args:
        engram: Engram instance.

    Returns:
        Dict with coverage statistics and recommendations.
    """
    with engram.keyword_lock:
        total_keywords = len(engram.keywords)
        keywords_with_hits = sum(
            1 for e in engram.keywords.values() if e.hit_count > 0
        )
        keywords_zero_hits = total_keywords - keywords_with_hits

        # Get coverage gaps (high traffic, low hit rate)
        coverage_gaps = get_coverage_gaps(engram, min_queries=10, max_hit_rate=0.2)

        # Get top performing keywords (high hit rate with significant traffic)
        top_performing: list[dict] = []
        for kw, entry in engram.keywords.items():
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
        zero_hits = get_zero_hit_keywords(engram, min_queries=10)
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
            "overall_hit_rate": round(get_overall_hit_rate(engram), 3),
            "coverage_gaps": coverage_gaps[:10],  # Top 10 gaps
            "top_performing": top_performing,
            "recommendations": recommendations,
        }
