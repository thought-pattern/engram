"""Section 3 accepted-response artifact and lifecycle contracts."""

from inspect import signature
from json import loads as json_loads

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    MAX_METADATA_DEPTH,
    TERMINAL_LIFECYCLE_STATES,
    HistoricalKeyReuseReason,
    LifecycleDecisionReason,
    LifecycleOperation,
    LifecycleState,
    artifact_provenance,
    artifact_provenance_from_dict,
    artifact_provenance_to_dict,
    artifact_statistics,
    artifact_statistics_from_dict,
    artifact_statistics_to_dict,
    cached_response_artifact,
    cached_response_artifact_from_dict,
    cached_response_artifact_from_json,
    cached_response_artifact_to_dict,
    cached_response_artifact_to_json,
    historical_key_reuse_decision,
    historical_key_reuse_decision_to_dict,
    lifecycle_after_capacity_eviction,
    lifecycle_base_decision_to_dict,
    lifecycle_base_eligibility,
    lifecycle_transition_decision,
    lifecycle_transition_decision_to_dict,
    require_lifecycle_transition,
    validate_cached_response_artifact,
    validate_historical_key_reuse_decision,
    validate_lifecycle_base_decision,
    validate_lifecycle_transition_decision,
)
from engram.constants import Tier
from engram.errors import InvalidRequestError, LifecycleError
from engram.identity import build_retrieval_representation, build_standalone_identity, scope_key

from .support_fixtures import ASSERTION_REFERENCE_A, ASSERTION_REFERENCE_B


def accepted_artifact(**overrides) -> dict:
    scope = overrides.pop("scope", scope_key(namespace="tenant-a", context_fingerprint="account:pro"))
    request = overrides.pop("request", "Who acquired GitHub?")
    values = {
        "statement_id": "stmt-response-1",
        "generation": 1,
        "response": "Microsoft acquired GitHub in 2018.",
        "query_identity": build_standalone_identity(request, scope),
        "retrieval": build_retrieval_representation(request, ("GitHub acquirer",)),
        "tier": Tier.STATIC,
        "lifecycle": LifecycleState.ACTIVE,
        "scope": scope,
        "support_references": (ASSERTION_REFERENCE_B, ASSERTION_REFERENCE_A),
        "valid_from": "",
        "valid_from_available": False,
        "valid_until": "",
        "valid_until_available": False,
        "superseded_by": "",
        "provenance": artifact_provenance(
            source_label="tapestry:released",
            caller_id="regulator-a",
            accepted_at="2026-08-12T16:00:00Z",
        ),
        "statistics": artifact_statistics(),
        "metadata": {},
    }
    values.update(overrides)
    result = cached_response_artifact(**values)
    return result


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
    assert decision["direct_answer_eligible"] is eligible
    assert decision["reason"] == reason
    assert lifecycle_base_decision_to_dict(decision) == {
        "lifecycle": state.value,
        "direct_answer_eligible": eligible,
        "reason": reason.value,
    }
    assert tuple(signature(lifecycle_base_eligibility).parameters) == ("lifecycle",)


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
    assert decision["allowed"] is True
    assert decision["reason"] == LifecycleDecisionReason.LEGAL_TRANSITION
    assert require_lifecycle_transition(LifecycleState.ACTIVE, target, operation) == decision


@pytest_mark.parametrize("state", tuple(TERMINAL_LIFECYCLE_STATES))
@pytest_mark.parametrize("operation", tuple(LifecycleOperation))
def test_non_active_lifecycle_states_are_terminal(state, operation) -> None:
    decision = lifecycle_transition_decision(state, LifecycleState.ACTIVE, operation)

    assert decision["allowed"] is False
    assert decision["reason"] == LifecycleDecisionReason.TERMINAL_STATE


def test_superseded_can_only_be_reached_through_explicit_supersession() -> None:
    for operation in (LifecycleOperation.INVALIDATE, LifecycleOperation.RETIRE):
        decision = lifecycle_transition_decision(LifecycleState.ACTIVE, LifecycleState.SUPERSEDED, operation)
        assert decision["allowed"] is False
        assert decision["reason"] == LifecycleDecisionReason.OPERATION_TARGET_MISMATCH


def test_same_state_retry_is_not_reinterpreted_as_a_transition() -> None:
    decision = lifecycle_transition_decision(
        LifecycleState.ACTIVE,
        LifecycleState.ACTIVE,
        LifecycleOperation.SUPERSEDE,
    )

    assert decision["allowed"] is False
    assert decision["reason"] == LifecycleDecisionReason.SAME_STATE_NOT_A_TRANSITION


def test_illegal_transition_raises_stable_lifecycle_error() -> None:
    with pytest_raises(LifecycleError, match="operation_target_mismatch"):
        require_lifecycle_transition(
            LifecycleState.ACTIVE,
            LifecycleState.RETIRED,
            LifecycleOperation.INVALIDATE,
        )


def test_historical_key_reuse_requires_explicit_expected_owner_and_generation() -> None:
    assert (
        historical_key_reuse_decision(
            explicit_replacement=False,
            expected_statement_id="stmt-old",
            expected_generation=4,
        )["reason"]
        == HistoricalKeyReuseReason.BASE_COMMIT_FORBIDDEN
    )
    assert (
        historical_key_reuse_decision(
            explicit_replacement=True,
            expected_statement_id="",
            expected_generation=4,
        )["reason"]
        == HistoricalKeyReuseReason.EXPECTED_STATEMENT_ID_REQUIRED
    )
    assert (
        historical_key_reuse_decision(
            explicit_replacement=True,
            expected_statement_id="stmt-old",
            expected_generation=0,
        )["reason"]
        == HistoricalKeyReuseReason.EXPECTED_GENERATION_REQUIRED
    )
    allowed = historical_key_reuse_decision(
        explicit_replacement=True,
        expected_statement_id="stmt-old",
        expected_generation=4,
    )
    assert type(allowed) is dict
    assert allowed["allowed"] is True
    assert allowed["reason"] == HistoricalKeyReuseReason.ALLOWED_EXPLICIT_REPLACEMENT


def test_lifecycle_decision_dictionaries_have_exact_codecs_and_revalidation() -> None:
    base = lifecycle_base_eligibility(LifecycleState.ACTIVE)
    transition = lifecycle_transition_decision(
        LifecycleState.ACTIVE,
        LifecycleState.RETIRED,
        LifecycleOperation.RETIRE,
    )
    historical = historical_key_reuse_decision(
        explicit_replacement=True,
        expected_statement_id="stmt-old",
        expected_generation=4,
    )

    assert validate_lifecycle_base_decision(base) == base
    assert validate_lifecycle_base_decision(base) is not base
    assert lifecycle_transition_decision_to_dict(transition) == {
        "current": "ACTIVE",
        "target": "RETIRED",
        "operation": "RETIRE",
        "allowed": True,
        "reason": "legal_transition",
    }
    assert historical_key_reuse_decision_to_dict(historical) == {
        "allowed": True,
        "reason": "allowed_explicit_replacement",
    }

    malformed_base = dict(base)
    malformed_base["direct_answer_eligible"] = False
    with pytest_raises(InvalidRequestError, match="do not match lifecycle policy"):
        validate_lifecycle_base_decision(malformed_base)

    malformed_transition = dict(transition)
    malformed_transition["allowed"] = False
    with pytest_raises(InvalidRequestError, match="do not match transition policy"):
        validate_lifecycle_transition_decision(malformed_transition)

    malformed_historical = dict(historical)
    malformed_historical["reason"] = HistoricalKeyReuseReason.BASE_COMMIT_FORBIDDEN
    with pytest_raises(InvalidRequestError, match="allowed does not match reason"):
        validate_historical_key_reuse_decision(malformed_historical)


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
        (
            lambda: historical_key_reuse_decision(
                explicit_replacement=1,
                expected_statement_id="stmt-old",
                expected_generation=1,
            ),
            "explicit_replacement must be a boolean",
        ),
    ],
)
def test_lifecycle_boundaries_reject_wrong_concrete_types(call, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        call()


def test_artifact_codec_is_deterministic_and_preserves_exact_unicode() -> None:
    response = "Café 👩🏽‍💻 — line one\nΔεύτερη γραμμή\r\n終わり"
    original = accepted_artifact(
        response=response,
        valid_from="2026-08-12T16:00:00Z",
        valid_from_available=True,
        valid_until="2027-08-12T16:00:00Z",
        valid_until_available=True,
        metadata={"z": [1, True, "é"], "a": {"ratio": 0.5}},
    )

    encoded = cached_response_artifact_to_json(original)
    restored = cached_response_artifact_from_json(encoded)

    assert restored == original
    assert type(restored) is dict
    assert cached_response_artifact_to_json(restored) == encoded
    assert restored["response"] == response
    assert restored["response"].encode("utf-8") == response.encode("utf-8")
    assert "\\u00e9" not in encoded
    assert none_paths(cached_response_artifact_to_dict(restored)) == []


def test_artifact_codec_uses_exact_required_fields() -> None:
    data = cached_response_artifact_to_dict(accepted_artifact())
    assert data["schema_version"] == ARTIFACT_SCHEMA_VERSION

    data["extra"] = "forbidden"
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        cached_response_artifact_from_dict(data)

    del data["extra"]
    del data["response"]
    with pytest_raises(InvalidRequestError, match="invalid fields"):
        cached_response_artifact_from_dict(data)


def test_artifact_dictionary_revalidates_mutation_and_copies_nested_records() -> None:
    provenance = artifact_provenance("source", "caller", "2026-08-12T16:00:00Z")
    statistics = artifact_statistics(1, 2, "", False)
    artifact = accepted_artifact(provenance=provenance, statistics=statistics)
    provenance["source_label"] = "late mutation"
    statistics["query_count"] = 99

    assert type(artifact) is dict
    assert artifact["provenance"]["source_label"] == "source"
    assert artifact["statistics"]["query_count"] == 2

    malformed = dict(artifact)
    malformed["generation"] = 0
    with pytest_raises(InvalidRequestError, match="positive integer"):
        validate_cached_response_artifact(malformed)


def test_support_is_bounded_ordered_and_duplicate_free() -> None:
    artifact = accepted_artifact(support_references=(ASSERTION_REFERENCE_B, ASSERTION_REFERENCE_A))
    assert artifact["support_references"] == (
        ASSERTION_REFERENCE_B,
        ASSERTION_REFERENCE_A,
    )
    assert cached_response_artifact_to_dict(artifact)["support_references"] == [
        ASSERTION_REFERENCE_B,
        ASSERTION_REFERENCE_A,
    ]
    with pytest_raises(InvalidRequestError, match="duplicate"):
        accepted_artifact(support_references=(ASSERTION_REFERENCE_A, ASSERTION_REFERENCE_A))


def test_metadata_is_deeply_copied_and_json_concrete() -> None:
    source = {"nested": {"items": [1, "two", False]}}
    artifact = accepted_artifact(metadata=source)
    source.get("nested", {})["items"].append("late")

    assert cached_response_artifact_to_dict(artifact)["metadata"] == {"nested": {"items": [1, "two", False]}}
    copied = validate_cached_response_artifact(artifact)
    artifact["metadata"]["new"] = "value"
    assert "new" not in copied["metadata"]


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
        accepted_artifact(metadata=metadata)


def test_metadata_depth_is_bounded() -> None:
    value = "leaf"
    for position in range(MAX_METADATA_DEPTH + 1):
        value = {f"level-{position}": value}
    with pytest_raises(InvalidRequestError, match="depth limit"):
        accepted_artifact(metadata=value)


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
        accepted_artifact(**overrides)


def test_artifact_scope_must_match_authoritative_identity_scope() -> None:
    with pytest_raises(InvalidRequestError, match="scope must match"):
        accepted_artifact(
            scope=scope_key(namespace="tenant-b"),
            query_identity=build_standalone_identity("Who acquired GitHub?"),
        )


def test_supersession_link_is_consistent_with_lifecycle() -> None:
    with pytest_raises(InvalidRequestError, match="ACTIVE artifact"):
        accepted_artifact(superseded_by="stmt-new")
    with pytest_raises(InvalidRequestError, match="must name superseded_by"):
        accepted_artifact(lifecycle=LifecycleState.SUPERSEDED)
    with pytest_raises(InvalidRequestError, match="must not reference itself"):
        accepted_artifact(lifecycle=LifecycleState.SUPERSEDED, superseded_by="stmt-response-1")

    superseded = accepted_artifact(lifecycle=LifecycleState.SUPERSEDED, superseded_by="stmt-response-2")
    assert superseded["superseded_by"] == "stmt-response-2"


def test_provenance_and_statistics_codecs_are_exact_and_deterministic() -> None:
    provenance = artifact_provenance("source", "caller", "2026-08-12T16:00:00Z")
    statistics = artifact_statistics(3, 5, "2026-08-12T17:00:00Z", True)

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
        ({"response": "bad\x00response"}, "unsupported control"),
        ({"tier": "STATIC"}, "must be a Tier"),
        ({"lifecycle": "ACTIVE"}, "must be a LifecycleState"),
        ({"support_references": []}, "must be a tuple"),
        ({"metadata": []}, "must be an object"),
    ],
)
def test_artifact_constructor_rejects_wrong_concrete_types(overrides, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        accepted_artifact(**overrides)


def test_artifact_json_loader_rejects_malformed_and_non_object_values() -> None:
    with pytest_raises(InvalidRequestError, match="must be a string"):
        cached_response_artifact_from_json({})
    with pytest_raises(InvalidRequestError, match="malformed"):
        cached_response_artifact_from_json("{")
    with pytest_raises(InvalidRequestError, match="must contain an object"):
        cached_response_artifact_from_json("[]")
