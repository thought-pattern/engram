"""Behavior tests for temporal selection, supplied trust, and conflicts."""

import pytest

from engram.constants import PredicateCardinality, RelationSelectionReason
from engram.graph import RelationPropositionProjection, relation_proposition_projection_from_graph_row
from engram.relation import select_relation_propositions
from engram.temporal import parse_temporal_query


def _item(
    proposition_id: str,
    object_id: str,
    *,
    cardinality: str = "SINGLE",
    trust: float = 0.8,
    trust_available: bool = True,
    trust_version: int = 1,
    valid_from: str = "2020-01-01T00:00:00Z",
    valid_to: str = "2030-01-01T00:00:00Z",
    system_from: str = "2020-01-01T00:00:00Z",
    system_to: str = "",
) -> RelationPropositionProjection:
    row = {
        "proposition_id": proposition_id,
        "subject_entity_id": "entity:account",
        "predicate_id": "predicate:status",
        "object_entity_id": object_id,
        "invalidated_at": "",
        "invalidated_at_available": False,
        "system_from": system_from,
        "system_from_available": bool(system_from),
        "system_to": system_to,
        "system_to_available": bool(system_to),
        "valid_from": valid_from,
        "valid_from_available": bool(valid_from),
        "valid_to": valid_to,
        "valid_to_available": bool(valid_to),
        "predicate_canonical": True,
        "ownership_category": "PUBLIC",
        "trust_category": "source_supplied" if trust_available else "",
        "trust_category_available": trust_available,
        "supplied_trust": trust if trust_available else 0.0,
        "supplied_trust_available": trust_available,
        "supplied_trust_version": trust_version if trust_available else 0,
        "supplied_trust_version_available": trust_available,
        "structured_match": 1.0,
        "structured_match_available": True,
        "semantic_similarity": 0.0,
        "semantic_similarity_available": False,
        "object_label": object_id.removeprefix("entity:").title(),
        "object_type": "ENTITY",
        "predicate_cardinality": cardinality,
    }
    result = relation_proposition_projection_from_graph_row(row)
    return result


def test_unique_current_proposition_requires_explicit_supplied_trust_for_direct_phrasing() -> None:
    trusted = select_relation_propositions(
        (_item("proposition:active", "entity:active"),),
        parse_temporal_query("What is the status now?"),
    )
    missing = select_relation_propositions(
        (_item("proposition:active", "entity:active", trust_available=False),),
        parse_temporal_query("What is the status now?"),
    )

    assert trusted["direct_answer"] is True
    assert trusted["selected_proposition_id"] == "proposition:active"
    assert missing["direct_answer"] is False
    assert missing["reason"] == RelationSelectionReason.TRUST_UNAVAILABLE


def test_same_object_propositions_use_only_comparable_supplied_trust_for_ranking() -> None:
    lower = _item("proposition:lower", "entity:active", trust=0.6)
    higher = _item("proposition:higher", "entity:active", trust=0.9)

    selection = select_relation_propositions((lower, higher), parse_temporal_query("What is the status?"))

    assert selection["direct_answer"] is True
    assert selection["selected_proposition_id"] == "proposition:higher"
    assert selection["ranking_proposition_ids"] == ("proposition:higher", "proposition:lower")
    assert selection["reason"] == RelationSelectionReason.SELECTED_TRUST_RANKED
    assert selection["trust_version"] == 1


def test_trust_versions_are_not_silently_compared() -> None:
    first = _item("proposition:first", "entity:active", trust=0.6, trust_version=1)
    second = _item("proposition:second", "entity:active", trust=0.9, trust_version=2)

    selection = select_relation_propositions((first, second), parse_temporal_query("What is the status?"))

    assert selection["direct_answer"] is False
    assert selection["reason"] == RelationSelectionReason.TRUST_VERSION_INCOMPARABLE
    assert selection["trust_version_available"] is False


@pytest.mark.parametrize(
    ("cardinality", "reason", "conflict"),
    [
        ("SINGLE", RelationSelectionReason.CONFLICT_SINGLE_VALUE, True),
        ("MULTI", RelationSelectionReason.VALID_MULTI_VALUE, False),
        ("UNKNOWN", RelationSelectionReason.CARDINALITY_UNKNOWN, False),
    ],
)
def test_incompatible_objects_respect_supplied_predicate_cardinality(cardinality, reason, conflict) -> None:
    first = _item("proposition:active", "entity:active", cardinality=cardinality)
    second = _item("proposition:paused", "entity:paused", cardinality=cardinality)

    selection = select_relation_propositions((first, second), parse_temporal_query("What is the status now?"))

    assert selection["direct_answer"] is False
    assert selection["reason"] == reason
    assert selection["evidence_proposition_ids"] == ("proposition:active", "proposition:paused")
    assert bool(selection["conflict_proposition_ids"]) is conflict
    assert selection["cardinality"] == PredicateCardinality(cardinality)


def test_bounded_history_distinguishes_successive_values_from_overlapping_conflict() -> None:
    older = _item(
        "proposition:older",
        "entity:active",
        valid_from="2023-01-01T00:00:00Z",
        valid_to="2024-01-01T00:00:00Z",
    )
    newer = _item(
        "proposition:newer",
        "entity:paused",
        valid_from="2024-01-01T00:00:00Z",
        valid_to="2025-01-01T00:00:00Z",
    )

    selection = select_relation_propositions((older, newer), parse_temporal_query("What was the status between 2023 and 2024?"))

    assert selection["direct_answer"] is False
    assert selection["reason"] == RelationSelectionReason.BOUNDED_MULTIPLE_PERIODS
    assert selection["conflict_proposition_ids"] == ()


def test_latest_selects_the_greatest_available_lower_bound_and_suppresses_distinct_ties() -> None:
    older = _item("proposition:older", "entity:active", valid_from="2023-01-01T00:00:00Z")
    latest = _item("proposition:latest", "entity:paused", valid_from="2025-01-01T00:00:00Z")
    tied = _item("proposition:tied", "entity:closed", valid_from="2025-01-01T00:00:00Z")
    temporal = parse_temporal_query("What is the latest status?")

    selected = select_relation_propositions((older, latest), temporal)
    tie = select_relation_propositions((older, latest, tied), temporal)
    multi_latest = select_relation_propositions(
        (
            _item("proposition:first-tag", "entity:red", cardinality="MULTI", valid_from="2025-01-01T00:00:00Z"),
            _item("proposition:second-tag", "entity:blue", cardinality="MULTI", valid_from="2025-01-01T00:00:00Z"),
        ),
        temporal,
    )

    assert selected["direct_answer"] is True
    assert selected["selected_proposition_id"] == "proposition:latest"
    assert selected["reason"] == RelationSelectionReason.SELECTED_LATEST
    assert tie["direct_answer"] is False
    assert tie["reason"] == RelationSelectionReason.LATEST_TIE
    assert multi_latest["reason"] == RelationSelectionReason.VALID_MULTI_VALUE
    assert multi_latest["conflict_proposition_ids"] == ()


def test_latest_and_historical_direct_selection_abstain_on_missing_or_open_bounds() -> None:
    missing_latest = _item("proposition:missing", "entity:active", valid_from="")
    open_history = _item("proposition:open", "entity:active", valid_to="")

    latest = select_relation_propositions((missing_latest,), parse_temporal_query("What is the latest status?"))
    historical = select_relation_propositions((open_history,), parse_temporal_query("What was the status in 2024?"))

    assert latest["reason"] == RelationSelectionReason.LATEST_BOUND_UNAVAILABLE
    assert historical["reason"] == RelationSelectionReason.TEMPORAL_BOUNDS_OPEN
    assert latest["direct_answer"] is historical["direct_answer"] is False


def test_system_time_latest_ranks_system_bounds_instead_of_valid_bounds() -> None:
    older_system = _item(
        "proposition:older-system",
        "entity:active",
        system_from="2023-01-01T00:00:00Z",
        valid_from="2025-01-01T00:00:00Z",
    )
    newer_system = _item(
        "proposition:newer-system",
        "entity:active",
        system_from="2024-01-01T00:00:00Z",
        valid_from="2020-01-01T00:00:00Z",
    )

    selection = select_relation_propositions(
        (older_system, newer_system),
        parse_temporal_query("What is the latest status by system time?"),
    )

    assert selection["selected_proposition_id"] == "proposition:newer-system"
    assert selection["direct_answer"] is True
