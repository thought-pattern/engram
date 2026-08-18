"""Current disclosure and publication revalidation tests for EGR-705."""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.evidence import (
    ClaimEligibilityEvaluator,
    ClaimEligibilityReason,
    ExactScopeVisibilityAuthority,
    claim_evidence_record,
    claim_validity_inputs_from_eligibility,
    revalidate_claims,
    validate_claim_eligibility_decision,
    visibility_authorization,
    visibility_grant,
)
from engram.graph import ClaimProjection, ClaimProjectionQuery, claim_projection, claim_projection_from_graph_row
from engram.identity import ScopeKey, scope_key
from engram.resolution import ClaimOwnership, QueryFrame, QueryFrameBuilder, query_frame_with_changes

EVALUATION_TIME = "2026-08-16T12:00:00Z"


def _scope(context: str = "tenant:acme") -> ScopeKey:
    result = scope_key(namespace="support", context_fingerprint=context)
    return result


DEFAULT_SCOPE = _scope()


def _frame(scope: ScopeKey = DEFAULT_SCOPE) -> QueryFrame:
    result = QueryFrameBuilder(
        Engram(),
        lambda: 1_000_000_000,
        lambda: datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    ).build("What is the account status?", scope)
    return result


def _projection(ownership: str = "PUBLIC") -> ClaimProjection:
    row = {
        "claim_id": "claim:account-status",
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
    result = claim_projection_from_graph_row(row, ClaimProjectionQuery.STRUCTURED_ENTITY_V1)
    return result


def _changed_projection(projection: ClaimProjection, **changes) -> ClaimProjection:
    values = dict(projection)
    values.update(changes)
    result = claim_projection(**values)
    return result


def _current(projection: ClaimProjection, **changes) -> ClaimProjection:
    result = _changed_projection(
        projection,
        projection_id=ClaimProjectionQuery.BY_ID_V1,
        structured_match=0.0,
        structured_match_available=False,
        semantic_similarity=0.0,
        semantic_similarity_available=False,
        **changes,
    )
    return result


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"invalidated_at": "2026-08-01T00:00:00Z", "invalidated_at_available": True},
            ClaimEligibilityReason.CLAIM_INACTIVE,
        ),
        (
            {"system_from": "", "system_from_available": False},
            ClaimEligibilityReason.SYSTEM_TIME_UNAVAILABLE,
        ),
        (
            {"system_from": "2027-01-01T00:00:00Z"},
            ClaimEligibilityReason.SYSTEM_NOT_YET_CURRENT,
        ),
        (
            {"system_to": "2026-08-01T00:00:00Z", "system_to_available": True},
            ClaimEligibilityReason.SYSTEM_NO_LONGER_CURRENT,
        ),
        (
            {"valid_from": "2027-01-01T00:00:00Z", "valid_from_available": True},
            ClaimEligibilityReason.VALID_TIME_NOT_YET_CURRENT,
        ),
        (
            {"valid_to": EVALUATION_TIME, "valid_to_available": True},
            ClaimEligibilityReason.VALID_TIME_NO_LONGER_CURRENT,
        ),
        ({"predicate_canonical": False}, ClaimEligibilityReason.RETRIEVAL_ONLY),
        ({"predicate_id": "generic_relation"}, ClaimEligibilityReason.RETRIEVAL_ONLY),
    ],
)
def test_current_claim_eligibility_truth_table(changes, reason) -> None:
    decision = ClaimEligibilityEvaluator().evaluate(_changed_projection(_projection(), **changes), _frame())

    assert decision["eligible"] is False
    assert decision["disclosure_available"] is False
    assert decision["reason"] == reason


def test_current_validity_is_lower_inclusive_and_upper_exclusive() -> None:
    evaluator = ClaimEligibilityEvaluator()
    frame = _frame()
    lower = _changed_projection(_projection(), valid_from=EVALUATION_TIME, valid_from_available=True)
    upper = _changed_projection(_projection(), valid_to=EVALUATION_TIME, valid_to_available=True)

    assert evaluator.evaluate(lower, frame)["eligible"] is True
    assert evaluator.evaluate(upper, frame)["reason"] == ClaimEligibilityReason.VALID_TIME_NO_LONGER_CURRENT


def test_public_claim_uses_explicit_public_rule_without_authority() -> None:
    frame = _frame()
    decision = ClaimEligibilityEvaluator().evaluate(_projection(), frame)

    assert type(decision) is dict
    assert decision["eligible"] is True
    assert decision["reason"] == ClaimEligibilityReason.ELIGIBLE_PUBLIC
    assert decision["disclosure"]["scope"] == frame["scope"]
    assert decision["disclosure"]["authority_available"] is False
    assert decision["disclosure"]["policy_version"] == "claim-disclosure-v1"
    copied = validate_claim_eligibility_decision(decision)
    assert copied == decision
    assert copied is not decision
    assert copied["projection"] is not decision["projection"]
    assert copied["disclosure"] is not decision["disclosure"]


def test_private_claim_requires_configured_exact_scope_and_ownership() -> None:
    projection = _projection("COMPANY")
    frame = _frame()
    unavailable = ClaimEligibilityEvaluator().evaluate(projection, frame)
    authority = ExactScopeVisibilityAuthority(
        "tapestry-visibility",
        "visibility-v3",
        (visibility_grant(frame["scope"], ClaimOwnership.COMPANY),),
    )
    allowed = ClaimEligibilityEvaluator(authority).evaluate(projection, frame)
    wrong_context = ClaimEligibilityEvaluator(authority).evaluate(projection, _frame(_scope("tenant:other")))
    wrong_ownership = ClaimEligibilityEvaluator(authority).evaluate(_projection("CUSTOMER"), frame)

    assert unavailable["reason"] == ClaimEligibilityReason.VISIBILITY_AUTHORITY_UNAVAILABLE
    assert allowed["eligible"] is True
    assert allowed["reason"] == ClaimEligibilityReason.ELIGIBLE_TRUSTED_SCOPE
    assert allowed["disclosure"]["authority"] == "tapestry-visibility"
    assert allowed["disclosure"]["scope"] == frame["scope"]
    assert wrong_context["reason"] == ClaimEligibilityReason.VISIBILITY_DENIED
    assert wrong_ownership["reason"] == ClaimEligibilityReason.VISIBILITY_DENIED
    assert allowed["projection"]["supplied_trust"] == 0.0
    assert allowed["projection"]["supplied_trust_available"] is False


def test_visibility_authority_configuration_is_bounded() -> None:
    grant = visibility_grant(_scope(), ClaimOwnership.COMPANY)

    with pytest.raises(InvalidRequestError, match="4096"):
        ExactScopeVisibilityAuthority("authority", "v1", (grant,) * 4_097)


def test_visibility_evaluator_rejects_falsey_invalid_authority() -> None:
    with pytest.raises(InvalidRequestError, match="must implement evaluate"):
        ClaimEligibilityEvaluator(False)


def test_visibility_authority_result_must_match_exact_input() -> None:
    def wrong_scope(_scope_value, ownership):
        result = visibility_authorization(
            True,
            _scope("tenant:other"),
            ownership,
            "wrong-scope",
            "v1",
            "granted",
        )
        return result

    def wrong_ownership(scope, _ownership):
        result = visibility_authorization(
            True,
            scope,
            ClaimOwnership.CUSTOMER,
            "wrong-owner",
            "v1",
            "granted",
        )
        return result

    wrong_scope_authority = Mock()
    wrong_scope_authority.evaluate.side_effect = wrong_scope
    wrong_ownership_authority = Mock()
    wrong_ownership_authority.evaluate.side_effect = wrong_ownership

    projection = _projection("COMPANY")
    frame = _frame()

    assert (
        ClaimEligibilityEvaluator(wrong_scope_authority).evaluate(projection, frame)["reason"]
        == ClaimEligibilityReason.VISIBILITY_SCOPE_MISMATCH
    )
    assert (
        ClaimEligibilityEvaluator(wrong_ownership_authority).evaluate(projection, frame)["reason"]
        == ClaimEligibilityReason.VISIBILITY_OWNERSHIP_MISMATCH
    )


def test_evaluation_time_unavailability_fails_closed() -> None:
    frame = _frame()
    context = dict(frame["eligibility_context"])
    context["evaluation_time"] = ""
    context["evaluation_time_available"] = False
    unavailable_frame = query_frame_with_changes(frame, {"eligibility_context": context})

    decision = ClaimEligibilityEvaluator().evaluate(_projection(), unavailable_frame)

    assert decision["reason"] == ClaimEligibilityReason.EVALUATION_TIME_UNAVAILABLE
    assert decision["eligible"] is False


def test_revalidation_rejects_missing_changed_and_newly_ineligible_claims() -> None:
    discovered = _projection()
    frame = _frame()
    evaluator = ClaimEligibilityEvaluator()

    class Reader:
        def __init__(self, values: tuple[ClaimProjection, ...]) -> None:
            self.values = values

        def current_claim_projection(self, claim_id: str) -> tuple[ClaimProjection, ...]:
            del claim_id
            result = self.values
            return result

    missing = evaluator.revalidate(discovered, frame, Reader(()).current_claim_projection)
    changed = evaluator.revalidate(
        discovered,
        frame,
        Reader((_current(discovered, object_entity_id="entity:changed"),)).current_claim_projection,
    )
    inactive = evaluator.revalidate(
        discovered,
        frame,
        Reader(
            (
                _current(
                    discovered,
                    invalidated_at="2026-08-16T11:00:00Z",
                    invalidated_at_available=True,
                ),
            )
        ).current_claim_projection,
    )
    wrong_projection = evaluator.revalidate(discovered, frame, Reader((discovered,)).current_claim_projection)

    assert missing["reason"] == ClaimEligibilityReason.REVALIDATION_MISSING
    assert changed["reason"] == ClaimEligibilityReason.REVALIDATION_IDENTITY_CONFLICT
    assert changed["revalidated"] is True
    assert inactive["reason"] == ClaimEligibilityReason.CLAIM_INACTIVE
    assert inactive["revalidated"] is True
    assert wrong_projection["reason"] == ClaimEligibilityReason.REVALIDATION_IDENTITY_CONFLICT
    assert wrong_projection["revalidated"] is True


def test_eligible_revalidation_uses_current_trust_and_builds_validity_inputs() -> None:
    discovered = _projection()
    current = _current(
        discovered,
        supplied_trust=0.0,
        supplied_trust_available=True,
        supplied_trust_version=4,
        supplied_trust_version_available=True,
    )

    def current_claim_projection(claim_id: str) -> tuple[ClaimProjection, ...]:
        del claim_id
        result = (current,)
        return result

    frame = _frame()
    evaluator = ClaimEligibilityEvaluator()
    decision = evaluator.revalidate(discovered, frame, current_claim_projection)
    validity = claim_validity_inputs_from_eligibility(decision, frame)

    assert decision["eligible"] is True
    assert decision["revalidated"] is True
    assert decision["projection"]["supplied_trust"] == 0.0
    assert decision["projection"]["supplied_trust_available"] is True
    assert decision["projection"]["supplied_trust_version"] == 4
    assert validity["evaluation_time"] == EVALUATION_TIME
    assert validity["active"] is validity["system_current"] is validity["valid_time_current"] is True


def test_claim_record_construction_requires_allowed_matching_discovery_provenance() -> None:
    discovered = _projection()
    frame = _frame()

    def current_claim_projection(claim_id: str) -> tuple[ClaimProjection, ...]:
        del claim_id
        result = (_current(discovered),)
        return result

    decision = ClaimEligibilityEvaluator().revalidate(discovered, frame, current_claim_projection)

    with pytest.raises(InvalidRequestError, match="allowed producer"):
        claim_evidence_record(discovered, decision, frame, "lexical")
    with pytest.raises(InvalidRequestError, match="vector discovery"):
        claim_evidence_record(discovered, decision, frame, "support_semantic")


def test_batch_revalidation_is_bounded_and_cooperative() -> None:
    discovered = _projection()
    current = _current(discovered)
    checks = []

    def current_claim_projection(claim_id: str) -> tuple[ClaimProjection, ...]:
        del claim_id
        result = (current,)
        return result

    decisions = revalidate_claims(
        (discovered,),
        _frame(),
        ClaimEligibilityEvaluator(),
        current_claim_projection,
        cooperative_check=lambda: checks.append("checked"),
    )

    assert len(decisions) == 1 and decisions[0]["eligible"]
    assert checks == ["checked", "checked"]
    with pytest.raises(InvalidRequestError, match="at most 1000"):
        revalidate_claims((discovered,) * 1_001, _frame(), ClaimEligibilityEvaluator(), current_claim_projection)


def test_validity_inputs_require_publication_revalidation() -> None:
    evaluator = ClaimEligibilityEvaluator()
    initial = evaluator.evaluate(_projection(), _frame())

    with pytest.raises(InvalidRequestError, match="revalidated"):
        claim_validity_inputs_from_eligibility(initial, _frame())
