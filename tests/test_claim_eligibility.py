"""Current disclosure and publication revalidation tests for EGR-705."""

from datetime import UTC, datetime
from unittest.mock import Mock

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.constants import TemporalAxis, TemporalQueryOperator
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.evidence import (
    ExactScopeVisibilityAuthority,
    PropositionEligibilityEvaluator,
    PropositionEligibilityReason,
    proposition_evidence_record,
    proposition_validity_inputs_from_eligibility,
    validate_proposition_eligibility_decision,
    visibility_authorization,
    visibility_grant,
)
from engram.graph import PropositionProjectionQuery, proposition_projection_from_graph_row, validate_proposition_projection
from engram.identity import scope_key
from engram.resolution import PropositionOwnership, QueryFrameBuilder, query_frame_with_changes

EVALUATION_TIME = "2026-08-16T12:00:00Z"
EVALUATION_INSTANT = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
CURRENT_REQUEST = "What is the account status?"
DEFAULT_SCOPE = scope_key(namespace="support", context_fingerprint="tenant:acme")
OTHER_TENANT_SCOPE = scope_key(namespace="support", context_fingerprint="tenant:other")

# Read-only fixtures: the evaluator and the projection validator copy them, and changed projections
# are new dictionaries revalidated through validate_proposition_projection.
ACCOUNT_STATUS_ROW = {
    "proposition_id": "proposition:account-status",
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
    "system_from": "2026-01-01T00:00:00Z",
    "system_from_available": True,
    "system_to": "",
    "system_to_available": False,
    "valid_from": "",
    "valid_from_available": False,
    "valid_to": "",
    "valid_to_available": False,
    "predicate_canonical": True,
    "ownership_category": "PUBLIC",
    "trust_category": "",
    "trust_category_available": False,
    "supplied_trust": 0.0,
    "supplied_trust_available": False,
    "structured_match": 1.0,
    "structured_match_available": True,
    "semantic_similarity": 0.0,
    "semantic_similarity_available": False,
}
PUBLIC_PROJECTION = proposition_projection_from_graph_row(dict(ACCOUNT_STATUS_ROW), PropositionProjectionQuery.STRUCTURED_ENTITY)
COMPANY_PROJECTION = proposition_projection_from_graph_row(
    {**ACCOUNT_STATUS_ROW, "ownership_category": "COMPANY"}, PropositionProjectionQuery.STRUCTURED_ENTITY
)
CUSTOMER_PROJECTION = proposition_projection_from_graph_row(
    {**ACCOUNT_STATUS_ROW, "ownership_category": "CUSTOMER"}, PropositionProjectionQuery.STRUCTURED_ENTITY
)
# The fixed by-ID read returns the current record without discovery measurements.
BY_ID_REVALIDATION_FIELDS = {
    "projection_id": PropositionProjectionQuery.BY_ID,
    "structured_match": 0.0,
    "structured_match_available": False,
    "semantic_similarity": 0.0,
    "semantic_similarity_available": False,
}


def fixed_monotonic_ns() -> int:
    result = 1_000_000_000
    return result


def evaluation_clock() -> datetime:
    result = EVALUATION_INSTANT
    return result


@pytest_mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"invalidated_at": "2026-08-01T00:00:00Z", "invalidated_at_available": True},
            PropositionEligibilityReason.PROPOSITION_INACTIVE,
        ),
        (
            {"system_from": "", "system_from_available": False},
            PropositionEligibilityReason.SYSTEM_TIME_UNAVAILABLE,
        ),
        (
            {"system_from": "2027-01-01T00:00:00Z"},
            PropositionEligibilityReason.SYSTEM_NOT_YET_CURRENT,
        ),
        (
            {"system_to": "2026-08-01T00:00:00Z", "system_to_available": True},
            PropositionEligibilityReason.SYSTEM_NO_LONGER_CURRENT,
        ),
        (
            {"valid_from": "2027-01-01T00:00:00Z", "valid_from_available": True},
            PropositionEligibilityReason.VALID_TIME_NOT_YET_CURRENT,
        ),
        (
            {"valid_to": EVALUATION_TIME, "valid_to_available": True},
            PropositionEligibilityReason.VALID_TIME_NO_LONGER_CURRENT,
        ),
        ({"predicate_canonical": False}, PropositionEligibilityReason.RETRIEVAL_ONLY),
        ({"predicate_id": "generic_relation"}, PropositionEligibilityReason.RETRIEVAL_ONLY),
    ],
)
def test_current_proposition_eligibility_truth_table(changes, reason) -> None:
    projection = validate_proposition_projection({**PUBLIC_PROJECTION, **changes})
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)
    decision = PropositionEligibilityEvaluator().evaluate(projection, frame)

    assert "eligible" in decision
    assert decision.get("eligible", False) is False
    assert "disclosure_available" in decision
    assert decision.get("disclosure_available", False) is False
    assert decision.get("reason", "") == reason


def test_current_validity_is_lower_inclusive_and_upper_exclusive() -> None:
    evaluator = PropositionEligibilityEvaluator()
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)
    lower = validate_proposition_projection({**PUBLIC_PROJECTION, "valid_from": EVALUATION_TIME, "valid_from_available": True})
    upper = validate_proposition_projection({**PUBLIC_PROJECTION, "valid_to": EVALUATION_TIME, "valid_to_available": True})

    assert evaluator.evaluate(lower, frame).get("eligible", False) is True
    assert evaluator.evaluate(upper, frame).get("reason", "") == PropositionEligibilityReason.VALID_TIME_NO_LONGER_CURRENT


def test_historical_valid_time_uses_the_requested_interval_without_presenting_it_as_current() -> None:
    historical = validate_proposition_projection(
        {
            **PUBLIC_PROJECTION,
            "valid_from": "2024-01-01T00:00:00Z",
            "valid_from_available": True,
            "valid_to": "2025-01-01T00:00:00Z",
            "valid_to_available": True,
        }
    )
    evaluator = PropositionEligibilityEvaluator()
    current_frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)
    requested_frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(
        "What was the account status in 2024?", DEFAULT_SCOPE
    )

    current = evaluator.evaluate(historical, current_frame)
    requested = evaluator.evaluate(historical, requested_frame)

    assert current.get("reason", "") == PropositionEligibilityReason.VALID_TIME_NO_LONGER_CURRENT
    assert requested.get("eligible", False) is True


def test_historical_ranges_preserve_half_open_boundaries() -> None:
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(
        "What was the account status in 2024?", DEFAULT_SCOPE
    )
    ended_at_start = validate_proposition_projection(
        {**PUBLIC_PROJECTION, "valid_to": "2024-01-01T00:00:00Z", "valid_to_available": True}
    )
    started_at_end = validate_proposition_projection(
        {**PUBLIC_PROJECTION, "valid_from": "2025-01-01T00:00:00Z", "valid_from_available": True}
    )
    evaluator = PropositionEligibilityEvaluator()

    ended_decision = evaluator.evaluate(ended_at_start, frame)
    started_decision = evaluator.evaluate(started_at_end, frame)
    assert ended_decision.get("reason", "") == PropositionEligibilityReason.VALID_TIME_NO_LONGER_CURRENT
    assert started_decision.get("reason", "") == PropositionEligibilityReason.VALID_TIME_NOT_YET_CURRENT


def test_historical_system_time_observes_the_proposition_lifecycle_at_the_requested_time() -> None:
    historical = validate_proposition_projection(
        {
            **PUBLIC_PROJECTION,
            "system_from": "2024-01-01T00:00:00Z",
            "invalidated_at": "2025-01-01T00:00:00Z",
            "invalidated_at_available": True,
        }
    )
    evaluator = PropositionEligibilityEvaluator()
    before_frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(
        "What was the status as known on 2024-06-01?", DEFAULT_SCOPE
    )
    after_frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(
        "What was the status as known on 2025-06-01?", DEFAULT_SCOPE
    )

    before_invalidation = evaluator.evaluate(historical, before_frame)
    after_invalidation = evaluator.evaluate(historical, after_frame)

    assert before_invalidation.get("eligible", False) is True
    assert after_invalidation.get("reason", "") == PropositionEligibilityReason.SYSTEM_NO_LONGER_CURRENT


def test_unresolved_temporal_expression_fails_closed() -> None:
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(
        "What was the status before last spring?", DEFAULT_SCOPE
    )
    decision = PropositionEligibilityEvaluator().evaluate(PUBLIC_PROJECTION, frame)

    assert decision.get("reason", "") == PropositionEligibilityReason.TEMPORAL_QUERY_UNRESOLVED
    assert "eligible" in decision
    assert decision.get("eligible", False) is False


def test_historical_queries_reuse_the_same_exact_scope_visibility_decision() -> None:
    projection = validate_proposition_projection(
        {
            **COMPANY_PROJECTION,
            "valid_from": "2024-01-01T00:00:00Z",
            "valid_from_available": True,
            "valid_to": "2025-01-01T00:00:00Z",
            "valid_to_available": True,
        }
    )
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build("What was the status in 2024?", DEFAULT_SCOPE)
    authority = ExactScopeVisibilityAuthority(
        "visibility-authority",
        (visibility_grant(frame.get("scope", {}), PropositionOwnership.COMPANY),),
    )

    unavailable = PropositionEligibilityEvaluator().evaluate(projection, frame)
    allowed = PropositionEligibilityEvaluator(authority).evaluate(projection, frame)

    assert unavailable.get("reason", "") == PropositionEligibilityReason.VISIBILITY_AUTHORITY_UNAVAILABLE
    assert allowed.get("reason", "") == PropositionEligibilityReason.ELIGIBLE_TRUSTED_SCOPE


def test_latest_valid_time_excludes_propositions_that_have_not_started() -> None:
    future = validate_proposition_projection(
        {**PUBLIC_PROJECTION, "valid_from": "2027-01-01T00:00:00Z", "valid_from_available": True}
    )
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build("What is the latest status?", DEFAULT_SCOPE)

    decision = PropositionEligibilityEvaluator().evaluate(future, frame)

    assert decision.get("reason", "") == PropositionEligibilityReason.VALID_TIME_NOT_YET_CURRENT


def test_historical_evidence_reports_request_match_without_marking_the_proposition_current() -> None:
    valid_history = validate_proposition_projection(
        {
            **PUBLIC_PROJECTION,
            "valid_from": "2024-01-01T00:00:00Z",
            "valid_from_available": True,
            "valid_to": "2025-01-01T00:00:00Z",
            "valid_to_available": True,
        }
    )
    system_history = validate_proposition_projection(
        {
            **PUBLIC_PROJECTION,
            "system_from": "2024-01-01T00:00:00Z",
            "invalidated_at": "2025-01-01T00:00:00Z",
            "invalidated_at_available": True,
        }
    )
    current_valid_history = validate_proposition_projection({**valid_history, **BY_ID_REVALIDATION_FIELDS})
    current_system_history = validate_proposition_projection({**system_history, **BY_ID_REVALIDATION_FIELDS})
    evaluator = PropositionEligibilityEvaluator()

    valid_frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(
        "What was the status in 2024?", DEFAULT_SCOPE
    )
    valid_decision = evaluator.revalidate(
        valid_history, valid_frame, lambda internal_proposition_id, internal_basis_window: (current_valid_history,)
    )
    valid_inputs = proposition_validity_inputs_from_eligibility(valid_decision, valid_frame)

    system_frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(
        "What was the status as known on 2024-06-01?", DEFAULT_SCOPE
    )
    system_decision = evaluator.revalidate(
        system_history, system_frame, lambda internal_proposition_id, internal_basis_window: (current_system_history,)
    )
    system_inputs = proposition_validity_inputs_from_eligibility(system_decision, system_frame)

    for field in ("valid_time_current", "active", "system_current", "valid_time_match", "valid_time_match_available"):
        assert field in valid_inputs
        assert field in system_inputs
    assert valid_inputs.get("eligible_for_request", False) is True
    assert valid_inputs.get("active", False) is valid_inputs.get("system_current", False) is True
    assert valid_inputs.get("valid_time_current", False) is False
    assert valid_inputs.get("system_time_match", False) is valid_inputs.get("valid_time_match", False) is True
    assert valid_inputs.get("valid_time_match_available", False) is True

    assert system_inputs.get("eligible_for_request", False) is system_inputs.get("system_time_match", False) is True
    assert system_inputs.get("active", False) is system_inputs.get("system_current", False) is False
    assert system_inputs.get("valid_time_match", False) is system_inputs.get("valid_time_match_available", False) is False


def test_public_proposition_uses_explicit_public_rule_without_authority() -> None:
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)
    decision = PropositionEligibilityEvaluator().evaluate(PUBLIC_PROJECTION, frame)
    disclosure = decision.get("disclosure", {})

    assert type(decision) is dict
    assert decision.get("eligible", False) is True
    assert decision.get("reason", "") == PropositionEligibilityReason.ELIGIBLE_PUBLIC
    assert "scope" in frame
    assert disclosure.get("scope", {}) == frame.get("scope", {})
    assert "authority_available" in disclosure
    assert disclosure.get("authority_available", False) is False
    copied = validate_proposition_eligibility_decision(decision)
    assert copied == decision
    assert copied is not decision
    assert "projection" in decision
    assert copied.get("projection", {}) is not decision.get("projection", {})
    assert "disclosure" in decision
    assert copied.get("disclosure", {}) is not disclosure


def test_private_proposition_requires_configured_exact_scope_and_ownership() -> None:
    projection = COMPANY_PROJECTION
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)
    other_frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, OTHER_TENANT_SCOPE)
    unavailable = PropositionEligibilityEvaluator().evaluate(projection, frame)
    authority = ExactScopeVisibilityAuthority(
        "visibility-authority",
        (visibility_grant(frame.get("scope", {}), PropositionOwnership.COMPANY),),
    )
    allowed = PropositionEligibilityEvaluator(authority).evaluate(projection, frame)
    wrong_context = PropositionEligibilityEvaluator(authority).evaluate(projection, other_frame)
    wrong_ownership = PropositionEligibilityEvaluator(authority).evaluate(CUSTOMER_PROJECTION, frame)
    allowed_disclosure = allowed.get("disclosure", {})
    allowed_projection = allowed.get("projection", {})

    assert unavailable.get("reason", "") == PropositionEligibilityReason.VISIBILITY_AUTHORITY_UNAVAILABLE
    assert allowed.get("eligible", False) is True
    assert allowed.get("reason", "") == PropositionEligibilityReason.ELIGIBLE_TRUSTED_SCOPE
    assert allowed_disclosure.get("authority", "") == "visibility-authority"
    assert "scope" in frame
    assert allowed_disclosure.get("scope", {}) == frame.get("scope", {})
    assert wrong_context.get("reason", "") == PropositionEligibilityReason.VISIBILITY_DENIED
    assert wrong_ownership.get("reason", "") == PropositionEligibilityReason.VISIBILITY_DENIED
    assert "supplied_trust" in allowed_projection
    assert allowed_projection.get("supplied_trust", 0.0) == 0.0
    assert "supplied_trust_available" in allowed_projection
    assert allowed_projection.get("supplied_trust_available", False) is False


def test_visibility_authority_configuration_is_bounded() -> None:
    grant = visibility_grant(DEFAULT_SCOPE, PropositionOwnership.COMPANY)

    with pytest_raises(InvalidRequestError, match="4096"):
        ExactScopeVisibilityAuthority("authority", (grant,) * 4_097)


def test_visibility_evaluator_rejects_falsey_invalid_authority() -> None:
    with pytest_raises(InvalidRequestError, match="must implement evaluate"):
        PropositionEligibilityEvaluator(False)


def test_visibility_authority_result_must_match_exact_input() -> None:
    def wrong_scope(scope_value, ownership):
        result = visibility_authorization(
            True,
            OTHER_TENANT_SCOPE,
            ownership,
            "wrong-scope",
            "granted",
        )
        return result

    def wrong_ownership(scope, internal_ownership):
        del internal_ownership
        result = visibility_authorization(
            True,
            scope,
            PropositionOwnership.CUSTOMER,
            "wrong-owner",
            "granted",
        )
        return result

    wrong_scope_authority = Mock()
    wrong_scope_authority.evaluate.side_effect = wrong_scope
    wrong_ownership_authority = Mock()
    wrong_ownership_authority.evaluate.side_effect = wrong_ownership

    projection = COMPANY_PROJECTION
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)

    assert (
        PropositionEligibilityEvaluator(wrong_scope_authority).evaluate(projection, frame).get("reason", "")
        == PropositionEligibilityReason.VISIBILITY_SCOPE_MISMATCH
    )
    assert (
        PropositionEligibilityEvaluator(wrong_ownership_authority).evaluate(projection, frame).get("reason", "")
        == PropositionEligibilityReason.VISIBILITY_OWNERSHIP_MISMATCH
    )


def test_evaluation_time_unavailability_fails_closed() -> None:
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)
    assert "eligibility_context" in frame
    context = dict(frame.get("eligibility_context", {}))
    context["evaluation_time"] = ""
    context["evaluation_time_available"] = False
    unavailable_frame = query_frame_with_changes(frame, {"eligibility_context": context})

    decision = PropositionEligibilityEvaluator().evaluate(PUBLIC_PROJECTION, unavailable_frame)

    assert decision.get("reason", "") == PropositionEligibilityReason.EVALUATION_TIME_UNAVAILABLE
    assert "eligible" in decision
    assert decision.get("eligible", False) is False


def test_revalidation_rejects_missing_changed_and_newly_ineligible_propositions() -> None:
    discovered = PUBLIC_PROJECTION
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)
    evaluator = PropositionEligibilityEvaluator()

    class Reader:
        def __init__(self, values: tuple[dict, ...]) -> None:
            self.values = values

        def current_proposition_projection(self, proposition_id: str, basis_window: dict) -> tuple[dict, ...]:
            del proposition_id, basis_window
            result = self.values
            return result

    changed_current = validate_proposition_projection(
        {**discovered, **BY_ID_REVALIDATION_FIELDS, "object_entity_id": "entity:changed"}
    )
    inactive_current = validate_proposition_projection(
        {
            **discovered,
            **BY_ID_REVALIDATION_FIELDS,
            "invalidated_at": "2026-08-16T11:00:00Z",
            "invalidated_at_available": True,
        }
    )
    missing = evaluator.revalidate(discovered, frame, Reader(()).current_proposition_projection)
    changed = evaluator.revalidate(discovered, frame, Reader((changed_current,)).current_proposition_projection)
    inactive = evaluator.revalidate(discovered, frame, Reader((inactive_current,)).current_proposition_projection)
    wrong_projection = evaluator.revalidate(discovered, frame, Reader((discovered,)).current_proposition_projection)

    assert missing.get("reason", "") == PropositionEligibilityReason.REVALIDATION_MISSING
    assert changed.get("reason", "") == PropositionEligibilityReason.REVALIDATION_IDENTITY_CONFLICT
    assert changed.get("revalidated", False) is True
    assert inactive.get("reason", "") == PropositionEligibilityReason.PROPOSITION_INACTIVE
    assert inactive.get("revalidated", False) is True
    assert wrong_projection.get("reason", "") == PropositionEligibilityReason.REVALIDATION_IDENTITY_CONFLICT
    assert wrong_projection.get("revalidated", False) is True


def test_eligible_revalidation_uses_current_trust_and_builds_validity_inputs() -> None:
    discovered = PUBLIC_PROJECTION
    current = validate_proposition_projection(
        {**discovered, **BY_ID_REVALIDATION_FIELDS, "supplied_trust": 0.0, "supplied_trust_available": True}
    )

    def current_proposition_projection(proposition_id: str, basis_window: dict) -> tuple[dict, ...]:
        del proposition_id, basis_window
        result = (current,)
        return result

    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)
    evaluator = PropositionEligibilityEvaluator()
    decision = evaluator.revalidate(discovered, frame, current_proposition_projection)
    validity = proposition_validity_inputs_from_eligibility(decision, frame)
    decision_projection = decision.get("projection", {})

    assert decision.get("eligible", False) is True
    assert decision.get("revalidated", False) is True
    assert "supplied_trust" in decision_projection
    assert decision_projection.get("supplied_trust", 0.0) == 0.0
    assert decision_projection.get("supplied_trust_available", False) is True
    assert validity.get("evaluation_time", "") == EVALUATION_TIME
    assert validity.get("active", False) is True
    assert validity.get("system_current", False) is True
    assert validity.get("valid_time_current", False) is True
    assert "temporal_operator" in validity
    assert validity.get("temporal_operator", TemporalQueryOperator.UNSPECIFIED).value == "unspecified"
    assert validity.get("temporal_axis", TemporalAxis.SYSTEM_TIME).value == "valid_time"
    assert validity.get("system_from", "") == "2026-01-01T00:00:00Z"
    assert validity.get("system_from_available", False) is True


def test_proposition_record_construction_requires_allowed_matching_discovery_provenance() -> None:
    discovered = PUBLIC_PROJECTION
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)

    def current_proposition_projection(proposition_id: str, basis_window: dict) -> tuple[dict, ...]:
        del proposition_id, basis_window
        current = validate_proposition_projection({**discovered, **BY_ID_REVALIDATION_FIELDS})
        result = (current,)
        return result

    decision = PropositionEligibilityEvaluator().revalidate(discovered, frame, current_proposition_projection)

    with pytest_raises(InvalidRequestError, match="allowed producer"):
        proposition_evidence_record(discovered, decision, frame, "lexical")
    with pytest_raises(InvalidRequestError, match="vector discovery"):
        proposition_evidence_record(discovered, decision, frame, "support_semantic")


def test_validity_inputs_require_publication_revalidation() -> None:
    evaluator = PropositionEligibilityEvaluator()
    frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)
    initial = evaluator.evaluate(PUBLIC_PROJECTION, frame)
    validity_frame = QueryFrameBuilder(Engram(), fixed_monotonic_ns, evaluation_clock).build(CURRENT_REQUEST, DEFAULT_SCOPE)

    with pytest_raises(InvalidRequestError, match="revalidated"):
        proposition_validity_inputs_from_eligibility(initial, validity_frame)
