"""Tests for the calibrated scoring algorithm."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from engram.constants import SYNONYM_OVERLAP_WEIGHT
from engram.models import keyword_entry, statement
from engram.scoring import (
    calculate_average_hit_rate,
    calculate_overlap,
    calculate_recency,
    keyword_idf,
    keyword_match_weights,
    score_statement,
)

"""Tests for inverse document frequency weighting."""


def test_keyword_idf_rare_keyword_outweighs_common() -> None:
    keyword_index = {
        "rare": keyword_entry(keyword="rare", statement_ids=["s1"]),
        "common": keyword_entry(keyword="common", statement_ids=[f"s{i}" for i in range(100)]),
    }
    assert keyword_idf("rare", keyword_index, 100) > keyword_idf("common", keyword_index, 100)


def test_keyword_idf_unindexed_keyword_gets_maximum_weight() -> None:
    keyword_index = {
        "known": keyword_entry(keyword="known", statement_ids=["s1", "s2"]),
    }
    assert keyword_idf("unknown", keyword_index, 100) == pytest.approx(math.log(1 + 100))
    assert keyword_idf("unknown", keyword_index, 100) > keyword_idf("known", keyword_index, 100)


def test_keyword_idf_empty_store_is_positive() -> None:
    assert keyword_idf("anything", {}, 0) > 0


"""Tests for per-keyword match weighting."""


def test_keyword_match_weights_exact_match_full_weight() -> None:
    weights = keyword_match_weights(["alpha", "beta"], ["alpha", "gamma"])
    assert weights == {"alpha": 1.0, "beta": 0.0}


def test_keyword_match_weights_synonym_match_discounted() -> None:
    weights = keyword_match_weights(
        ["car"],
        ["automobile", "fast"],
        synonyms={"car": ("automobile", "auto")},
    )
    assert weights == {"car": SYNONYM_OVERLAP_WEIGHT}


def test_keyword_match_weights_exact_match_beats_synonym() -> None:
    weights = keyword_match_weights(
        ["car"],
        ["car", "automobile"],
        synonyms={"car": ("automobile",)},
    )
    assert weights == {"car": 1.0}


def test_keyword_match_weights_no_synonym_map_no_synonym_credit() -> None:
    weights = keyword_match_weights(["car"], ["automobile"])
    assert weights == {"car": 0.0}


"""Tests for the IDF-weighted overlap fraction."""


def test_calculate_overlap_full_overlap_is_one() -> None:
    match = {"alpha": 1.0, "beta": 1.0}
    assert calculate_overlap(match, {}, 1) == pytest.approx(1.0)


def test_calculate_overlap_no_overlap_is_zero() -> None:
    match = {"alpha": 0.0, "beta": 0.0}
    assert calculate_overlap(match, {}, 1) == 0.0


def test_calculate_overlap_uniform_idf_reduces_to_fraction() -> None:
    # With no index, every keyword has the same IDF, so overlap is the
    # plain matched fraction.
    match = {"alpha": 1.0, "beta": 0.0}
    assert calculate_overlap(match, {}, 1) == pytest.approx(0.5)


def test_calculate_overlap_rare_match_outscores_common_match() -> None:
    keyword_index = {
        "rare": keyword_entry(keyword="rare", statement_ids=["s1"]),
        "common": keyword_entry(keyword="common", statement_ids=[f"s{i}" for i in range(50)]),
    }
    rare_match = calculate_overlap({"rare": 1.0, "common": 0.0}, keyword_index, 50)
    common_match = calculate_overlap({"rare": 0.0, "common": 1.0}, keyword_index, 50)
    assert rare_match > common_match


def test_calculate_overlap_synonym_weight_scales_contribution() -> None:
    exact = calculate_overlap({"car": 1.0}, {}, 1)
    via_synonym = calculate_overlap({"car": SYNONYM_OVERLAP_WEIGHT}, {}, 1)
    assert via_synonym == pytest.approx(exact * SYNONYM_OVERLAP_WEIGHT)


def test_calculate_overlap_empty_query() -> None:
    assert calculate_overlap({}, {}, 1) == 0.0


"""Tests for time-decay recency."""


def test_calculate_recency_fresh_statement_is_one() -> None:
    stmt = statement("fresh")
    assert calculate_recency(stmt, half_life_seconds=3600.0) == pytest.approx(1.0, abs=0.01)


def test_calculate_recency_one_half_life_is_half() -> None:
    stmt = statement("aging")
    stmt["created_at"] = datetime.now(UTC) - timedelta(seconds=3600)
    assert calculate_recency(stmt, half_life_seconds=3600.0) == pytest.approx(0.5, abs=0.01)


def test_calculate_recency_two_half_lives_is_quarter() -> None:
    stmt = statement("old")
    stmt["created_at"] = datetime.now(UTC) - timedelta(seconds=7200)
    assert calculate_recency(stmt, half_life_seconds=3600.0) == pytest.approx(0.25, abs=0.01)


def test_calculate_recency_hit_refreshes_recency() -> None:
    stmt = statement("revived")
    stmt["created_at"] = datetime.now(UTC) - timedelta(seconds=7200)
    stmt["last_hit"] = datetime.now(UTC)
    assert calculate_recency(stmt, half_life_seconds=3600.0) == pytest.approx(1.0, abs=0.01)


def test_calculate_recency_stable_under_store_changes() -> None:
    # Recency depends only on the statement's own timestamps, so it does
    # not shift when other statements are stored or evicted.
    stmt = statement("stable")
    stmt["created_at"] = datetime.now(UTC) - timedelta(seconds=1800)
    first = calculate_recency(stmt, half_life_seconds=3600.0)
    second = calculate_recency(stmt, half_life_seconds=3600.0)
    assert first == pytest.approx(second, abs=0.01)


"""Tests for average hit rate calculation."""


def test_calculate_average_hit_rate_single_keyword() -> None:
    keyword_index = {
        "paris": keyword_entry(keyword="paris", query_count=100, hit_count=90),
    }
    result = calculate_average_hit_rate(["paris"], keyword_index)
    assert result == 0.9


def test_calculate_average_hit_rate_multiple_keywords() -> None:
    keyword_index = {
        "paris": keyword_entry(keyword="paris", query_count=100, hit_count=90),
        "france": keyword_entry(keyword="france", query_count=100, hit_count=80),
    }
    result = calculate_average_hit_rate(["paris", "france"], keyword_index)
    assert result == pytest.approx(0.85)  # (0.9 + 0.8) / 2


def test_calculate_average_hit_rate_no_matches() -> None:
    result = calculate_average_hit_rate([], {})
    assert result == 0.5  # Default


def test_calculate_average_hit_rate_missing_keyword_entry() -> None:
    result = calculate_average_hit_rate(["paris"], {})
    assert result == 0.5  # Default for unknown keywords


"""Tests for calibrated statement scoring."""


def _score_statement_score(stmt, query_keywords, keyword_index=(), synonyms=(), total=1):
    result = score_statement(
        statement=stmt,
        query_keywords=query_keywords,
        keyword_index=dict(keyword_index or ()),
        total_statements=total,
        weight_base=0.5,
        weight_recency=0.3,
        weight_hit_rate=0.2,
        recency_half_life_seconds=604800.0,
        synonyms=synonyms,
    )
    return result


def test_score_statement_perfect_fresh_match_bounds() -> None:
    stmt = statement("France has a population of 67 million", keywords=["france", "population"])
    keyword_index = {
        "population": keyword_entry(keyword="population", statement_ids=[stmt["id"]], query_count=50, hit_count=45),
        "france": keyword_entry(keyword="france", statement_ids=[stmt["id"]], query_count=150, hit_count=140),
    }
    score = _score_statement_score(stmt, ["population", "france"], keyword_index, total=3)
    # Full overlap, fresh recency, high hit rates: close to 1.0, never above.
    assert 0.9 < score <= 1.0


def test_score_statement_score_is_calibrated_to_unit_interval() -> None:
    stmt = statement("Test statement", keywords=["test", "statement"])
    score = _score_statement_score(stmt, ["test"])
    assert 0.0 < score <= 1.0


def test_score_statement_zero_overlap() -> None:
    stmt = statement("Hello world", keywords=["hello", "world"])
    assert _score_statement_score(stmt, ["goodbye"]) == 0.0


def test_score_statement_partial_match_scores_below_full_match() -> None:
    stmt = statement("Test statement", keywords=["test", "statement"])
    partial = _score_statement_score(stmt, ["test", "missing"])
    full = _score_statement_score(stmt, ["test", "statement"])
    assert 0.0 < partial < full <= 1.0


def test_score_statement_synonym_only_match_scores_positive() -> None:
    stmt = statement("The automobile is fast", keywords=["automobile", "fast"])
    without = _score_statement_score(stmt, ["car"])
    with_synonyms = _score_statement_score(stmt, ["car"], synonyms={"car": ("automobile", "auto")})
    assert without == 0.0
    assert 0.0 < with_synonyms < 1.0


def test_score_statement_older_statement_scores_lower() -> None:
    fresh = statement("fresh entry", keywords=["shared", "topic"])
    stale = statement("stale entry", keywords=["shared", "topic"])
    stale["created_at"] = datetime.now(UTC) - timedelta(days=30)
    assert _score_statement_score(fresh, ["shared", "topic"]) > _score_statement_score(stale, ["shared", "topic"])


def test_score_statement_priority_added_to_matching_statement() -> None:
    plain = statement("plain answer", keywords=["alpha"])
    boosted = statement("boosted answer", keywords=["alpha"], priority=1)
    assert _score_statement_score(boosted, ["alpha"]) == pytest.approx(_score_statement_score(plain, ["alpha"]) + 1)
    assert _score_statement_score(boosted, ["alpha"]) > 1.0


def test_score_statement_priority_ignored_without_overlap() -> None:
    boosted = statement("boosted answer", keywords=["alpha"], priority=5)
    assert _score_statement_score(boosted, ["unrelated"]) == 0.0


def test_score_statement_custom_weights_still_calibrated() -> None:
    # Weights that do not sum to 1.0 are normalized by the formula.
    stmt = statement("Test statement", keywords=["test"])
    score = score_statement(
        statement=stmt,
        query_keywords=["test"],
        keyword_index={},
        total_statements=1,
        weight_base=2.0,
        weight_recency=1.0,
        weight_hit_rate=1.0,
        recency_half_life_seconds=604800.0,
    )
    assert 0.0 < score <= 1.0
