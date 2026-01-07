"""Scoring algorithm for ENGRAM."""

from engram.models import KeywordEntry, Statement


def calculate_overlap(query_keywords: list[str], statement_keywords: list[str]) -> int:
    """Calculate keyword overlap between query and statement.

    Args:
        query_keywords: Keywords from the query.
        statement_keywords: Keywords from the statement.

    Returns:
        Count of query keywords present in statement.
    """
    statement_set = set(statement_keywords)
    return sum(1 for kw in query_keywords if kw in statement_set)


def calculate_recency(statement_index: int, total_statements: int) -> float:
    """Calculate recency score based on statement position.

    Args:
        statement_index: Index of statement in list (0-based).
        total_statements: Total number of statements.

    Returns:
        Recency score from 0.0 to 1.0 (higher = more recent).
    """
    if total_statements <= 1:
        return 1.0
    return statement_index / (total_statements - 1)


def calculate_average_hit_rate(
    query_keywords: list[str],
    statement_keywords: list[str],
    keyword_index: dict[str, KeywordEntry],
) -> float:
    """Calculate average hit rate of matched keywords.

    Args:
        query_keywords: Keywords from the query.
        statement_keywords: Keywords from the statement.
        keyword_index: Keyword index with statistics.

    Returns:
        Average hit rate of matched keywords.
    """
    statement_set = set(statement_keywords)
    matched_keywords = [kw for kw in query_keywords if kw in statement_set]

    if not matched_keywords:
        return 0.5  # Default when no matches

    hit_rates: list[float] = []
    for kw in matched_keywords:
        entry = keyword_index.get(kw)
        if entry:
            hit_rates.append(entry.hit_rate)
        else:
            hit_rates.append(0.5)  # Default for unknown keywords

    return sum(hit_rates) / len(hit_rates)


def score_statement(
    statement: Statement,
    statement_index: int,
    total_statements: int,
    query_keywords: list[str],
    keyword_index: dict[str, KeywordEntry],
    weight_base: float,
    weight_recency: float,
    weight_hit_rate: float,
) -> float:
    """Calculate score for a statement against a query.

    Formula:
        score = overlap * (weight_base + weight_recency * recency + weight_hit_rate * hit_rate)

    Args:
        statement: The statement to score.
        statement_index: Index of statement in list.
        total_statements: Total number of statements.
        query_keywords: Keywords from the query.
        keyword_index: Keyword index with statistics.
        weight_base: Base score weight.
        weight_recency: Recency weight.
        weight_hit_rate: Hit rate weight.

    Returns:
        Numeric score (higher = better match).
    """
    overlap = calculate_overlap(query_keywords, statement.keywords)

    if overlap == 0:
        return 0.0

    recency = calculate_recency(statement_index, total_statements)
    hit_rate = calculate_average_hit_rate(query_keywords, statement.keywords, keyword_index)

    score = overlap * (weight_base + weight_recency * recency + weight_hit_rate * hit_rate)
    return score
