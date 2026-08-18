"""Metrics and coverage analysis for ENGRAM.

This module provides functions for analyzing keyword performance,
coverage gaps, and generating recommendations for improving the knowledge base.
"""

from engram.constants import Tier
from engram.models import keyword_entry_hit_rate


def get_statement_count(engram) -> int:
    """Number of statements stored."""
    count = len(engram.statements)
    return count


def get_static_count(engram) -> int:
    """Number of STATIC tier statements."""

    count = sum(1 for s in engram.statements if s["tier"] == Tier.STATIC)
    return count


def get_dynamic_count(engram) -> int:
    """Number of DYNAMIC tier statements."""

    count = sum(1 for s in engram.statements if s["tier"] == Tier.DYNAMIC)
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

    Hit statistics are otherwise immortal: an entry that earned a strong hit
    rate long ago keeps it forever after it stops being used, and min_hit_rate
    would protect it indefinitely. Calling this periodically (like
    expire_sessions) ages the evidence -- counts shrink proportionally, so
    rates are preserved while confidence decays, and an entry that stops
    re-earning its statistics eventually returns to zero query history and
    becomes evictable again.

    Args:
        engram: Engram instance.
        factor: Multiplier applied to every count, 0.0 <= factor < 1.0
            (0.5 halves everything; 0.0 resets all statistics).

    Returns:
        Number of records (keyword entries plus statements) whose counts changed.
    """
    if factor < 0.0 or factor >= 1.0:
        raise ValueError("factor must be at least 0.0 and below 1.0")

    changed = 0
    with engram.keyword_lock:
        for entry in engram.keywords.values():
            new_queries = int(entry["query_count"] * factor)
            new_hits = int(entry["hit_count"] * factor)
            if new_queries != entry["query_count"] or new_hits != entry["hit_count"]:
                entry["query_count"] = new_queries
                entry["hit_count"] = new_hits
                changed += 1
    with engram.statement_lock:
        for stmt in engram.statements:
            new_queries = int(stmt["query_count"] * factor)
            new_hits = int(stmt["hit_count"] * factor)
            if new_queries != stmt["query_count"] or new_hits != stmt["hit_count"]:
                stmt["query_count"] = new_queries
                stmt["hit_count"] = new_hits
                changed += 1
    return changed


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
            hit_rate = keyword_entry_hit_rate(entry)
            if entry["query_count"] >= min_queries and hit_rate <= max_hit_rate:
                results.append((kw, entry["query_count"], hit_rate))
    ranked = sorted(results, key=lambda x: x[1], reverse=True)
    return ranked


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
            if entry["query_count"] >= min_queries and entry["hit_count"] == 0:
                results.append((kw, entry["query_count"]))
    ranked = sorted(results, key=lambda x: x[1], reverse=True)
    return ranked


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
            hit_rate = keyword_entry_hit_rate(entry)
            if entry["query_count"] >= min_queries and hit_rate <= max_hit_rate:
                results.append(
                    {
                        "keyword": kw,
                        "queries": entry["query_count"],
                        "hits": entry["hit_count"],
                        "hit_rate": round(hit_rate, 3),
                    }
                )
    ranked = sorted(results, key=lambda x: x["queries"], reverse=True)
    return ranked


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
        keywords_with_hits = sum(1 for e in engram.keywords.values() if e["hit_count"] > 0)
        keywords_zero_hits = total_keywords - keywords_with_hits

        # Get coverage gaps (high traffic, low hit rate)
        coverage_gaps = get_coverage_gaps(engram, min_queries=10, max_hit_rate=0.2)

        # Get top performing keywords (high hit rate with significant traffic)
        top_performing: list[dict] = []
        for kw, entry in engram.keywords.items():
            hit_rate = keyword_entry_hit_rate(entry)
            if entry["query_count"] >= 10 and hit_rate >= 0.5:
                top_performing.append(
                    {
                        "keyword": kw,
                        "queries": entry["query_count"],
                        "hits": entry["hit_count"],
                        "hit_rate": round(hit_rate, 3),
                    }
                )
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
                recommendations.append(f"Review low-performing: {gap['keyword']} ({gap['hit_rate'] * 100:.1f}% hit rate)")

        report = {
            "total_keywords": total_keywords,
            "keywords_with_hits": keywords_with_hits,
            "keywords_zero_hits": keywords_zero_hits,
            "overall_hit_rate": round(get_overall_hit_rate(engram), 3),
            "coverage_gaps": coverage_gaps[:10],  # Top 10 gaps
            "top_performing": top_performing,
            "recommendations": recommendations,
        }
        return report
