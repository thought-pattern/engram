"""Process-memory accepted-response eligibility tests."""

from datetime import UTC, datetime
from json import loads as json_loads

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.constants import EligibilityExclusionReason, ExactLookupOutcome, LifecycleState
from engram.eligibility import (
    ContextualExactLookup,
    EligibilityContextCapture,
    eligibility_context,
    eligibility_context_from_json,
    eligibility_context_to_json,
    eligibility_decision_context_signature,
    eligibility_decision_to_dict,
    evaluate_artifact_eligibility,
    validate_eligibility_context,
    validate_eligibility_decision,
)
from engram.errors import InvalidRequestError
from engram.identity import retrieval_representation_bindings, scope_key
from tests.support_fixtures import accepted_artifact

NOW = "2026-08-12T16:00:00Z"


def context(**changes) -> dict:
    values = {
        "evaluation_time": NOW,
        "evaluation_time_available": True,
        "namespace": "tenant-a",
        "artifact_repository_available": True,
    }
    values.update(changes)
    result = eligibility_context(**values)
    return result


def test_context_is_concrete_and_round_trips_at_the_external_json_boundary() -> None:
    original = context()
    encoded = eligibility_context_to_json(original)

    assert eligibility_context_from_json(encoded) == original
    assert json_loads(encoded) == original
    assert set(original) == {
        "schema_version",
        "evaluation_time",
        "evaluation_time_available",
        "namespace",
        "artifact_repository_available",
    }


def test_factory_captures_one_utc_process_snapshot() -> None:
    calls = []

    def clock() -> datetime:
        calls.append(True)
        result = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
        return result

    captured = EligibilityContextCapture(clock).capture_standalone(
        scope_key(namespace="tenant-a"),
        True,
    )

    assert captured == context()
    assert len(calls) == 1


@pytest_mark.parametrize(
    ("changes", "reason"),
    [
        ({"artifact_repository_available": False}, EligibilityExclusionReason.ARTIFACT_REPOSITORY_UNAVAILABLE),
        (
            {"evaluation_time": "", "evaluation_time_available": False},
            EligibilityExclusionReason.EVALUATION_TIME_UNAVAILABLE,
        ),
        ({"namespace": "tenant-b"}, EligibilityExclusionReason.SCOPE_NAMESPACE_MISMATCH),
    ],
)
def test_request_context_exclusions_are_explicit(changes, reason) -> None:
    decision = evaluate_artifact_eligibility(accepted_artifact(), context(**changes))

    assert decision.get("direct_answer_eligible") is False
    assert decision.get("exclusion_reason") == reason


@pytest_mark.parametrize(
    ("lifecycle", "reason"),
    [
        (LifecycleState.SUPERSEDED, EligibilityExclusionReason.LIFECYCLE_SUPERSEDED),
        (LifecycleState.INVALIDATED, EligibilityExclusionReason.LIFECYCLE_INVALIDATED),
        (LifecycleState.RETIRED, EligibilityExclusionReason.LIFECYCLE_RETIRED),
    ],
)
def test_terminal_lifecycle_states_are_never_direct_answers(lifecycle, reason) -> None:
    replacement = "stmt-next" if lifecycle == LifecycleState.SUPERSEDED else ""
    decision = evaluate_artifact_eligibility(
        accepted_artifact(lifecycle=lifecycle, superseded_by=replacement),
        context(),
    )

    assert decision.get("direct_answer_eligible") is False
    assert decision.get("exclusion_reason") == reason


@pytest_mark.parametrize(
    ("artifact_changes", "evaluation_time", "reason"),
    [
        (
            {"valid_from": "2026-08-12T17:00:00Z", "valid_from_available": True},
            NOW,
            EligibilityExclusionReason.NOT_YET_VALID,
        ),
        (
            {"valid_until": NOW, "valid_until_available": True},
            NOW,
            EligibilityExclusionReason.EXPIRED,
        ),
        (
            {
                "valid_from": "2026-08-13T00:00:00Z",
                "valid_from_available": True,
                "valid_until": "2026-08-12T00:00:00Z",
                "valid_until_available": True,
            },
            NOW,
            EligibilityExclusionReason.VALIDITY_INTERVAL_INVALID,
        ),
    ],
)
def test_validity_interval_is_half_open_and_evaluated_at_request_time(
    artifact_changes,
    evaluation_time,
    reason,
) -> None:
    decision = evaluate_artifact_eligibility(
        accepted_artifact(**artifact_changes),
        context(evaluation_time=evaluation_time),
    )

    assert decision.get("direct_answer_eligible") is False
    assert decision.get("exclusion_reason") == reason


def test_active_current_artifact_is_eligible_and_has_stable_external_form() -> None:
    decision = evaluate_artifact_eligibility(accepted_artifact(), context())
    external = eligibility_decision_to_dict(decision)

    assert decision.get("direct_answer_eligible") is True
    assert external.get("exclusion_reason") == "eligible"
    assert eligibility_decision_context_signature(decision) == ("20:2026-08-12T16:00:00Z|4:True|8:tenant-a|4:True")
    assert validate_eligibility_decision(decision) == decision


def test_contextual_exact_lookup_scans_current_artifacts_without_secondary_state() -> None:
    artifact = accepted_artifact()
    current_context = context()
    key = retrieval_representation_bindings(
        artifact.get("retrieval", {}),
        artifact.get("scope", {}),
    )[
        0
    ].get("key", {})
    lookup = ContextualExactLookup(
        {artifact.get("statement_id", ""): artifact},
    )

    eligible = lookup.exact_lookup(key, current_context)
    unavailable = lookup.exact_lookup(
        key,
        context(artifact_repository_available=False),
    )

    assert eligible.get("lookup", {}).get("outcome") == ExactLookupOutcome.FOUND
    assert eligible.get("lookup", {}).get("statement_id") == artifact.get("statement_id", "")
    assert unavailable.get("lookup", {}).get("outcome") == ExactLookupOutcome.MISS
    assert unavailable.get("lookup", {}).get("statement_id") == ""
    assert set(unavailable) == {"lookup", "decisions", "context_signature"}


@pytest_mark.parametrize(
    ("value", "message"),
    [
        ({}, "invalid fields"),
        (
            {
                "schema_version": 1,
                "evaluation_time": "2026-08-12T12:00:00-04:00",
                "evaluation_time_available": True,
                "namespace": "tenant-a",
                "artifact_repository_available": True,
            },
            "ending in Z",
        ),
    ],
)
def test_context_validation_rejects_malformed_external_values(value, message) -> None:
    with pytest_raises(InvalidRequestError, match=message):
        validate_eligibility_context(value)


def test_eligibility_rejects_non_contract_inputs() -> None:
    with pytest_raises(InvalidRequestError, match="CachedResponseArtifact"):
        evaluate_artifact_eligibility({}, context())
    with pytest_raises(InvalidRequestError, match="EligibilityContext"):
        evaluate_artifact_eligibility(accepted_artifact(), {})
