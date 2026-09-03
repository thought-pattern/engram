"""Current disclosure and publication revalidation tests for EGR-705."""

from datetime import UTC, datetime
from unittest.mock import Mock

from pytest import mark as pytest_mark, raises as pytest_raises

from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.evidence import (
    ExactScopeVisibilityAuthority,
    PropositionEligibilityEvaluator,
    PropositionEligibilityReason,
    proposition_evidence_record,
    proposition_validity_inputs_from_eligibility,
    revalidate_propositions,
    validate_proposition_eligibility_decision,
    visibility_authorization,
    visibility_grant,
)
from engram.graph import PropositionProjectionQuery, proposition_projection, proposition_projection_from_graph_row
from engram.identity import scope_key
from engram.resolution import PropositionOwnership, QueryFrameBuilder, query_frame_with_changes

EVALUATION_TIME = "2026-08-16T12:00:00Z"


def internal_scope(context: str = "tenant:acme") -> dict:
    result = scope_key(namespace="support", context_fingerprint=context)
    return result


DEFAULT_SCOPE = internal_scope()


def internal_frame(scope: dict = DEFAULT_SCOPE, request: str = "What is the account status?") -> dict:
    result = QueryFrameBuilder(
        Engram(),
        lambda: 1_000_000_000,
        lambda: datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    ).build(request, scope)
    return result


def internal_projection(ownership: str = "PUBLIC") -> dict:
    row = {
        "proposition_id": "proposition:account-status",
        "subject_entity_id": "entity:account",
        "predicate_id": "predicate:status",
        "object_entity_id": "entity:active",
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
        "ownership_category": ownership,
        "trust_category": "",
        "trust_category_available": False,
        "supplied_trust": 0.0,
        "supplied_trust_available": False,
        "supplied_trust_version": 0,
        "supplied_trust_version_available": False,
        "structured_match": 1.0,
        "structured_match_available": True,
        "semantic_similarity": 0.0,
        "semantic_similarity_available": False,
    }
    result = proposition_projection_from_graph_row(row, PropositionProjectionQuery.STRUCTURED_ENTITY_V1)
    return result


def changed_projection(projection: dict, **changes) -> dict:
    values = dict(projection)
    values.update(changes)
    result = proposition_projection(**values)
    return result


def internal_current(projection: dict, **changes) -> dict:
    result = changed_projection(
        projection,
        projection_id=PropositionProjectionQuery.BY_ID_V1,
        structured_match=0.0,
        structured_match_available=False,
        semantic_similarity=0.0,
        semantic_similarity_available=False,
        **changes,
    )
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
    decision = PropositionEligibilityEvaluator().evaluate(changed_projection(internal_projection(), **changes), internal_frame())

    assert decision["eligible"] is False
    assert decision["disclosure_available"] is False
    assert decision["reason"] == reason


def test_current_validity_is_lower_inclusive_and_upper_exclusive() -> None:
    evaluator = PropositionEligibilityEvaluator()
    frame = internal_frame()
    lower = changed_projection(internal_projection(), valid_from=EVALUATION_TIME, valid_from_available=True)
    upper = changed_projection(internal_projection(), valid_to=EVALUATION_TIME, valid_to_available=True)

    assert evaluator.evaluate(lower, frame)["eligible"] is True
    assert evaluator.evaluate(upper, frame)["reason"] == PropositionEligibilityReason.VALID_TIME_NO_LONGER_CURRENT


def test_historical_valid_time_uses_the_requested_interval_without_presenting_it_as_current() -> None:
    historical = changed_projection(
        internal_projection(),
        valid_from="2024-01-01T00:00:00Z",
        valid_from_available=True,
        valid_to="2025-01-01T00:00:00Z",
        valid_to_available=True,
    )
    evaluator = PropositionEligibilityEvaluator()

    current = evaluator.evaluate(historical, internal_frame())
    requested = evaluator.evaluate(historical, internal_frame(request="What was the account status in 2024?"))

    assert current["reason"] == PropositionEligibilityReason.VALID_TIME_NO_LONGER_CURRENT
    assert requested["eligible"] is True


def test_historical_ranges_preserve_half_open_boundaries() -> None:
    frame = internal_frame(request="What was the account status in 2024?")
    ended_at_start = changed_projection(
        internal_projection(),
        valid_to="2024-01-01T00:00:00Z",
        valid_to_available=True,
    )
    started_at_end = changed_projection(
        internal_projection(),
        valid_from="2025-01-01T00:00:00Z",
        valid_from_available=True,
    )
    evaluator = PropositionEligibilityEvaluator()

    assert evaluator.evaluate(ended_at_start, frame)["reason"] == PropositionEligibilityReason.VALID_TIME_NO_LONGER_CURRENT
    assert evaluator.evaluate(started_at_end, frame)["reason"] == PropositionEligibilityReason.VALID_TIME_NOT_YET_CURRENT


def test_historical_system_time_observes_the_proposition_lifecycle_at_the_requested_time() -> None:
    historical = changed_projection(
        internal_projection(),
        system_from="2024-01-01T00:00:00Z",
        invalidated_at="2025-01-01T00:00:00Z",
        invalidated_at_available=True,
    )
    evaluator = PropositionEligibilityEvaluator()

    before_invalidation = evaluator.evaluate(historical, internal_frame(request="What was the status as known on 2024-06-01?"))
    after_invalidation = evaluator.evaluate(historical, internal_frame(request="What was the status as known on 2025-06-01?"))

    assert before_invalidation["eligible"] is True
    assert after_invalidation["reason"] == PropositionEligibilityReason.SYSTEM_NO_LONGER_CURRENT


def test_unresolved_temporal_expression_fails_closed() -> None:
    decision = PropositionEligibilityEvaluator().evaluate(
        internal_projection(), internal_frame(request="What was the status before last spring?")
    )

    assert decision["reason"] == PropositionEligibilityReason.TEMPORAL_QUERY_UNRESOLVED
    assert decision["eligible"] is False


def test_historical_queries_reuse_the_same_exact_scope_visibility_decision() -> None:
    projection = changed_projection(
        internal_projection("COMPANY"),
        valid_from="2024-01-01T00:00:00Z",
        valid_from_available=True,
        valid_to="2025-01-01T00:00:00Z",
        valid_to_available=True,
    )
    frame = internal_frame(request="What was the status in 2024?")
    authority = ExactScopeVisibilityAuthority(
        "tapestry-visibility",
        "visibility-v3",
        (visibility_grant(frame["scope"], PropositionOwnership.COMPANY),),
    )

    unavailable = PropositionEligibilityEvaluator().evaluate(projection, frame)
    allowed = PropositionEligibilityEvaluator(authority).evaluate(projection, frame)

    assert unavailable["reason"] == PropositionEligibilityReason.VISIBILITY_AUTHORITY_UNAVAILABLE
    assert allowed["reason"] == PropositionEligibilityReason.ELIGIBLE_TRUSTED_SCOPE


def test_latest_valid_time_excludes_propositions_that_have_not_started() -> None:
    future = changed_projection(
        internal_projection(),
        valid_from="2027-01-01T00:00:00Z",
        valid_from_available=True,
    )

    decision = PropositionEligibilityEvaluator().evaluate(future, internal_frame(request="What is the latest status?"))

    assert decision["reason"] == PropositionEligibilityReason.VALID_TIME_NOT_YET_CURRENT


def test_historical_evidence_reports_request_match_without_marking_the_proposition_current() -> None:
    valid_history = changed_projection(
        internal_projection(),
        valid_from="2024-01-01T00:00:00Z",
        valid_from_available=True,
        valid_to="2025-01-01T00:00:00Z",
        valid_to_available=True,
    )
    system_history = changed_projection(
        internal_projection(),
        system_from="2024-01-01T00:00:00Z",
        invalidated_at="2025-01-01T00:00:00Z",
        invalidated_at_available=True,
    )
    evaluator = PropositionEligibilityEvaluator()

    valid_frame = internal_frame(request="What was the status in 2024?")
    valid_decision = evaluator.revalidate(
        valid_history, valid_frame, lambda internal_proposition_id: (internal_current(valid_history),)
    )
    valid_inputs = proposition_validity_inputs_from_eligibility(valid_decision, valid_frame)

    system_frame = internal_frame(request="What was the status as known on 2024-06-01?")
    system_decision = evaluator.revalidate(
        system_history, system_frame, lambda internal_proposition_id: (internal_current(system_history),)
    )
    system_inputs = proposition_validity_inputs_from_eligibility(system_decision, system_frame)

    assert valid_inputs["eligible_for_request"] is True
    assert valid_inputs["active"] is valid_inputs["system_current"] is True
    assert valid_inputs["valid_time_current"] is False
    assert valid_inputs["system_time_match"] is valid_inputs["valid_time_match"] is True
    assert valid_inputs["valid_time_match_available"] is True

    assert system_inputs["eligible_for_request"] is system_inputs["system_time_match"] is True
    assert system_inputs["active"] is system_inputs["system_current"] is False
    assert system_inputs["valid_time_match"] is system_inputs["valid_time_match_available"] is False


def test_public_proposition_uses_explicit_public_rule_without_authority() -> None:
    frame = internal_frame()
    decision = PropositionEligibilityEvaluator().evaluate(internal_projection(), frame)

    assert type(decision) is dict
    assert decision["eligible"] is True
    assert decision["reason"] == PropositionEligibilityReason.ELIGIBLE_PUBLIC
    assert decision["disclosure"]["scope"] == frame["scope"]
    assert decision["disclosure"]["authority_available"] is False
    assert decision["disclosure"]["policy_version"] == "proposition-disclosure-v1"
    copied = validate_proposition_eligibility_decision(decision)
    assert copied == decision
    assert copied is not decision
    assert copied["projection"] is not decision["projection"]
    assert copied["disclosure"] is not decision["disclosure"]


def test_private_proposition_requires_configured_exact_scope_and_ownership() -> None:
    projection = internal_projection("COMPANY")
    frame = internal_frame()
    unavailable = PropositionEligibilityEvaluator().evaluate(projection, frame)
    authority = ExactScopeVisibilityAuthority(
        "tapestry-visibility",
        "visibility-v3",
        (visibility_grant(frame["scope"], PropositionOwnership.COMPANY),),
    )
    allowed = PropositionEligibilityEvaluator(authority).evaluate(projection, frame)
    wrong_context = PropositionEligibilityEvaluator(authority).evaluate(projection, internal_frame(internal_scope("tenant:other")))
    wrong_ownership = PropositionEligibilityEvaluator(authority).evaluate(internal_projection("CUSTOMER"), frame)

    assert unavailable["reason"] == PropositionEligibilityReason.VISIBILITY_AUTHORITY_UNAVAILABLE
    assert allowed["eligible"] is True
    assert allowed["reason"] == PropositionEligibilityReason.ELIGIBLE_TRUSTED_SCOPE
    assert allowed["disclosure"]["authority"] == "tapestry-visibility"
    assert allowed["disclosure"]["scope"] == frame["scope"]
    assert wrong_context["reason"] == PropositionEligibilityReason.VISIBILITY_DENIED
    assert wrong_ownership["reason"] == PropositionEligibilityReason.VISIBILITY_DENIED
    assert allowed["projection"]["supplied_trust"] == 0.0
    assert allowed["projection"]["supplied_trust_available"] is False


def test_visibility_authority_configuration_is_bounded() -> None:
    grant = visibility_grant(internal_scope(), PropositionOwnership.COMPANY)

    with pytest_raises(InvalidRequestError, match="4096"):
        ExactScopeVisibilityAuthority("authority", "v1", (grant,) * 4_097)


def test_visibility_evaluator_rejects_falsey_invalid_authority() -> None:
    with pytest_raises(InvalidRequestError, match="must implement evaluate"):
        PropositionEligibilityEvaluator(False)


def test_visibility_authority_result_must_match_exact_input() -> None:
    def wrong_scope(scope_value, ownership):
        result = visibility_authorization(
            True,
            internal_scope("tenant:other"),
            ownership,
            "wrong-scope",
            "v1",
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
            "v1",
            "granted",
        )
        return result

    wrong_scope_authority = Mock()
    wrong_scope_authority.evaluate.side_effect = wrong_scope
    wrong_ownership_authority = Mock()
    wrong_ownership_authority.evaluate.side_effect = wrong_ownership

    projection = internal_projection("COMPANY")
    frame = internal_frame()

    assert (
        PropositionEligibilityEvaluator(wrong_scope_authority).evaluate(projection, frame)["reason"]
        == PropositionEligibilityReason.VISIBILITY_SCOPE_MISMATCH
    )
    assert (
        PropositionEligibilityEvaluator(wrong_ownership_authority).evaluate(projection, frame)["reason"]
        == PropositionEligibilityReason.VISIBILITY_OWNERSHIP_MISMATCH
    )


def test_evaluation_time_unavailability_fails_closed() -> None:
    frame = internal_frame()
    context = dict(frame["eligibility_context"])
    context["evaluation_time"] = ""
    context["evaluation_time_available"] = False
    unavailable_frame = query_frame_with_changes(frame, {"eligibility_context": context})

    decision = PropositionEligibilityEvaluator().evaluate(internal_projection(), unavailable_frame)

    assert decision["reason"] == PropositionEligibilityReason.EVALUATION_TIME_UNAVAILABLE
    assert decision["eligible"] is False


def test_revalidation_rejects_missing_changed_and_newly_ineligible_propositions() -> None:
    discovered = internal_projection()
    frame = internal_frame()
    evaluator = PropositionEligibilityEvaluator()

    class Reader:
        def __init__(self, values: tuple[dict, ...]) -> None:
            self.values = values

        def current_proposition_projection(self, proposition_id: str) -> tuple[dict, ...]:
            del proposition_id
            result = self.values
            return result

    missing = evaluator.revalidate(discovered, frame, Reader(()).current_proposition_projection)
    changed = evaluator.revalidate(
        discovered,
        frame,
        Reader((internal_current(discovered, object_entity_id="entity:changed"),)).current_proposition_projection,
    )
    inactive = evaluator.revalidate(
        discovered,
        frame,
        Reader(
            (
                internal_current(
                    discovered,
                    invalidated_at="2026-08-16T11:00:00Z",
                    invalidated_at_available=True,
                ),
            )
        ).current_proposition_projection,
    )
    wrong_projection = evaluator.revalidate(discovered, frame, Reader((discovered,)).current_proposition_projection)

    assert missing["reason"] == PropositionEligibilityReason.REVALIDATION_MISSING
    assert changed["reason"] == PropositionEligibilityReason.REVALIDATION_IDENTITY_CONFLICT
    assert changed["revalidated"] is True
    assert inactive["reason"] == PropositionEligibilityReason.PROPOSITION_INACTIVE
    assert inactive["revalidated"] is True
    assert wrong_projection["reason"] == PropositionEligibilityReason.REVALIDATION_IDENTITY_CONFLICT
    assert wrong_projection["revalidated"] is True


def test_eligible_revalidation_uses_current_trust_and_builds_validity_inputs() -> None:
    discovered = internal_projection()
    current = internal_current(
        discovered,
        supplied_trust=0.0,
        supplied_trust_available=True,
        supplied_trust_version=4,
        supplied_trust_version_available=True,
    )

    def current_proposition_projection(proposition_id: str) -> tuple[dict, ...]:
        del proposition_id
        result = (current,)
        return result

    frame = internal_frame()
    evaluator = PropositionEligibilityEvaluator()
    decision = evaluator.revalidate(discovered, frame, current_proposition_projection)
    validity = proposition_validity_inputs_from_eligibility(decision, frame)

    assert decision["eligible"] is True
    assert decision["revalidated"] is True
    assert decision["projection"]["supplied_trust"] == 0.0
    assert decision["projection"]["supplied_trust_available"] is True
    assert decision["projection"]["supplied_trust_version"] == 4
    assert validity["evaluation_time"] == EVALUATION_TIME
    assert validity["active"] is validity["system_current"] is validity["valid_time_current"] is True
    assert validity["temporal_operator"].value == "unspecified"
    assert validity["temporal_axis"].value == "valid_time"
    assert validity["system_from"] == "2026-01-01T00:00:00Z"
    assert validity["system_from_available"] is True


def test_proposition_record_construction_requires_allowed_matching_discovery_provenance() -> None:
    discovered = internal_projection()
    frame = internal_frame()

    def current_proposition_projection(proposition_id: str) -> tuple[dict, ...]:
        del proposition_id
        result = (internal_current(discovered),)
        return result

    decision = PropositionEligibilityEvaluator().revalidate(discovered, frame, current_proposition_projection)

    with pytest_raises(InvalidRequestError, match="allowed producer"):
        proposition_evidence_record(discovered, decision, frame, "lexical")
    with pytest_raises(InvalidRequestError, match="vector discovery"):
        proposition_evidence_record(discovered, decision, frame, "support_semantic")


def test_batch_revalidation_is_bounded_and_cooperative() -> None:
    discovered = internal_projection()
    current = internal_current(discovered)
    checks = []

    def current_proposition_projection(proposition_id: str) -> tuple[dict, ...]:
        del proposition_id
        result = (current,)
        return result

    decisions = revalidate_propositions(
        (discovered,),
        internal_frame(),
        PropositionEligibilityEvaluator(),
        current_proposition_projection,
        cooperative_check=lambda: checks.append("checked"),
    )

    assert len(decisions) == 1 and decisions[0]["eligible"]
    assert checks == ["checked", "checked"]
    with pytest_raises(InvalidRequestError, match="at most 1000"):
        revalidate_propositions(
            (discovered,) * 1_001, internal_frame(), PropositionEligibilityEvaluator(), current_proposition_projection
        )


def test_validity_inputs_require_publication_revalidation() -> None:
    evaluator = PropositionEligibilityEvaluator()
    initial = evaluator.evaluate(internal_projection(), internal_frame())

    with pytest_raises(InvalidRequestError, match="revalidated"):
        proposition_validity_inputs_from_eligibility(initial, internal_frame())
