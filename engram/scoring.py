"""Scoring algorithm for ENGRAM.

Scores are calibrated to 0.0 - 1.0 so confidence thresholds are meaningful and
portable across queries:

    score = overlap * (wb + wr * recency + wh * hit_rate) / (wb + wr + wh) + priority

- overlap: IDF-weighted fraction of query keywords found in the statement
  (0.0 - 1.0). Rare keywords count for more than common ones, and a keyword
  matched only through a WordNet synonym counts at SYNONYM_OVERLAP_WEIGHT.
- recency: exponential time decay of the statement's last activity (last_hit,
  falling back to created_at) with half-life recency_half_life_seconds. A
  statement's recency depends only on its own timestamps, not on its position
  in the store, so it is stable under eviction.
- hit_rate: average hit rate of the matched keywords (0.0 - 1.0).
- priority: the statement's priority field, added on top. Calibrated scores
  never exceed 1.0, so a priority of 1 or more outranks every unprioritized
  statement; priority applies only when the statement matched (overlap > 0).
"""

import math
from datetime import UTC, datetime

from engram.constants import SYNONYM_OVERLAP_WEIGHT
from engram.models import keyword_entry_hit_rate


def keyword_idf(keyword: str, keyword_index: dict[str, dict], total_statements: int) -> float:
    """Inverse document frequency of a keyword across the statement store.

    Rare keywords are more discriminative: a query keyword carried by two
    statements says more about which statement is wanted than one carried by
    two thousand. The document frequency is the number of statements indexed
    under the keyword; an unindexed keyword gets the maximum weight (df = 1),
    since it represents unmatched query intent.

    Args:
        keyword: The keyword to weigh.
        keyword_index: Keyword index (entries carry statement_ids).
        total_statements: Total statements in the store.

    Returns:
        IDF weight, always positive.
    """
    entry = keyword_index.get(keyword)
    df = len(entry["statement_ids"]) if entry else 0
    if df < 1:
        df = 1
    if total_statements < 1:
        total_statements = 1
    idf = math.log(1.0 + total_statements / df)
    return idf


def keyword_match_weights(
    query_keywords: list[str],
    statement_keywords: list[str],
    synonyms=(),
) -> dict[str, float]:
    """Per-query-keyword match weight against a statement's keywords.

    1.0 when the keyword itself is present, SYNONYM_OVERLAP_WEIGHT when only a
    synonym of it is present, 0.0 otherwise.

    Args:
        query_keywords: Keywords extracted from the query.
        statement_keywords: Keywords the statement is indexed under.
        synonyms: Optional map of query keyword -> tuple of synonyms.

    Returns:
        Dict of query keyword -> match weight.
    """
    statement_set = set(statement_keywords)
    synonym_map = synonyms if isinstance(synonyms, dict) else {}
    weights: dict[str, float] = {}
    for kw in query_keywords:
        if kw in statement_set:
            weights[kw] = 1.0
        elif any(syn in statement_set for syn in synonym_map.get(kw, ())):
            weights[kw] = SYNONYM_OVERLAP_WEIGHT
        else:
            weights[kw] = 0.0
    return weights


def calculate_overlap(
    match_weights: dict[str, float],
    keyword_index: dict[str, dict],
    total_statements: int,
) -> float:
    """IDF-weighted overlap fraction between a query and a statement.

    Each query keyword contributes its IDF weight scaled by its match weight;
    the sum is normalized by the query's total IDF. The result is 0.0 - 1.0,
    reaching 1.0 only when every query keyword matches the statement exactly.

    Args:
        match_weights: Per-keyword match weights from keyword_match_weights.
        keyword_index: Keyword index (for document frequencies).
        total_statements: Total statements in the store.

    Returns:
        Overlap fraction from 0.0 to 1.0.
    """
    if not match_weights:
        return 0.0

    weighted = 0.0
    total = 0.0
    for kw, weight in match_weights.items():
        idf = keyword_idf(kw, keyword_index, total_statements)
        weighted += idf * weight
        total += idf

    if total == 0.0:
        return 0.0
    overlap = weighted / total
    return overlap


def calculate_recency(statement: dict, half_life_seconds: float) -> float:
    """Exponential time-decay recency of a statement's last activity.

    1.0 for a statement created or hit this instant, 0.5 one half-life ago,
    approaching 0.0 as it ages. Uses last_hit when the statement has been hit,
    falling back to created_at, so recency tracks use rather than list
    position and is unaffected by other statements being stored or evicted.

    Args:
        statement: The statement to weigh.
        half_life_seconds: Seconds for recency to halve.

    Returns:
        Recency score from 0.0 to 1.0.
    """
    last_active = statement["last_hit"] or statement["created_at"]
    age_seconds = (datetime.now(UTC) - last_active).total_seconds()
    if age_seconds <= 0:
        return 1.0
    recency = 0.5 ** (age_seconds / half_life_seconds)
    return recency


def calculate_average_hit_rate(
    matched_keywords: list[str],
    keyword_index: dict[str, dict],
) -> float:
    """Average hit rate of the matched keywords.

    Args:
        matched_keywords: Query keywords that matched the statement (exactly
            or through a synonym).
        keyword_index: Keyword index with statistics.

    Returns:
        Average hit rate, 0.5 when there is nothing to average.
    """
    if not matched_keywords:
        return 0.5  # Default when no matches

    hit_rates: list[float] = []
    for kw in matched_keywords:
        entry = keyword_index.get(kw)
        if entry:
            hit_rates.append(keyword_entry_hit_rate(entry))
        else:
            hit_rates.append(0.5)  # Default for unknown keywords

    average = sum(hit_rates) / len(hit_rates)
    return average


def score_statement(
    statement: dict,
    query_keywords: list[str],
    keyword_index: dict[str, dict],
    total_statements: int,
    weight_base: float,
    weight_recency: float,
    weight_hit_rate: float,
    recency_half_life_seconds: float,
    synonyms=(),
) -> float:
    """Calculate the calibrated score for a statement against a query.

    Formula:
        score = overlap * (wb + wr * recency + wh * hit_rate) / (wb + wr + wh)
                + priority

    The relevance term is 0.0 - 1.0 regardless of the configured weights; the
    statement's priority is added on top so priority >= 1 is an absolute
    override among matching statements.

    Args:
        statement: The statement to score.
        query_keywords: Keywords from the query.
        keyword_index: Keyword index with statistics.
        total_statements: Total statements in the store (for IDF).
        weight_base: Base score weight.
        weight_recency: Recency weight.
        weight_hit_rate: Hit rate weight.
        recency_half_life_seconds: Half-life for the recency decay.
        synonyms: Optional map of query keyword -> tuple of synonyms.

    Returns:
        Numeric score (0.0 when the statement does not match; higher = better).
    """
    match = keyword_match_weights(query_keywords, statement["keywords"], synonyms)
    overlap = calculate_overlap(match, keyword_index, total_statements)

    if overlap == 0.0:
        return 0.0

    matched = [kw for kw, weight in match.items() if weight > 0]
    recency = calculate_recency(statement, recency_half_life_seconds)
    hit_rate = calculate_average_hit_rate(matched, keyword_index)

    total_weight = weight_base + weight_recency + weight_hit_rate
    relevance = overlap * (weight_base + weight_recency * recency + weight_hit_rate * hit_rate) / total_weight
    score = relevance + statement["priority"]
    return score
