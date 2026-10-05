"""Section 3 accepted-response artifact and lifecycle contracts."""

from json import loads as json_loads

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.artifacts import (
    MAX_METADATA_DEPTH,
    TERMINAL_LIFECYCLE_STATES,
    LifecycleDecisionReason,
    LifecycleOperation,
    LifecycleState,
    artifact_provenance_from_dict,
    artifact_provenance_to_dict,
    artifact_statistics_from_dict,
    artifact_statistics_to_dict,
    cached_response_artifact_from_dict,
    cached_response_artifact_to_dict,
    lifecycle_after_capacity_eviction,
    lifecycle_base_eligibility,
    lifecycle_transition_decision,
    require_lifecycle_transition,
    validate_artifact_provenance,
    validate_artifact_statistics,
    validate_cached_response_artifact,
    validate_lifecycle_base_decision,
    validate_lifecycle_transition_decision,
)
from engram.errors import InvalidRequestError, LifecycleError
from engram.identity import extract_standalone_identity, scope_key
from tests.support_fixtures import ACCEPTED_ARTIFACT_FIELDS, ASSERTION_REFERENCE_A, ASSERTION_REFERENCE_B

# The shared accepted artifact with two support references, listed in the order the artifact must preserve.
ARTIFACT_FIELDS = {**ACCEPTED_ARTIFACT_FIELDS, "support_references": (ASSERTION_REFERENCE_B, ASSERTION_REFERENCE_A)}


def none_paths(value, path="root") -> list[str]:
    if value is None:
        result = [path]
        return result
    if isinstance(value, dict):
        result = [nested for key, item in value.items() for nested in none_paths(item, f"{path}.{key}")]
        return result
    if isinstance(value, (list, tuple, set)):
        result = [nested for index, item in enumerate(value) for nested in none_paths(item, f"{path}[{index}]")]
        return result
    result = []
    return result


@pytest_mark.parametrize(
    ("state", "eligible", "reason"),
    [
        (LifecycleState.ACTIVE, True, LifecycleDecisionReason.ELIGIBLE),
        (LifecycleState.SUPERSEDED, False, LifecycleDecisionReason.SUPERSEDED),
        (LifecycleState.INVALIDATED, False, LifecycleDecisionReason.INVALIDATED),
        (LifecycleState.RETIRED, False, LifecycleDecisionReason.RETIRED),
    ],
)
def test_base_eligibility_is_complete_and_tier_independent(state, eligible, reason) -> None:
    decision = lifecycle_base_eligibility(state)

    assert type(decision) is dict
    assert "direct_answer_eligible" in decision
    assert "reason" in decision
    assert decision.get("direct_answer_eligible", False) is eligible
    assert decision.get("reason", LifecycleDecisionReason.ELIGIBLE) == reason


@pytest_mark.parametrize(
    ("operation", "target"),
    [
        (LifecycleOperation.SUPERSEDE, LifecycleState.SUPERSEDED),
        (LifecycleOperation.INVALIDATE, LifecycleState.INVALIDATED),
        (LifecycleOperation.RETIRE, LifecycleState.RETIRED),
    ],
)
def test_active_has_only_the_three_named_transitions(operation, target) -> None:
    decision = lifecycle_transition_decision(LifecycleState.ACTIVE, target, operation)

    assert type(decision) is dict
    assert decision.get("allowed", False) is True
    assert decision.get("reason", LifecycleDecisionReason.ELIGIBLE) == LifecycleDecisionReason.LEGAL_TRANSITION
    assert require_lifecycle_transition(LifecycleState.ACTIVE, target, operation) == decision


@pytest_mark.parametrize("state", tuple(TERMINAL_LIFECYCLE_STATES))
@pytest_mark.parametrize("operation", tuple(LifecycleOperation))
def test_non_active_lifecycle_states_are_terminal(state, operation) -> None:
    decision = lifecycle_transition_decision(state, LifecycleState.ACTIVE, operation)

    assert "allowed" in decision
    assert decision.get("allowed", False) is False
    assert decision.get("reason", LifecycleDecisionReason.ELIGIBLE) == LifecycleDecisionReason.TERMINAL_STATE


def test_superseded_can_only_be_reached_through_explicit_supersession() -> None:
    for operation in (LifecycleOperation.INVALIDATE, LifecycleOperation.RETIRE):
        decision = lifecycle_transition_decision(LifecycleState.ACTIVE, LifecycleState.SUPERSEDED, operation)
        assert "allowed" in decision
        assert decision.get("allowed", False) is False
        assert decision.get("reason", LifecycleDecisionReason.ELIGIBLE) == LifecycleDecisionReason.OPERATION_TARGET_MISMATCH


def test_same_state_retry_is_not_reinterpreted_as_a_transition() -> None:
    decision = lifecycle_transition_decision(
        LifecycleState.ACTIVE,
        LifecycleState.ACTIVE,
        LifecycleOperation.SUPERSEDE,
    )

    assert "allowed" in decision
    assert decision.get("allowed", False) is False
    assert decision.get("reason", LifecycleDecisionReason.ELIGIBLE) == LifecycleDecisionReason.SAME_STATE_NOT_A_TRANSITION


def test_illegal_transition_raises_stable_lifecycle_error() -> None:
    with pytest_raises(LifecycleError, match="operation_target_mismatch"):
        require_lifecycle_transition(
            LifecycleState.ACTIVE,
            LifecycleState.RETIRED,
            LifecycleOperation.INVALIDATE,
        )


def test_lifecycle_decisions_are_revalidated_against_policy() -> None:
    base = lifecycle_base_eligibility(LifecycleState.ACTIVE)
    transition = lifecycle_transition_decision(
        LifecycleState.ACTIVE,
        LifecycleState.RETIRED,
        LifecycleOperation.RETIRE,
    )

    assert validate_lifecycle_base_decision(base) == base
    assert validate_lifecycle_base_decision(base) is not base

    malformed_base = dict(base)
    malformed_base["direct_answer_eligible"] = False
    with pytest_raises(InvalidRequestError, match="do not match lifecycle policy"):
        validate_lifecycle_base_decision(malformed_base)

    malformed_transition = dict(transition)
    malformed_transition["allowed"] = False
    with pytest_raises(InvalidRequestError, match="do not match transition policy"):
        validate_lifecycle_transition_decision(malformed_transition)


@pytest_mark.parametrize("state", tuple(LifecycleState))
def test_capacity_eviction_never_changes_lifecycle(state) -> None:
    assert lifecycle_after_capacity_eviction(state) == state


@pytest_mark.parametrize(
    ("call", "message"),
    [
        (lambda: lifecycle_base_eligibility("ACTIVE"), "lifecycle must be a LifecycleState"),
        (
            lambda: lifecycle_transition_decision(
                LifecycleState.ACTIVE,
                LifecycleState.RETIRED,
                "RETIRE",
            ),
            "operation must be a LifecycleOperation",
        ),
    ],
)
def test_lifecycle_boundaries_reject_wrong_concrete_types(call, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        call()


def test_artifact_codec_is_deterministic_and_preserves_exact_unicode() -> None:
    response = "Café 👩🏽‍💻 — line one\nΔεύτερη γραμμή\r\n終わり"
    original = validate_cached_response_artifact(
        {
            **ARTIFACT_FIELDS,
            "response": response,
            "valid_from": "2026-08-12T16:00:00Z",
            "valid_from_available": True,
            "valid_until": "2027-08-12T16:00:00Z",
            "valid_until_available": True,
            "metadata": {"z": [1, True, "é"], "a": {"ratio": 0.5}},
        }
    )

    encoded = cached_response_artifact_to_dict(original)
    restored = cached_response_artifact_from_dict(encoded)

    assert restored == original
    assert cached_response_artifact_to_dict(restored) == encoded
    assert restored.get("response", "") == response
    assert none_paths(encoded) == []


def test_artifact_codec_uses_exact_required_fields() -> None:
    data = cached_response_artifact_to_dict(validate_cached_response_artifact(ARTIFACT_FIELDS))

    data["extra"] = "forbidden"
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        cached_response_artifact_from_dict(data)

    del data["extra"]
    del data["response"]
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        cached_response_artifact_from_dict(data)


def test_artifact_dictionary_revalidates_mutation_and_copies_nested_records() -> None:
    provenance = validate_artifact_provenance(
        {"source_label": "source", "caller_id": "caller", "accepted_at": "2026-08-12T16:00:00Z"}
    )
    statistics = validate_artifact_statistics({"hit_count": 1, "query_count": 2, "last_hit": "", "last_hit_available": False})
    artifact = validate_cached_response_artifact({**ARTIFACT_FIELDS, "provenance": provenance, "statistics": statistics})
    provenance["source_label"] = "late mutation"
    statistics["query_count"] = 99

    assert type(artifact) is dict
    assert artifact.get("provenance", {}).get("source_label", "") == "source"
    assert artifact.get("statistics", {}).get("query_count", 0) == 2

    malformed = dict(artifact)
    malformed["generation"] = 0
    with pytest_raises(InvalidRequestError, match="positive integer"):
        validate_cached_response_artifact(malformed)


def test_support_is_bounded_ordered_and_duplicate_free() -> None:
    artifact = validate_cached_response_artifact(
        {**ARTIFACT_FIELDS, "support_references": (ASSERTION_REFERENCE_B, ASSERTION_REFERENCE_A)}
    )
    assert artifact.get("support_references", ()) == (
        ASSERTION_REFERENCE_B,
        ASSERTION_REFERENCE_A,
    )
    assert cached_response_artifact_to_dict(artifact).get("support_references", []) == [
        ASSERTION_REFERENCE_B,
        ASSERTION_REFERENCE_A,
    ]
    with pytest_raises(InvalidRequestError, match="duplicate"):
        validate_cached_response_artifact({**ARTIFACT_FIELDS, "support_references": (ASSERTION_REFERENCE_A, ASSERTION_REFERENCE_A)})


def test_metadata_is_deeply_copied_and_json_concrete() -> None:
    source = {"nested": {"items": [1, "two", False]}}
    artifact = validate_cached_response_artifact({**ARTIFACT_FIELDS, "metadata": source})
    source.get("nested", {}).get("items", []).append("late")

    assert cached_response_artifact_to_dict(artifact).get("metadata", {}) == {"nested": {"items": [1, "two", False]}}
    copied = validate_cached_response_artifact(artifact)
    # Both artifacts are validated, so their metadata is present; the presence checks keep that explicit.
    assert "metadata" in artifact
    assert "metadata" in copied
    artifact.get("metadata", {})["new"] = "value"
    assert "new" not in copied.get("metadata", {})


@pytest_mark.parametrize(
    ("metadata", "message"),
    [
        (json_loads('{"missing": null}'), "unsupported JSON value"),
        ({"nan": float("nan")}, "non-finite number"),
        ({1: "value"}, "metadata key must be a string"),
        ({"valid": "value", 1: "invalid"}, "metadata key must be a string"),
    ],
)
def test_metadata_rejects_non_json_or_non_concrete_values(metadata, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        validate_cached_response_artifact({**ARTIFACT_FIELDS, "metadata": metadata})


def test_metadata_depth_is_bounded() -> None:
    value = "leaf"
    for position in range(MAX_METADATA_DEPTH + 1):
        value = {f"level-{position}": value}
    with pytest_raises(InvalidRequestError, match="depth limit"):
        validate_cached_response_artifact({**ARTIFACT_FIELDS, "metadata": value})


@pytest_mark.parametrize(
    ("overrides", "message"),
    [
        ({"valid_from": "2026-08-12T16:00:00Z", "valid_from_available": False}, "must be empty"),
        ({"valid_until": "", "valid_until_available": True}, "must not be empty"),
        ({"valid_from": "2026-08-12T12:00:00-04:00", "valid_from_available": True}, "ending in Z"),
        ({"valid_from": "2026-08-12T16:00:00.000Z", "valid_from_available": True}, "canonical RFC 3339"),
    ],
)
def test_temporal_presence_fields_are_concrete(overrides, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        validate_cached_response_artifact({**ARTIFACT_FIELDS, **overrides})


def test_artifact_scope_must_match_authoritative_identity_scope() -> None:
    with pytest_raises(InvalidRequestError, match="scope must match"):
        validate_cached_response_artifact(
            {
                **ARTIFACT_FIELDS,
                "scope": scope_key(namespace="tenant-b"),
                "query_identity": extract_standalone_identity("Who acquired GitHub?"),
            }
        )


def test_supersession_link_is_consistent_with_lifecycle() -> None:
    with pytest_raises(InvalidRequestError, match="ACTIVE artifact"):
        validate_cached_response_artifact({**ARTIFACT_FIELDS, "superseded_by": "stmt-new"})
    with pytest_raises(InvalidRequestError, match="must name superseded_by"):
        validate_cached_response_artifact({**ARTIFACT_FIELDS, "lifecycle": LifecycleState.SUPERSEDED})
    with pytest_raises(InvalidRequestError, match="must not reference itself"):
        validate_cached_response_artifact(
            {**ARTIFACT_FIELDS, "lifecycle": LifecycleState.SUPERSEDED, "superseded_by": "stmt-response-1"}
        )

    superseded = validate_cached_response_artifact(
        {**ARTIFACT_FIELDS, "lifecycle": LifecycleState.SUPERSEDED, "superseded_by": "stmt-response-2"}
    )
    assert superseded.get("superseded_by", "") == "stmt-response-2"


def test_provenance_and_statistics_codecs_are_exact_and_deterministic() -> None:
    provenance = validate_artifact_provenance(
        {"source_label": "source", "caller_id": "caller", "accepted_at": "2026-08-12T16:00:00Z"}
    )
    statistics = validate_artifact_statistics(
        {"hit_count": 3, "query_count": 5, "last_hit": "2026-08-12T17:00:00Z", "last_hit_available": True}
    )

    assert type(provenance) is dict
    assert type(statistics) is dict
    assert artifact_provenance_from_dict(artifact_provenance_to_dict(provenance)) == provenance
    assert artifact_statistics_from_dict(artifact_statistics_to_dict(statistics)) == statistics

    bad_statistics = artifact_statistics_to_dict(statistics)
    bad_statistics["last_hit_available"] = False
    with pytest_raises(InvalidRequestError, match="must be empty"):
        artifact_statistics_from_dict(bad_statistics)


@pytest_mark.parametrize(
    ("overrides", "message"),
    [
        ({"statement_id": ""}, "must not be empty"),
        ({"generation": True}, "positive integer"),
        ({"response": "bad\x00response"}, "control character"),
        ({"tier": "STATIC"}, "must be a Tier"),
        ({"lifecycle": "ACTIVE"}, "must be a LifecycleState"),
        ({"support_references": []}, "must be a tuple"),
        ({"metadata": []}, "must be an object"),
    ],
)
def test_artifact_validation_rejects_wrong_concrete_types(overrides, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        validate_cached_response_artifact({**ARTIFACT_FIELDS, **overrides})
