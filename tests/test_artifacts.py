"""Section 3 accepted-response artifact and lifecycle contracts."""

from inspect import signature
from typing import Any, cast

import pytest

from engram.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    LEGAL_LIFECYCLE_TRANSITIONS,
    MAX_METADATA_DEPTH,
    TERMINAL_LIFECYCLE_STATES,
    ArtifactProvenance,
    ArtifactStatistics,
    CachedResponseArtifact,
    HistoricalKeyReuseReason,
    LifecycleDecisionReason,
    LifecycleOperation,
    LifecycleState,
    historical_key_reuse_decision,
    lifecycle_after_capacity_eviction,
    lifecycle_base_eligibility,
    lifecycle_transition_decision,
    require_lifecycle_transition,
)
from engram.constants import Tier
from engram.errors import InvalidRequestError, LifecycleError
from engram.identity import ScopeKey, build_retrieval_representation, build_standalone_identity


def accepted_artifact(**overrides) -> CachedResponseArtifact:
    scope = overrides.pop("scope", ScopeKey(namespace="tenant-a", context_fingerprint="account:pro"))
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
        "support_claim_ids": ("claim-2", "claim-1"),
        "valid_from": "",
        "valid_from_available": False,
        "valid_until": "",
        "valid_until_available": False,
        "knowledge_epoch": 0,
        "knowledge_epoch_available": False,
        "superseded_by": "",
        "provenance": ArtifactProvenance(
            source_label="tapestry:released",
            caller_id="regulator-a",
            accepted_at="2026-08-12T16:00:00Z",
        ),
        "statistics": ArtifactStatistics(),
        "metadata": {},
    }
    values.update(overrides)
    return CachedResponseArtifact(**values)


def none_paths(value, path="root") -> list[str]:
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [nested for key, item in value.items() for nested in none_paths(item, f"{path}.{key}")]
    if isinstance(value, (list, tuple, set)):
        return [nested for index, item in enumerate(value) for nested in none_paths(item, f"{path}[{index}]")]
    return []


def test_lifecycle_vocabulary_and_terminal_states_are_closed() -> None:
    assert tuple(LifecycleState) == (
        LifecycleState.ACTIVE,
        LifecycleState.SUPERSEDED,
        LifecycleState.INVALIDATED,
        LifecycleState.RETIRED,
    )
    assert {
        LifecycleState.SUPERSEDED,
        LifecycleState.INVALIDATED,
        LifecycleState.RETIRED,
    } == TERMINAL_LIFECYCLE_STATES


@pytest.mark.parametrize(
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

    assert decision.direct_answer_eligible is eligible
    assert decision.reason == reason
    assert decision.to_dict() == {
        "lifecycle": state.value,
        "direct_answer_eligible": eligible,
        "reason": reason.value,
    }
    assert tuple(signature(lifecycle_base_eligibility).parameters) == ("lifecycle",)


@pytest.mark.parametrize(
    ("operation", "target"),
    [
        (LifecycleOperation.SUPERSEDE, LifecycleState.SUPERSEDED),
        (LifecycleOperation.INVALIDATE, LifecycleState.INVALIDATED),
        (LifecycleOperation.RETIRE, LifecycleState.RETIRED),
    ],
)
def test_active_has_only_the_three_named_transitions(operation, target) -> None:
    decision = lifecycle_transition_decision(LifecycleState.ACTIVE, target, operation)

    assert decision.allowed is True
    assert decision.reason == LifecycleDecisionReason.LEGAL_TRANSITION
    assert require_lifecycle_transition(LifecycleState.ACTIVE, target, operation) == decision
    assert LEGAL_LIFECYCLE_TRANSITIONS[LifecycleState.ACTIVE][operation] == target


@pytest.mark.parametrize("state", tuple(TERMINAL_LIFECYCLE_STATES))
@pytest.mark.parametrize("operation", tuple(LifecycleOperation))
def test_non_active_lifecycle_states_are_terminal(state, operation) -> None:
    decision = lifecycle_transition_decision(state, LifecycleState.ACTIVE, operation)

    assert decision.allowed is False
    assert decision.reason == LifecycleDecisionReason.TERMINAL_STATE
    assert LEGAL_LIFECYCLE_TRANSITIONS[state] == {}


def test_superseded_can_only_be_reached_through_explicit_supersession() -> None:
    for operation in (LifecycleOperation.INVALIDATE, LifecycleOperation.RETIRE):
        decision = lifecycle_transition_decision(LifecycleState.ACTIVE, LifecycleState.SUPERSEDED, operation)
        assert decision.allowed is False
        assert decision.reason == LifecycleDecisionReason.OPERATION_TARGET_MISMATCH


def test_same_state_retry_is_not_reinterpreted_as_a_transition() -> None:
    decision = lifecycle_transition_decision(
        LifecycleState.ACTIVE,
        LifecycleState.ACTIVE,
        LifecycleOperation.SUPERSEDE,
    )

    assert decision.allowed is False
    assert decision.reason == LifecycleDecisionReason.SAME_STATE_NOT_A_TRANSITION


def test_illegal_transition_raises_stable_lifecycle_error() -> None:
    with pytest.raises(LifecycleError, match="operation_target_mismatch"):
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
        ).reason
        == HistoricalKeyReuseReason.BASE_COMMIT_FORBIDDEN
    )
    assert (
        historical_key_reuse_decision(
            explicit_replacement=True,
            expected_statement_id="",
            expected_generation=4,
        ).reason
        == HistoricalKeyReuseReason.EXPECTED_STATEMENT_ID_REQUIRED
    )
    assert (
        historical_key_reuse_decision(
            explicit_replacement=True,
            expected_statement_id="stmt-old",
            expected_generation=0,
        ).reason
        == HistoricalKeyReuseReason.EXPECTED_GENERATION_REQUIRED
    )
    allowed = historical_key_reuse_decision(
        explicit_replacement=True,
        expected_statement_id="stmt-old",
        expected_generation=4,
    )
    assert allowed.allowed is True
    assert allowed.reason == HistoricalKeyReuseReason.ALLOWED_EXPLICIT_REPLACEMENT


@pytest.mark.parametrize("state", tuple(LifecycleState))
def test_capacity_eviction_never_changes_lifecycle(state) -> None:
    assert lifecycle_after_capacity_eviction(state) == state


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: lifecycle_base_eligibility(cast(Any, "ACTIVE")), "lifecycle must be a LifecycleState"),
        (
            lambda: lifecycle_transition_decision(
                LifecycleState.ACTIVE,
                LifecycleState.RETIRED,
                cast(Any, "RETIRE"),
            ),
            "operation must be a LifecycleOperation",
        ),
        (
            lambda: historical_key_reuse_decision(
                explicit_replacement=cast(Any, 1),
                expected_statement_id="stmt-old",
                expected_generation=1,
            ),
            "explicit_replacement must be a boolean",
        ),
    ],
)
def test_lifecycle_boundaries_reject_wrong_concrete_types(call, message) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        call()


def test_artifact_codec_is_deterministic_and_preserves_exact_unicode() -> None:
    response = "Café 👩🏽‍💻 — line one\nΔεύτερη γραμμή\r\n終わり"
    original = accepted_artifact(
        response=response,
        valid_from="2026-08-12T16:00:00Z",
        valid_from_available=True,
        valid_until="2027-08-12T16:00:00Z",
        valid_until_available=True,
        knowledge_epoch=42,
        knowledge_epoch_available=True,
        metadata={"z": [1, True, "é"], "a": {"ratio": 0.5}},
    )

    encoded = original.to_json()
    restored = CachedResponseArtifact.from_json(encoded)

    assert restored == original
    assert restored.to_json() == encoded
    assert restored.response == response
    assert restored.response.encode("utf-8") == response.encode("utf-8")
    assert "\\u00e9" not in encoded
    assert none_paths(restored.to_dict()) == []


def test_artifact_codec_uses_exact_required_fields() -> None:
    data = accepted_artifact().to_dict()
    assert data["schema_version"] == ARTIFACT_SCHEMA_VERSION

    data["extra"] = "forbidden"
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        CachedResponseArtifact.from_dict(data)

    del data["extra"]
    del data["response"]
    with pytest.raises(InvalidRequestError, match="invalid fields"):
        CachedResponseArtifact.from_dict(data)


def test_support_is_bounded_deduplicated_and_sorted() -> None:
    artifact = accepted_artifact(support_claim_ids=("claim-z", "claim-a", "claim-z"))
    assert artifact.support_claim_ids == ("claim-a", "claim-z")
    assert artifact.to_dict()["support_claim_ids"] == ["claim-a", "claim-z"]


def test_metadata_is_deeply_immutable_and_json_concrete() -> None:
    source = {"nested": {"items": [1, "two", False]}}
    artifact = accepted_artifact(metadata=source)
    source["nested"]["items"].append("late")

    assert artifact.to_dict()["metadata"] == {"nested": {"items": [1, "two", False]}}
    with pytest.raises(TypeError):
        cast(dict[str, object], artifact.metadata)["new"] = "value"


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"missing": None}, "unsupported JSON value"),
        ({"nan": float("nan")}, "non-finite number"),
        ({1: "value"}, "metadata key must be a string"),
        ({"valid": "value", 1: "invalid"}, "metadata key must be a string"),
    ],
)
def test_metadata_rejects_non_json_or_non_concrete_values(metadata, message) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        accepted_artifact(metadata=metadata)


def test_metadata_depth_is_bounded() -> None:
    value = "leaf"
    for position in range(MAX_METADATA_DEPTH + 1):
        value = {f"level-{position}": value}
    with pytest.raises(InvalidRequestError, match="depth limit"):
        accepted_artifact(metadata=value)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"valid_from": "2026-08-12T16:00:00Z", "valid_from_available": False}, "must be empty"),
        ({"valid_until": "", "valid_until_available": True}, "must not be empty"),
        ({"valid_from": "2026-08-12T12:00:00-04:00", "valid_from_available": True}, "ending in Z"),
        ({"valid_from": "2026-08-12T16:00:00.000Z", "valid_from_available": True}, "canonical RFC 3339"),
        ({"knowledge_epoch": 5, "knowledge_epoch_available": False}, "must be 0 when unavailable"),
        ({"knowledge_epoch": -1, "knowledge_epoch_available": True}, "nonnegative integer"),
    ],
)
def test_temporal_presence_and_epoch_fields_are_concrete(overrides, message) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        accepted_artifact(**overrides)


def test_artifact_scope_must_match_authoritative_identity_scope() -> None:
    with pytest.raises(InvalidRequestError, match="scope must match"):
        accepted_artifact(scope=ScopeKey(namespace="tenant-b"), query_identity=build_standalone_identity("Who acquired GitHub?"))


def test_supersession_link_is_consistent_with_lifecycle() -> None:
    with pytest.raises(InvalidRequestError, match="ACTIVE artifact"):
        accepted_artifact(superseded_by="stmt-new")
    with pytest.raises(InvalidRequestError, match="must name superseded_by"):
        accepted_artifact(lifecycle=LifecycleState.SUPERSEDED)
    with pytest.raises(InvalidRequestError, match="must not reference itself"):
        accepted_artifact(lifecycle=LifecycleState.SUPERSEDED, superseded_by="stmt-response-1")

    superseded = accepted_artifact(lifecycle=LifecycleState.SUPERSEDED, superseded_by="stmt-response-2")
    assert superseded.superseded_by == "stmt-response-2"


def test_provenance_and_statistics_codecs_are_exact_and_deterministic() -> None:
    provenance = ArtifactProvenance("source", "caller", "2026-08-12T16:00:00Z")
    statistics = ArtifactStatistics(3, 5, "2026-08-12T17:00:00Z", True)

    assert ArtifactProvenance.from_dict(provenance.to_dict()) == provenance
    assert ArtifactStatistics.from_dict(statistics.to_dict()) == statistics

    bad_statistics = statistics.to_dict()
    bad_statistics["last_hit_available"] = False
    with pytest.raises(InvalidRequestError, match="must be empty"):
        ArtifactStatistics.from_dict(bad_statistics)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"statement_id": ""}, "must not be empty"),
        ({"generation": True}, "positive integer"),
        ({"response": "bad\x00response"}, "unsupported control"),
        ({"tier": "STATIC"}, "must be a Tier"),
        ({"lifecycle": "ACTIVE"}, "must be a LifecycleState"),
        ({"support_claim_ids": []}, "must be a tuple"),
        ({"metadata": []}, "must be an object"),
    ],
)
def test_artifact_constructor_rejects_wrong_concrete_types(overrides, message) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        accepted_artifact(**overrides)


def test_artifact_json_loader_rejects_malformed_and_non_object_values() -> None:
    with pytest.raises(InvalidRequestError, match="must be a string"):
        CachedResponseArtifact.from_json(cast(Any, {}))
    with pytest.raises(InvalidRequestError, match="malformed"):
        CachedResponseArtifact.from_json("{")
    with pytest.raises(InvalidRequestError, match="must contain an object"):
        CachedResponseArtifact.from_json("[]")
