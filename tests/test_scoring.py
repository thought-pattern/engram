"""Tests for the calibrated scoring algorithm."""

from datetime import UTC, datetime, timedelta
from math import log as math_log

from pytest import approx as pytest_approx

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


class TestKeywordIdf:
    """Tests for inverse document frequency weighting."""

    def test_rare_keyword_outweighs_common(self) -> bool:
        keyword_index = {
            "rare": keyword_entry(keyword="rare", statement_ids=["s1"]),
            "common": keyword_entry(keyword="common", statement_ids=[f"s{i}" for i in range(100)]),
        }
        assert keyword_idf("rare", keyword_index, 100) > keyword_idf("common", keyword_index, 100)
        return False

    def test_unindexed_keyword_gets_maximum_weight(self) -> bool:
        keyword_index = {
            "known": keyword_entry(keyword="known", statement_ids=["s1", "s2"]),
        }
        assert keyword_idf("unknown", keyword_index, 100) == pytest_approx(math_log(1 + 100))
        assert keyword_idf("unknown", keyword_index, 100) > keyword_idf("known", keyword_index, 100)
        return False

    def test_empty_store_is_positive(self) -> bool:
        assert keyword_idf("anything", {}, 0) > 0
        return False


class TestKeywordMatchWeights:
    """Tests for per-keyword match weighting."""

    def test_exact_match_full_weight(self) -> bool:
        weights = keyword_match_weights(["alpha", "beta"], ["alpha", "gamma"])
        assert weights == {"alpha": 1.0, "beta": 0.0}
        return False

    def test_synonym_match_discounted(self) -> bool:
        weights = keyword_match_weights(
            ["car"],
            ["automobile", "fast"],
            synonyms={"car": ("automobile", "auto")},
        )
        assert weights == {"car": SYNONYM_OVERLAP_WEIGHT}
        return False

    def test_exact_match_beats_synonym(self) -> bool:
        weights = keyword_match_weights(
            ["car"],
            ["car", "automobile"],
            synonyms={"car": ("automobile",)},
        )
        assert weights == {"car": 1.0}
        return False

    def test_no_synonym_map_no_synonym_credit(self) -> bool:
        weights = keyword_match_weights(["car"], ["automobile"])
        assert weights == {"car": 0.0}
        return False


class TestCalculateOverlap:
    """Tests for the IDF-weighted overlap fraction."""

    def test_full_overlap_is_one(self) -> bool:
        match = {"alpha": 1.0, "beta": 1.0}
        assert calculate_overlap(match, {}, 1) == pytest_approx(1.0)
        return False

    def test_no_overlap_is_zero(self) -> bool:
        match = {"alpha": 0.0, "beta": 0.0}
        assert calculate_overlap(match, {}, 1) == 0.0
        return False

    def test_uniform_idf_reduces_to_fraction(self) -> bool:
        # With no index, every keyword has the same IDF, so overlap is the
        # plain matched fraction.
        match = {"alpha": 1.0, "beta": 0.0}
        assert calculate_overlap(match, {}, 1) == pytest_approx(0.5)
        return False

    def test_rare_match_outscores_common_match(self) -> bool:
        keyword_index = {
            "rare": keyword_entry(keyword="rare", statement_ids=["s1"]),
            "common": keyword_entry(keyword="common", statement_ids=[f"s{i}" for i in range(50)]),
        }
        rare_match = calculate_overlap({"rare": 1.0, "common": 0.0}, keyword_index, 50)
        common_match = calculate_overlap({"rare": 0.0, "common": 1.0}, keyword_index, 50)
        assert rare_match > common_match
        return False

    def test_synonym_weight_scales_contribution(self) -> bool:
        exact = calculate_overlap({"car": 1.0}, {}, 1)
        via_synonym = calculate_overlap({"car": SYNONYM_OVERLAP_WEIGHT}, {}, 1)
        assert via_synonym == pytest_approx(exact * SYNONYM_OVERLAP_WEIGHT)
        return False

    def test_empty_query(self) -> bool:
        assert calculate_overlap({}, {}, 1) == 0.0
        return False


class TestCalculateRecency:
    """Tests for time-decay recency."""

    def test_fresh_statement_is_one(self) -> bool:
        stmt = statement("fresh")
        assert calculate_recency(stmt, half_life_seconds=3600.0) == pytest_approx(1.0, abs=0.01)
        return False

    def test_one_half_life_is_half(self) -> bool:
        stmt = statement("aging")
        stmt["created_at"] = datetime.now(UTC) - timedelta(seconds=3600)
        assert calculate_recency(stmt, half_life_seconds=3600.0) == pytest_approx(0.5, abs=0.01)
        return False

    def test_two_half_lives_is_quarter(self) -> bool:
        stmt = statement("old")
        stmt["created_at"] = datetime.now(UTC) - timedelta(seconds=7200)
        assert calculate_recency(stmt, half_life_seconds=3600.0) == pytest_approx(0.25, abs=0.01)
        return False

    def test_hit_refreshes_recency(self) -> bool:
        stmt = statement("revived")
        stmt["created_at"] = datetime.now(UTC) - timedelta(seconds=7200)
        stmt["last_hit"] = datetime.now(UTC)
        assert calculate_recency(stmt, half_life_seconds=3600.0) == pytest_approx(1.0, abs=0.01)
        return False

    def test_stable_under_store_changes(self) -> bool:
        # Recency depends only on the statement's own timestamps, so it does
        # not shift when other statements are stored or evicted.
        stmt = statement("stable")
        stmt["created_at"] = datetime.now(UTC) - timedelta(seconds=1800)
        first = calculate_recency(stmt, half_life_seconds=3600.0)
        second = calculate_recency(stmt, half_life_seconds=3600.0)
        assert first == pytest_approx(second, abs=0.01)
        return False


class TestCalculateAverageHitRate:
    """Tests for average hit rate calculation."""

    def test_single_keyword(self) -> bool:
        keyword_index = {
            "paris": keyword_entry(keyword="paris", query_count=100, hit_count=90),
        }
        result = calculate_average_hit_rate(["paris"], keyword_index)
        assert result == 0.9
        return False

    def test_multiple_keywords(self) -> bool:
        keyword_index = {
            "paris": keyword_entry(keyword="paris", query_count=100, hit_count=90),
            "france": keyword_entry(keyword="france", query_count=100, hit_count=80),
        }
        result = calculate_average_hit_rate(["paris", "france"], keyword_index)
        assert result == pytest_approx(0.85)  # (0.9 + 0.8) / 2
        return False

    def test_no_matches(self) -> bool:
        result = calculate_average_hit_rate([], {})
        assert result == 0.5  # Default
        return False

    def test_missing_keyword_entry(self) -> bool:
        result = calculate_average_hit_rate(["paris"], {})
        assert result == 0.5  # Default for unknown keywords
        return False


class TestScoreStatement:
    """Tests for calibrated statement scoring."""

    def _score(self, stmt, query_keywords, keyword_index=False, synonyms=False, total=1):
        if keyword_index is None:
            keyword_index = False
        _return_value = score_statement(
            statement=stmt,
            query_keywords=query_keywords,
            keyword_index=keyword_index if keyword_index is not False else {},
            total_statements=total,
            weight_base=0.5,
            weight_recency=0.3,
            weight_hit_rate=0.2,
            recency_half_life_seconds=604800.0,
            synonyms=synonyms,
        )
        return _return_value

    def test_perfect_fresh_match_bounds(self) -> bool:
        stmt = statement("France has a population of 67 million", keywords=["france", "population"])
        keyword_index = {
            "population": keyword_entry(keyword="population", statement_ids=[stmt.get("id", "")], query_count=50, hit_count=45),
            "france": keyword_entry(keyword="france", statement_ids=[stmt.get("id", "")], query_count=150, hit_count=140),
        }
        score = self._score(stmt, ["population", "france"], keyword_index, total=3)
        # Full overlap, fresh recency, high hit rates: close to 1.0, never above.
        assert 0.9 < score <= 1.0
        return False

    def test_score_is_calibrated_to_unit_interval(self) -> bool:
        stmt = statement("Test statement", keywords=["test", "statement"])
        score = self._score(stmt, ["test"])
        assert 0.0 < score <= 1.0
        return False

    def test_zero_overlap(self) -> bool:
        stmt = statement("Hello world", keywords=["hello", "world"])
        assert self._score(stmt, ["goodbye"]) == 0.0
        return False

    def test_partial_match_scores_below_full_match(self) -> bool:
        stmt = statement("Test statement", keywords=["test", "statement"])
        partial = self._score(stmt, ["test", "missing"])
        full = self._score(stmt, ["test", "statement"])
        assert 0.0 < partial < full <= 1.0
        return False

    def test_synonym_only_match_scores_positive(self) -> bool:
        stmt = statement("The automobile is fast", keywords=["automobile", "fast"])
        without = self._score(stmt, ["car"])
        with_synonyms = self._score(stmt, ["car"], synonyms={"car": ("automobile", "auto")})
        assert without == 0.0
        assert 0.0 < with_synonyms < 1.0
        return False

    def test_older_statement_scores_lower(self) -> bool:
        fresh = statement("fresh entry", keywords=["shared", "topic"])
        stale = statement("stale entry", keywords=["shared", "topic"])
        stale["created_at"] = datetime.now(UTC) - timedelta(days=30)
        assert self._score(fresh, ["shared", "topic"]) > self._score(stale, ["shared", "topic"])
        return False

    def test_priority_added_to_matching_statement(self) -> bool:
        plain = statement("plain answer", keywords=["alpha"])
        boosted = statement("boosted answer", keywords=["alpha"], priority=1)
        assert self._score(boosted, ["alpha"]) == pytest_approx(self._score(plain, ["alpha"]) + 1)
        assert self._score(boosted, ["alpha"]) > 1.0
        return False

    def test_priority_ignored_without_overlap(self) -> bool:
        boosted = statement("boosted answer", keywords=["alpha"], priority=5)
        assert self._score(boosted, ["unrelated"]) == 0.0
        return False

    def test_custom_weights_still_calibrated(self) -> bool:
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
        return False
