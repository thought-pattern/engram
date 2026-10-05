"""Behavior tests for temporal selection, supplied trust, and conflicts."""

from pytest import mark as pytest_mark

from engram.constants import PredicateCardinality, RelationSelectionReason
from engram.graph import relation_proposition_projection_from_graph_row
from engram.relation import select_relation_propositions
from engram.temporal import parse_temporal_query

# One current, trusted, single-valued account-status graph row; tests override the fields they vary.
ACTIVE_STATUS_ROW = {
    "proposition_id": "proposition:active",
    "subject_entity_id": "entity:account",
    "predicate_id": "predicate:status",
    "object_entity_id": "entity:active",
    "polarity": "positive",
    "modality_family": "none",
    "modality_operator": "none",
    "argument_count": 2,
    "qualification_count": 0,
    "context_count": 0,
    "applicability_count": 0,
    "invalidated_at": "",
    "invalidated_at_available": False,
    "system_from": "2020-01-01T00:00:00Z",
    "system_from_available": True,
    "system_to": "",
    "system_to_available": False,
    "valid_from": "2020-01-01T00:00:00Z",
    "valid_from_available": True,
    "valid_to": "2030-01-01T00:00:00Z",
    "valid_to_available": True,
    "predicate_canonical": True,
    "ownership_category": "PUBLIC",
    "trust_category": "source_supplied",
    "trust_category_available": True,
    "supplied_trust": 0.8,
    "supplied_trust_available": True,
    "structured_match": 1.0,
    "structured_match_available": True,
    "semantic_similarity": 0.0,
    "semantic_similarity_available": False,
    "object_label": "Active",
    "object_type": "ENTITY",
    "predicate_cardinality": "SINGLE",
}
UNTRUSTED_FIELDS = {
    "trust_category": "",
    "trust_category_available": False,
    "supplied_trust": 0.0,
    "supplied_trust_available": False,
}
SELECTION_FIELDS = {
    "direct_answer",
    "selected_proposition_id",
    "evidence_proposition_ids",
    "conflict_proposition_ids",
    "ranking_proposition_ids",
    "reason",
    "cardinality",
}


def test_unique_current_proposition_requires_explicit_supplied_trust_for_direct_phrasing() -> None:
    trusted = select_relation_propositions(
        (relation_proposition_projection_from_graph_row(ACTIVE_STATUS_ROW),),
        parse_temporal_query("What is the status now?"),
    )
    missing = select_relation_propositions(
        (relation_proposition_projection_from_graph_row({**ACTIVE_STATUS_ROW, **UNTRUSTED_FIELDS}),),
        parse_temporal_query("What is the status now?"),
    )

    assert trusted.keys() >= SELECTION_FIELDS
    assert missing.keys() >= SELECTION_FIELDS
    assert trusted.get("direct_answer", False) is True
    assert trusted.get("selected_proposition_id", "") == "proposition:active"
    assert missing.get("direct_answer", False) is False
    assert missing.get("reason", RelationSelectionReason.NO_ELIGIBLE_PROPOSITION) == RelationSelectionReason.TRUST_UNAVAILABLE


def test_same_object_propositions_use_only_comparable_supplied_trust_for_ranking() -> None:
    lower = relation_proposition_projection_from_graph_row(
        {**ACTIVE_STATUS_ROW, "proposition_id": "proposition:lower", "supplied_trust": 0.6}
    )
    higher = relation_proposition_projection_from_graph_row(
        {**ACTIVE_STATUS_ROW, "proposition_id": "proposition:higher", "supplied_trust": 0.9}
    )

    selection = select_relation_propositions((lower, higher), parse_temporal_query("What is the status?"))

    assert selection.keys() >= SELECTION_FIELDS
    assert selection.get("direct_answer", False) is True
    assert selection.get("selected_proposition_id", "") == "proposition:higher"
    assert selection.get("ranking_proposition_ids", ()) == ("proposition:higher", "proposition:lower")
    assert selection.get("reason", RelationSelectionReason.NO_ELIGIBLE_PROPOSITION) == RelationSelectionReason.SELECTED_TRUST_RANKED


@pytest_mark.parametrize(
    ("cardinality", "reason", "conflict"),
    [
        ("SINGLE", RelationSelectionReason.CONFLICT_SINGLE_VALUE, True),
        ("MULTI", RelationSelectionReason.VALID_MULTI_VALUE, False),
        ("UNKNOWN", RelationSelectionReason.CARDINALITY_UNKNOWN, False),
    ],
)
def test_incompatible_objects_respect_supplied_predicate_cardinality(cardinality, reason, conflict) -> None:
    first = relation_proposition_projection_from_graph_row({**ACTIVE_STATUS_ROW, "predicate_cardinality": cardinality})
    second = relation_proposition_projection_from_graph_row(
        {
            **ACTIVE_STATUS_ROW,
            "proposition_id": "proposition:paused",
            "object_entity_id": "entity:paused",
            "object_label": "Paused",
            "predicate_cardinality": cardinality,
        }
    )

    selection = select_relation_propositions((first, second), parse_temporal_query("What is the status now?"))

    assert selection.keys() >= SELECTION_FIELDS
    assert selection.get("direct_answer", False) is False
    assert selection.get("reason", RelationSelectionReason.NO_ELIGIBLE_PROPOSITION) == reason
    assert selection.get("evidence_proposition_ids", ()) == ("proposition:active", "proposition:paused")
    assert bool(selection.get("conflict_proposition_ids", ())) is conflict
    assert selection.get("cardinality", PredicateCardinality.UNKNOWN) == PredicateCardinality(cardinality)


def test_bounded_history_distinguishes_successive_values_from_overlapping_conflict() -> None:
    older = relation_proposition_projection_from_graph_row(
        {
            **ACTIVE_STATUS_ROW,
            "proposition_id": "proposition:older",
            "valid_from": "2023-01-01T00:00:00Z",
            "valid_to": "2024-01-01T00:00:00Z",
        }
    )
    newer = relation_proposition_projection_from_graph_row(
        {
            **ACTIVE_STATUS_ROW,
            "proposition_id": "proposition:newer",
            "object_entity_id": "entity:paused",
            "object_label": "Paused",
            "valid_from": "2024-01-01T00:00:00Z",
            "valid_to": "2025-01-01T00:00:00Z",
        }
    )

    selection = select_relation_propositions((older, newer), parse_temporal_query("What was the status between 2023 and 2024?"))

    reason = selection.get("reason", RelationSelectionReason.NO_ELIGIBLE_PROPOSITION)

    assert selection.keys() >= SELECTION_FIELDS
    assert selection.get("direct_answer", False) is False
    assert reason == RelationSelectionReason.BOUNDED_MULTIPLE_PERIODS
    assert selection.get("conflict_proposition_ids", ()) == ()


def test_latest_selects_the_greatest_available_lower_bound_and_suppresses_distinct_ties() -> None:
    older = relation_proposition_projection_from_graph_row(
        {**ACTIVE_STATUS_ROW, "proposition_id": "proposition:older", "valid_from": "2023-01-01T00:00:00Z"}
    )
    latest = relation_proposition_projection_from_graph_row(
        {
            **ACTIVE_STATUS_ROW,
            "proposition_id": "proposition:latest",
            "object_entity_id": "entity:paused",
            "object_label": "Paused",
            "valid_from": "2025-01-01T00:00:00Z",
        }
    )
    tied = relation_proposition_projection_from_graph_row(
        {
            **ACTIVE_STATUS_ROW,
            "proposition_id": "proposition:tied",
            "object_entity_id": "entity:closed",
            "object_label": "Closed",
            "valid_from": "2025-01-01T00:00:00Z",
        }
    )
    first_tag = relation_proposition_projection_from_graph_row(
        {
            **ACTIVE_STATUS_ROW,
            "proposition_id": "proposition:first-tag",
            "object_entity_id": "entity:red",
            "object_label": "Red",
            "predicate_cardinality": "MULTI",
            "valid_from": "2025-01-01T00:00:00Z",
        }
    )
    second_tag = relation_proposition_projection_from_graph_row(
        {
            **ACTIVE_STATUS_ROW,
            "proposition_id": "proposition:second-tag",
            "object_entity_id": "entity:blue",
            "object_label": "Blue",
            "predicate_cardinality": "MULTI",
            "valid_from": "2025-01-01T00:00:00Z",
        }
    )
    temporal = parse_temporal_query("What is the latest status?")

    selected = select_relation_propositions((older, latest), temporal)
    tie = select_relation_propositions((older, latest, tied), temporal)
    multi_latest = select_relation_propositions((first_tag, second_tag), temporal)

    assert selected.keys() >= SELECTION_FIELDS
    assert tie.keys() >= SELECTION_FIELDS
    assert multi_latest.keys() >= SELECTION_FIELDS
    assert selected.get("direct_answer", False) is True
    assert selected.get("selected_proposition_id", "") == "proposition:latest"
    assert selected.get("reason", RelationSelectionReason.NO_ELIGIBLE_PROPOSITION) == RelationSelectionReason.SELECTED_LATEST
    assert tie.get("direct_answer", False) is False
    assert tie.get("reason", RelationSelectionReason.NO_ELIGIBLE_PROPOSITION) == RelationSelectionReason.LATEST_TIE
    assert multi_latest.get("reason", RelationSelectionReason.NO_ELIGIBLE_PROPOSITION) == RelationSelectionReason.VALID_MULTI_VALUE
    assert multi_latest.get("conflict_proposition_ids", ()) == ()


def test_latest_and_historical_direct_selection_abstain_on_missing_or_open_bounds() -> None:
    missing_latest = relation_proposition_projection_from_graph_row(
        {**ACTIVE_STATUS_ROW, "proposition_id": "proposition:missing", "valid_from": "", "valid_from_available": False}
    )
    open_history = relation_proposition_projection_from_graph_row(
        {**ACTIVE_STATUS_ROW, "proposition_id": "proposition:open", "valid_to": "", "valid_to_available": False}
    )

    latest = select_relation_propositions((missing_latest,), parse_temporal_query("What is the latest status?"))
    historical = select_relation_propositions((open_history,), parse_temporal_query("What was the status in 2024?"))

    assert latest.keys() >= SELECTION_FIELDS
    assert historical.keys() >= SELECTION_FIELDS
    assert latest.get("reason", RelationSelectionReason.NO_ELIGIBLE_PROPOSITION) == RelationSelectionReason.LATEST_BOUND_UNAVAILABLE
    assert historical.get("reason", RelationSelectionReason.NO_ELIGIBLE_PROPOSITION) == RelationSelectionReason.TEMPORAL_BOUNDS_OPEN
    assert latest.get("direct_answer", False) is historical.get("direct_answer", False) is False


def test_system_time_latest_ranks_system_bounds_instead_of_valid_bounds() -> None:
    older_system = relation_proposition_projection_from_graph_row(
        {
            **ACTIVE_STATUS_ROW,
            "proposition_id": "proposition:older-system",
            "system_from": "2023-01-01T00:00:00Z",
            "valid_from": "2025-01-01T00:00:00Z",
        }
    )
    newer_system = relation_proposition_projection_from_graph_row(
        {**ACTIVE_STATUS_ROW, "proposition_id": "proposition:newer-system", "system_from": "2024-01-01T00:00:00Z"}
    )

    selection = select_relation_propositions(
        (older_system, newer_system),
        parse_temporal_query("What is the latest status by system time?"),
    )

    assert selection.keys() >= SELECTION_FIELDS
    assert selection.get("selected_proposition_id", "") == "proposition:newer-system"
    assert selection.get("direct_answer", False) is True
