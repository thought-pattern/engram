"""Behavioral temporal-query contract and parser tests for EGR-901 and EGR-902."""

from datetime import UTC, datetime

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.constants import TemporalAxis, TemporalQueryOperator
from engram.contextual import (
    compact_query_frame_from_dict,
    compact_query_frame_from_frame,
    compact_query_frame_to_dict,
    enrich_query_frame,
)
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.resolution import QueryFrameBuilder
from engram.temporal import parse_temporal_query, temporal_query, temporal_query_from_dict, temporal_query_to_dict

NOW = datetime(2026, 8, 20, 16, 0, tzinfo=UTC)
TEMPORAL_QUERY_FIELDS = {
    "operator",
    "axis",
    "start",
    "start_available",
    "end",
    "end_available",
    "resolved",
    "confidence",
    "source_text",
}


@pytest_mark.parametrize(
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

    assert result.keys() >= TEMPORAL_QUERY_FIELDS
    assert result.get("operator", TemporalQueryOperator.UNSPECIFIED) == operator
    assert result.get("axis", TemporalAxis.VALID_TIME) == TemporalAxis.VALID_TIME
    assert result.get("start", "") == start
    assert result.get("start_available", False) is bool(start)
    assert result.get("end", "") == end
    assert result.get("end_available", False) is bool(end)
    assert result.get("resolved", False) is True
    assert result.get("confidence", 0.0) == 1.0
    assert result.get("source_text", "") in input_text


def test_parser_distinguishes_system_observation_time_from_valid_time() -> None:
    result = parse_temporal_query("Who was recorded as known on 2024-05-01?")

    assert result.get("operator", TemporalQueryOperator.UNSPECIFIED) == TemporalQueryOperator.AS_OF
    assert result.get("axis", TemporalAxis.VALID_TIME) == TemporalAxis.SYSTEM_TIME
    assert result.get("start", "") == "2024-05-01T23:59:59.999999Z"


def test_parser_preserves_unresolved_expression_and_conflicting_qualifiers() -> None:
    unresolved = parse_temporal_query("Who owned Atlas as of last summer?")
    conflicting = parse_temporal_query("Who owns Atlas currently in 2024?")

    assert unresolved.keys() >= TEMPORAL_QUERY_FIELDS
    assert conflicting.keys() >= TEMPORAL_QUERY_FIELDS
    assert unresolved.get("operator", TemporalQueryOperator.UNSPECIFIED) == TemporalQueryOperator.AS_OF
    assert unresolved.get("source_text", "") == "as of last summer"
    assert unresolved.get("resolved", False) is False
    assert unresolved.get("start_available", False) is False
    assert unresolved.get("confidence", 0.0) == 0.0
    assert conflicting.get("resolved", False) is False
    assert "currently" in conflicting.get("source_text", "")
    assert "in 2024" in conflicting.get("source_text", "")


def test_bare_year_is_a_bounded_in_year_follow_up() -> None:
    result = parse_temporal_query("2024?")

    assert result.get("operator", TemporalQueryOperator.UNSPECIFIED) == TemporalQueryOperator.IN_YEAR
    assert result.get("start", "") == "2024-01-01T00:00:00Z"
    assert result.get("end", "") == "2025-01-01T00:00:00Z"


def test_temporal_parser_treats_tabs_and_line_breaks_as_spaces() -> None:
    result = parse_temporal_query("Who owned Atlas\tin 2024?\n")
    frame = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build("What is Engram?\n", diagnostic_seed="What is Engram?\n")

    assert result.get("operator", TemporalQueryOperator.UNSPECIFIED) == TemporalQueryOperator.IN_YEAR
    assert frame.get("resolved_text", "")
    with pytest_raises(InvalidRequestError, match="control character"):
        parse_temporal_query("What is\x00Engram?")


def test_long_unresolved_expression_is_shortened_not_rejected() -> None:
    request = "What changed before " + "the long migration window " * 30

    result = parse_temporal_query(request)
    frame = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build(request, diagnostic_seed=request)
    frame_temporal_query = frame.get("temporal_query", {})

    assert result.keys() >= TEMPORAL_QUERY_FIELDS
    assert frame_temporal_query.keys() >= TEMPORAL_QUERY_FIELDS
    assert result.get("resolved", False) is False
    assert result.get("operator", TemporalQueryOperator.UNSPECIFIED) == TemporalQueryOperator.BEFORE
    assert 0 < len(result.get("source_text", "").encode("utf-8")) <= 512
    assert frame_temporal_query.get("resolved", False) is False


def test_a_full_date_after_in_is_not_read_as_its_whole_year() -> None:
    result = parse_temporal_query("Who owned Atlas in 2024-05-01?")

    assert result.keys() >= TEMPORAL_QUERY_FIELDS
    assert result.get("operator", TemporalQueryOperator.UNSPECIFIED) != TemporalQueryOperator.IN_YEAR
    assert result.get("start", "") != "2024-01-01T00:00:00Z"


def test_temporal_contract_round_trips_and_rejects_inconsistent_bounds() -> None:
    value = parse_temporal_query("between 2023-04-01 and 2023-04-30")

    assert temporal_query_from_dict(temporal_query_to_dict(value)) == value
    with pytest_raises(InvalidRequestError):
        temporal_query(operator=TemporalQueryOperator.BEFORE, source_text="before 2024", confidence=1.0)
    with pytest_raises(InvalidRequestError):
        temporal_query(
            operator=TemporalQueryOperator.BETWEEN,
            source_text="between 2025 and 2024",
            start="2025-01-01T00:00:00Z",
            start_available=True,
            end="2024-01-01T00:00:00Z",
            end_available=True,
            confidence=1.0,
        )


def test_query_frame_keeps_temporal_interpretation_out_of_lexical_terms() -> None:
    request = "Who owned Atlas in 2024?"
    frame = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build(request, diagnostic_seed=request)
    identity = frame.get("identity", {})

    assert frame.get("temporal_query", {}).get("operator", TemporalQueryOperator.UNSPECIFIED) == TemporalQueryOperator.IN_YEAR
    assert "lexical_terms" in identity
    assert "2024" not in identity.get("lexical_terms", ())


def test_compact_frame_round_trip_preserves_temporal_query() -> None:
    request = "Who owned Atlas in 2024?"
    built = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build(request, diagnostic_seed=request)
    frame = enrich_query_frame(built, current_turn=1)
    compact = compact_query_frame_from_frame(frame, source_turn=1)

    assert compact_query_frame_from_dict(compact_query_frame_to_dict(compact)) == compact
    assert compact.get("temporal_query", {}).get("operator", TemporalQueryOperator.UNSPECIFIED) == TemporalQueryOperator.IN_YEAR


def test_temporal_follow_up_replaces_prior_time_and_inherits_subject_relation() -> None:
    first_request = "Who owned Atlas in 2025?"
    first_built = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build(first_request, diagnostic_seed=first_request)
    first = enrich_query_frame(first_built, current_turn=1)
    compact = compact_query_frame_from_frame(first, source_turn=1)
    second_built = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build("2024?", diagnostic_seed="2024?")
    second = enrich_query_frame(second_built, previous=compact, current_turn=2)
    first_identity = first.get("identity", {})
    second_identity = second.get("identity", {})
    second_temporal_query = second.get("temporal_query", {})
    inheritance = second.get("inheritance", ())

    assert {"entities", "relation"} <= first_identity.keys()
    assert {"entities", "relation"} <= second_identity.keys()
    assert second_identity.get("entities", ()) == first_identity.get("entities", ())
    assert second_identity.get("relation", {}) == first_identity.get("relation", {})
    assert second_temporal_query.get("operator", TemporalQueryOperator.UNSPECIFIED) == TemporalQueryOperator.IN_YEAR
    assert second_temporal_query.get("start", "") == "2024-01-01T00:00:00Z"
    assert "inheritance" in second
    assert all("field_name" in value for value in inheritance)
    assert "temporal_query" not in {value.get("field_name", "") for value in inheritance}


def test_elliptical_follow_up_inherits_prior_temporal_query() -> None:
    first_request = "Who owned Atlas in 2024?"
    first_built = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build(first_request, diagnostic_seed=first_request)
    first = enrich_query_frame(first_built, current_turn=1)
    compact = compact_query_frame_from_frame(first, source_turn=1)
    second_request = "And who managed it?"
    second_built = QueryFrameBuilder(Engram(), lambda: 1, lambda: NOW).build(second_request, diagnostic_seed=second_request)
    second = enrich_query_frame(second_built, previous=compact, current_turn=2)

    assert "temporal_query" in first
    assert "temporal_query" in second
    assert second.get("temporal_query", {}) == first.get("temporal_query", {})
    assert "temporal_query" in {value.get("field_name", "") for value in second.get("inheritance", ())}
