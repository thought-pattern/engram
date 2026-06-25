"""Tests for scoring algorithm."""

import pytest

from engram.models import KeywordEntry, Statement, Tier
from engram.scoring import (
    calculate_average_hit_rate,
    calculate_overlap,
    calculate_recency,
    score_statement,
)


class TestCalculateOverlap:
    """Tests for overlap calculation."""

    def test_full_overlap(self) -> None:
        query = ["alpha", "beta", "gamma"]
        statement = ["alpha", "beta", "gamma"]
        assert calculate_overlap(query, statement) == 3

    def test_partial_overlap(self) -> None:
        query = ["alpha", "beta", "gamma"]
        statement = ["alpha", "delta"]
        assert calculate_overlap(query, statement) == 1

    def test_no_overlap(self) -> None:
        query = ["alpha", "beta"]
        statement = ["gamma", "delta"]
        assert calculate_overlap(query, statement) == 0

    def test_empty_query(self) -> None:
        assert calculate_overlap([], ["alpha"]) == 0

    def test_empty_statement(self) -> None:
        assert calculate_overlap(["alpha"], []) == 0


class TestCalculateRecency:
    """Tests for recency calculation."""

    def test_first_statement(self) -> None:
        assert calculate_recency(0, 10) == 0.0

    def test_last_statement(self) -> None:
        assert calculate_recency(9, 10) == 1.0

    def test_middle_statement(self) -> None:
        # Index 5 of 11 = 5/10 = 0.5
        assert calculate_recency(5, 11) == 0.5

    def test_single_statement(self) -> None:
        assert calculate_recency(0, 1) == 1.0

    def test_two_statements(self) -> None:
        assert calculate_recency(0, 2) == 0.0
        assert calculate_recency(1, 2) == 1.0


class TestCalculateAverageHitRate:
    """Tests for average hit rate calculation."""

    def test_single_keyword(self) -> None:
        keyword_index = {
            "paris": KeywordEntry(keyword="paris", query_count=100, hit_count=90),
        }
        result = calculate_average_hit_rate(["paris"], ["paris"], keyword_index)
        assert result == 0.9

    def test_multiple_keywords(self) -> None:
        keyword_index = {
            "paris": KeywordEntry(keyword="paris", query_count=100, hit_count=90),
            "france": KeywordEntry(keyword="france", query_count=100, hit_count=80),
        }
        result = calculate_average_hit_rate(["paris", "france"], ["paris", "france"], keyword_index)
        assert result == pytest.approx(0.85)  # (0.9 + 0.8) / 2

    def test_partial_match(self) -> None:
        keyword_index = {
            "paris": KeywordEntry(keyword="paris", query_count=100, hit_count=90),
        }
        # Only "paris" matches between query and statement
        result = calculate_average_hit_rate(["paris", "capital"], ["paris", "city"], keyword_index)
        assert result == 0.9

    def test_no_matches(self) -> None:
        keyword_index = {}
        result = calculate_average_hit_rate(["alpha"], ["beta"], keyword_index)
        assert result == 0.5  # Default

    def test_missing_keyword_entry(self) -> None:
        keyword_index = {}  # Empty index
        result = calculate_average_hit_rate(["paris"], ["paris"], keyword_index)
        assert result == 0.5  # Default for unknown keywords


class TestScoreStatement:
    """Tests for statement scoring."""

    def test_spec_example(self) -> None:
        # Example from spec: "population of france" query
        stmt = Statement(
            "France has a population of 67 million",
            keywords=["france", "population", "67", "million"],
        )

        keyword_index = {
            "population": KeywordEntry(keyword="population", query_count=50, hit_count=45),
            "france": KeywordEntry(keyword="france", query_count=150, hit_count=140),
        }

        # Statement is index 2 of 3 total
        score = score_statement(
            statement=stmt,
            statement_index=2,
            total_statements=3,
            query_keywords=["population", "france"],
            keyword_index=keyword_index,
            weight_base=0.5,
            weight_recency=0.3,
            weight_hit_rate=0.2,
        )

        # Expected:
        # overlap = 2
        # recency = 2/2 = 1.0
        # hit_rate(population) = 45/50 = 0.90
        # hit_rate(france) = 140/150 = 0.9333...
        # avg_hit_rate = (0.90 + 0.9333) / 2 = 0.9167
        # score = 2 * (0.5 + 0.3 * 1.0 + 0.2 * 0.9167)
        #       = 2 * (0.5 + 0.3 + 0.1833)
        #       = 2 * 0.9833
        #       = 1.9667

        assert 1.9 < score < 2.0

    def test_zero_overlap(self) -> None:
        stmt = Statement("Hello world", keywords=["hello", "world"])

        score = score_statement(
            statement=stmt,
            statement_index=0,
            total_statements=1,
            query_keywords=["goodbye"],
            keyword_index={},
            weight_base=0.5,
            weight_recency=0.3,
            weight_hit_rate=0.2,
        )

        assert score == 0.0

    def test_default_weights(self) -> None:
        stmt = Statement("Test statement", keywords=["test", "statement"])

        score = score_statement(
            statement=stmt,
            statement_index=0,
            total_statements=1,
            query_keywords=["test"],
            keyword_index={},
            weight_base=0.5,
            weight_recency=0.3,
            weight_hit_rate=0.2,
        )

        # overlap = 1
        # recency = 1.0 (single statement)
        # hit_rate = 0.5 (default)
        # score = 1 * (0.5 + 0.3 * 1.0 + 0.2 * 0.5) = 1 * 0.9 = 0.9
        assert score == pytest.approx(0.9)
