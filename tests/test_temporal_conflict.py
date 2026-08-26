"""Behavior tests for temporal selection, supplied trust, and conflicts."""

import pytest

from engram.constants import PredicateCardinality, RelationSelectionReason
from engram.graph import RelationClaimProjection, relation_claim_projection_from_graph_row
from engram.relation import select_relation_claims
from engram.temporal import parse_temporal_query


def _item(
    claim_id: str,
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
) -> RelationClaimProjection:
    row = {
        "claim_id": claim_id,
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
    result = relation_claim_projection_from_graph_row(row)
    return result


def test_unique_current_claim_requires_explicit_supplied_trust_for_direct_phrasing() -> None:
    trusted = select_relation_claims(
        (_item("claim:active", "entity:active"),),
        parse_temporal_query("What is the status now?"),
    )
    missing = select_relation_claims(
        (_item("claim:active", "entity:active", trust_available=False),),
        parse_temporal_query("What is the status now?"),
    )

    assert trusted["direct_answer"] is True
    assert trusted["selected_claim_id"] == "claim:active"
    assert missing["direct_answer"] is False
    assert missing["reason"] == RelationSelectionReason.TRUST_UNAVAILABLE


def test_same_object_claims_use_only_comparable_supplied_trust_for_ranking() -> None:
    lower = _item("claim:lower", "entity:active", trust=0.6)
    higher = _item("claim:higher", "entity:active", trust=0.9)

    selection = select_relation_claims((lower, higher), parse_temporal_query("What is the status?"))

    assert selection["direct_answer"] is True
    assert selection["selected_claim_id"] == "claim:higher"
    assert selection["ranking_claim_ids"] == ("claim:higher", "claim:lower")
    assert selection["reason"] == RelationSelectionReason.SELECTED_TRUST_RANKED
    assert selection["trust_version"] == 1


def test_trust_versions_are_not_silently_compared() -> None:
    first = _item("claim:first", "entity:active", trust=0.6, trust_version=1)
    second = _item("claim:second", "entity:active", trust=0.9, trust_version=2)

    selection = select_relation_claims((first, second), parse_temporal_query("What is the status?"))

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
    first = _item("claim:active", "entity:active", cardinality=cardinality)
    second = _item("claim:paused", "entity:paused", cardinality=cardinality)

    selection = select_relation_claims((first, second), parse_temporal_query("What is the status now?"))

    assert selection["direct_answer"] is False
    assert selection["reason"] == reason
    assert selection["evidence_claim_ids"] == ("claim:active", "claim:paused")
    assert bool(selection["conflict_claim_ids"]) is conflict
    assert selection["cardinality"] == PredicateCardinality(cardinality)


def test_bounded_history_distinguishes_successive_values_from_overlapping_conflict() -> None:
    older = _item(
        "claim:older",
        "entity:active",
        valid_from="2023-01-01T00:00:00Z",
        valid_to="2024-01-01T00:00:00Z",
    )
    newer = _item(
        "claim:newer",
        "entity:paused",
        valid_from="2024-01-01T00:00:00Z",
        valid_to="2025-01-01T00:00:00Z",
    )

    selection = select_relation_claims((older, newer), parse_temporal_query("What was the status between 2023 and 2024?"))

    assert selection["direct_answer"] is False
    assert selection["reason"] == RelationSelectionReason.BOUNDED_MULTIPLE_PERIODS
    assert selection["conflict_claim_ids"] == ()


def test_latest_selects_the_greatest_available_lower_bound_and_suppresses_distinct_ties() -> None:
    older = _item("claim:older", "entity:active", valid_from="2023-01-01T00:00:00Z")
    latest = _item("claim:latest", "entity:paused", valid_from="2025-01-01T00:00:00Z")
    tied = _item("claim:tied", "entity:closed", valid_from="2025-01-01T00:00:00Z")
    temporal = parse_temporal_query("What is the latest status?")

    selected = select_relation_claims((older, latest), temporal)
    tie = select_relation_claims((older, latest, tied), temporal)
    multi_latest = select_relation_claims(
        (
            _item("claim:first-tag", "entity:red", cardinality="MULTI", valid_from="2025-01-01T00:00:00Z"),
            _item("claim:second-tag", "entity:blue", cardinality="MULTI", valid_from="2025-01-01T00:00:00Z"),
        ),
        temporal,
    )

    assert selected["direct_answer"] is True
    assert selected["selected_claim_id"] == "claim:latest"
    assert selected["reason"] == RelationSelectionReason.SELECTED_LATEST
    assert tie["direct_answer"] is False
    assert tie["reason"] == RelationSelectionReason.LATEST_TIE
    assert multi_latest["reason"] == RelationSelectionReason.VALID_MULTI_VALUE
    assert multi_latest["conflict_claim_ids"] == ()


def test_latest_and_historical_direct_selection_abstain_on_missing_or_open_bounds() -> None:
    missing_latest = _item("claim:missing", "entity:active", valid_from="")
    open_history = _item("claim:open", "entity:active", valid_to="")

    latest = select_relation_claims((missing_latest,), parse_temporal_query("What is the latest status?"))
    historical = select_relation_claims((open_history,), parse_temporal_query("What was the status in 2024?"))

    assert latest["reason"] == RelationSelectionReason.LATEST_BOUND_UNAVAILABLE
    assert historical["reason"] == RelationSelectionReason.TEMPORAL_BOUNDS_OPEN
    assert latest["direct_answer"] is historical["direct_answer"] is False


def test_system_time_latest_ranks_system_bounds_instead_of_valid_bounds() -> None:
    older_system = _item(
        "claim:older-system",
        "entity:active",
        system_from="2023-01-01T00:00:00Z",
        valid_from="2025-01-01T00:00:00Z",
    )
    newer_system = _item(
        "claim:newer-system",
        "entity:active",
        system_from="2024-01-01T00:00:00Z",
        valid_from="2020-01-01T00:00:00Z",
    )

    selection = select_relation_claims(
        (older_system, newer_system),
        parse_temporal_query("What is the latest status by system time?"),
    )

    assert selection["selected_claim_id"] == "claim:newer-system"
    assert selection["direct_answer"] is True
