"""Behavioral temporal-query contract and parser tests for EGR-901 and EGR-902."""

import json
from datetime import UTC, datetime

import pytest

from engram.constants import TemporalAxis, TemporalQueryOperator
from engram.contextual import (
    compact_query_frame_from_dict,
    compact_query_frame_from_frame,
    compact_query_frame_to_dict,
    enrich_query_frame,
)
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.resolution import QueryFrameBuilder, query_frame_from_json, query_frame_to_json
from engram.temporal import parse_temporal_query, temporal_query, temporal_query_from_dict, temporal_query_to_dict

NOW = datetime(2026, 8, 20, 16, 0, tzinfo=UTC)


def _frame(request: str):
    engine = Engram()
    result = QueryFrameBuilder(engine, lambda: 1, lambda: NOW).build(request, diagnostic_seed=request)
    return result


@pytest.mark.parametrize(
    ("input_text", "operator", "start", "end"),
    [
        ("Who owns Atlas currently?", TemporalQueryOperator.CURRENT, "", ""),
        ("Who owns Atlas now?", TemporalQueryOperator.NOW, "", ""),
        ("Who owned Atlas as of 2024?", TemporalQueryOperator.AS_OF, "2024-12-31T23:59:59.999999Z", ""),
        ("Who owned Atlas in 2024?", TemporalQueryOperator.IN_YEAR, "2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
        ("Who owned Atlas before 2024?", TemporalQueryOperator.BEFORE, "", "2024-01-01T00:00:00Z"),
        ("Who owned Atlas after 2024?", TemporalQueryOperator.AFTER, "2025-01-01T00:00:00Z", ""),
        (
            "Who owned Atlas between 2023 and 2024?",
            TemporalQueryOperator.BETWEEN,
            "2023-01-01T00:00:00Z",
            "2025-01-01T00:00:00Z",
        ),
        ("Who was the latest Atlas owner?", TemporalQueryOperator.LATEST, "", ""),
    ],
)
def test_parser_represents_each_temporal_operator_with_normalized_bounds(input_text, operator, start, end) -> None:
    result = parse_temporal_query(input_text)

    assert result["operator"] == operator
    assert result["axis"] == TemporalAxis.VALID_TIME
    assert result["start"] == start
    assert result["start_available"] is bool(start)
    assert result["end"] == end
    assert result["end_available"] is bool(end)
    assert result["resolved"] is True
    assert result["confidence"] == 1.0
    assert result["source_text"] in input_text


def test_parser_distinguishes_system_observation_time_from_valid_time() -> None:
    result = parse_temporal_query("Who was recorded as known on 2024-05-01?")

    assert result["operator"] == TemporalQueryOperator.AS_OF
    assert result["axis"] == TemporalAxis.SYSTEM_TIME
    assert result["start"] == "2024-05-01T23:59:59.999999Z"


def test_parser_preserves_unresolved_expression_and_conflicting_qualifiers() -> None:
    unresolved = parse_temporal_query("Who owned Atlas as of last summer?")
    conflicting = parse_temporal_query("Who owns Atlas currently in 2024?")

    assert unresolved["operator"] == TemporalQueryOperator.AS_OF
    assert unresolved["source_text"] == "as of last summer"
    assert unresolved["resolved"] is False
    assert unresolved["start_available"] is False
    assert unresolved["confidence"] == 0.0
    assert conflicting["resolved"] is False
    assert "currently" in conflicting["source_text"]
    assert "in 2024" in conflicting["source_text"]


def test_bare_year_is_a_bounded_in_year_follow_up() -> None:
    result = parse_temporal_query("2024?")

    assert result["operator"] == TemporalQueryOperator.IN_YEAR
    assert result["start"] == "2024-01-01T00:00:00Z"
    assert result["end"] == "2025-01-01T00:00:00Z"


def test_temporal_contract_round_trips_and_rejects_inconsistent_bounds() -> None:
    value = parse_temporal_query("between 2023-04-01 and 2023-04-30")

    assert temporal_query_from_dict(temporal_query_to_dict(value)) == value
    with pytest.raises(InvalidRequestError):
        temporal_query(operator=TemporalQueryOperator.BEFORE, source_text="before 2024", confidence=1.0)
    with pytest.raises(InvalidRequestError):
        temporal_query(
            operator=TemporalQueryOperator.BETWEEN,
            source_text="between 2025 and 2024",
            start="2025-01-01T00:00:00Z",
            start_available=True,
            end="2024-01-01T00:00:00Z",
            end_available=True,
            confidence=1.0,
        )


def test_query_frame_keeps_temporal_interpretation_out_of_lexical_terms_and_round_trips() -> None:
    frame = _frame("Who owned Atlas in 2024?")

    assert frame["temporal_query"]["operator"] == TemporalQueryOperator.IN_YEAR
    assert "2024" not in frame["identity"]["lexical_terms"]
    assert query_frame_from_json(query_frame_to_json(frame)) == frame
    serialized = json.loads(query_frame_to_json(frame))
    assert serialized["temporal_query"]["operator"] == "in_year"


def test_compact_frame_round_trip_preserves_temporal_query() -> None:
    frame = enrich_query_frame(_frame("Who owned Atlas in 2024?"), current_turn=1)
    compact = compact_query_frame_from_frame(frame, source_turn=1)

    assert compact_query_frame_from_dict(compact_query_frame_to_dict(compact)) == compact
    assert compact["temporal_query"]["operator"] == TemporalQueryOperator.IN_YEAR


def test_temporal_follow_up_replaces_prior_time_and_inherits_subject_relation() -> None:
    first = enrich_query_frame(_frame("Who owned Atlas in 2025?"), current_turn=1)
    compact = compact_query_frame_from_frame(first, source_turn=1)
    second = enrich_query_frame(_frame("2024?"), previous=compact, current_turn=2)

    assert second["identity"]["entities"] == first["identity"]["entities"]
    assert second["identity"]["relation"] == first["identity"]["relation"]
    assert second["temporal_query"]["operator"] == TemporalQueryOperator.IN_YEAR
    assert second["temporal_query"]["start"] == "2024-01-01T00:00:00Z"
    assert "temporal_query" not in {value["field_name"] for value in second["inheritance"]}


def test_elliptical_follow_up_inherits_prior_temporal_query() -> None:
    first = enrich_query_frame(_frame("Who owned Atlas in 2024?"), current_turn=1)
    compact = compact_query_frame_from_frame(first, source_turn=1)
    second = enrich_query_frame(_frame("And who managed it?"), previous=compact, current_turn=2)

    assert second["temporal_query"] == first["temporal_query"]
    assert "temporal_query" in {value["field_name"] for value in second["inheritance"]}
