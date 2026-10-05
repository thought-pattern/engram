"""Current-time disclosure eligibility for response-less Proposition evidence."""

from concurrent.futures import CancelledError
from datetime import datetime
from logging import getLogger as logging_getLogger
from math import isfinite as math_isfinite

from engram.constants import (
    CANONICAL_COMPLETENESS_FLOOR,
    EMPTY_SCOPE_KEY,
    EVIDENCE_USEFULNESS_POLICY_FIELDS,
    MAX_PROPOSITION_IDENTIFIER_BYTES,
    MAX_VISIBILITY_GRANTS,
    PROPOSITION_ELIGIBILITY_DECISION_FIELDS,
    PROPOSITION_EVIDENCE_PRODUCERS,
    PROPOSITION_SEMANTIC_PROJECTION_FIELDS,
    SEMANTIC_SIMILARITY_FLOOR,
    SOURCE_AGREEMENT_FLOOR,
    STRUCTURED_MATCH_FLOOR,
    UNCONSTRAINED_ASSERTION_BASIS,
    VISIBILITY_AUTHORIZATION_FIELDS,
    VISIBILITY_GRANT_FIELDS,
    EvidenceUsefulnessReason,
    PropositionEligibilityReason,
    TemporalAxis,
    TemporalQueryOperator,
)
from engram.errors import IdentityValidationError, InvalidRequestError, ResolutionCancelledError
from engram.graph import PropositionProjectionQuery, validate_proposition_projection
from engram.identity import scope_key_signature, validate_scope_key
from engram.resolution import (
    MAX_PROPOSITION_SELECTION_REASONS,
    MAX_PROPOSITION_SOURCE_CONTRIBUTIONS,
    MAX_RESOLUTION_VALUES,
    DisclosureBasis,
    PropositionOwnership,
    canonical_proposition_references,
    disclosure_decision,
    feature_set,
    proposition_evidence_record as build_proposition_evidence_record,
    proposition_evidence_record_with_changes,
    proposition_trust_inputs,
    proposition_validity_inputs,
    validate_disclosure_decision,
    validate_proposition_evidence_record,
    validate_query_frame,
)
from engram.validation import require_identifier, require_utc_datetime

logger = logging_getLogger(__name__)
# The concrete unavailable disclosure decision. Read-only: eligibility
# validation copies it and otherwise only compares against it.
EMPTY_DISCLOSURE_DECISION = disclosure_decision(PropositionOwnership.PUBLIC, DisclosureBasis.PUBLIC_RULE, EMPTY_SCOPE_KEY)


def no_cooperative_check() -> bool:
    return False


def exact_mapping(value: object, name: str, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise InvalidRequestError(f"{name} has invalid fields")
    result = value
    return result


def evidence_usefulness_decision(
    proposition_id: object,
    included: object,
    reasons: object,
) -> dict:
    """Validate one content-free evidence inclusion decision."""
    normalized_proposition_id = require_identifier(
        proposition_id, "evidence usefulness proposition_id", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES
    )
    if not isinstance(included, bool):
        raise InvalidRequestError("evidence usefulness included must be a boolean")
    if not isinstance(reasons, tuple) or not reasons:
        raise InvalidRequestError("evidence usefulness reasons must be a non-empty tuple")
    if not all(isinstance(reason, EvidenceUsefulnessReason) for reason in reasons):
        raise InvalidRequestError("evidence usefulness reasons are invalid")
    normalized_reasons = tuple(reasons)
    if normalized_reasons != tuple(sorted(set(normalized_reasons), key=lambda reason: reason.value)):
        raise InvalidRequestError("evidence usefulness reasons must be unique and sorted")
    exclusion_reasons = {
        EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_UNAVAILABLE,
        EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_BELOW_FLOOR,
        EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_UNAVAILABLE,
        EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR,
        EvidenceUsefulnessReason.SUPPLIED_TRUST_REQUIRED_UNAVAILABLE,
        EvidenceUsefulnessReason.SUPPLIED_TRUST_BELOW_FLOOR,
    }
    qualifying_reasons = {
        EvidenceUsefulnessReason.STRUCTURED_MATCH_QUALIFIED,
        EvidenceUsefulnessReason.SEMANTIC_SIMILARITY_QUALIFIED,
    }
    has_exclusion = bool(exclusion_reasons.intersection(normalized_reasons))
    has_qualifying_signal = bool(qualifying_reasons.intersection(normalized_reasons))
    if included and (has_exclusion or not has_qualifying_signal):
        raise InvalidRequestError("included evidence usefulness decision has inconsistent reasons")
    if not included and not has_exclusion:
        raise InvalidRequestError("excluded evidence usefulness decision requires an exclusion reason")
    result: dict = {
        "proposition_id": normalized_proposition_id,
        "included": included,
        "reasons": normalized_reasons,
    }
    return result


def evidence_usefulness_policy(
    canonical_completeness_floor: object = CANONICAL_COMPLETENESS_FLOOR,
    structured_match_floor: object = STRUCTURED_MATCH_FLOOR,
    semantic_similarity_floor: object = SEMANTIC_SIMILARITY_FLOOR,
    source_agreement_floor: object = SOURCE_AGREEMENT_FLOOR,
    supplied_trust_floor: object = 0.0,
    supplied_trust_floor_available: object = False,
) -> dict:
    """Validate the frozen hand-authored evidence policy used by release qualification."""
    frozen = {
        "canonical_completeness_floor": (canonical_completeness_floor, CANONICAL_COMPLETENESS_FLOOR),
        "structured_match_floor": (structured_match_floor, STRUCTURED_MATCH_FLOOR),
        "semantic_similarity_floor": (semantic_similarity_floor, SEMANTIC_SIMILARITY_FLOOR),
        "source_agreement_floor": (source_agreement_floor, SOURCE_AGREEMENT_FLOOR),
    }
    normalized_floors: dict[str, float] = {}
    for name, pair in frozen.items():
        value, expected = pair
        if not isinstance(value, float):
            raise InvalidRequestError(f"evidence usefulness {name} must be a float")
        if not math_isfinite(value) or value != expected:
            raise InvalidRequestError(f"evidence usefulness {name} is frozen at {expected}")
        normalized_floors[name] = value
    if not isinstance(supplied_trust_floor_available, bool):
        raise InvalidRequestError("evidence usefulness supplied_trust_floor_available must be a boolean")
    if (
        not isinstance(supplied_trust_floor, float)
        or not math_isfinite(supplied_trust_floor)
        or not 0.0 <= supplied_trust_floor <= 1.0
    ):
        raise InvalidRequestError("evidence usefulness supplied_trust_floor must be a finite float in [0, 1]")
    if not supplied_trust_floor_available and supplied_trust_floor != 0.0:
        raise InvalidRequestError("unavailable evidence usefulness supplied_trust_floor must be zero")
    result: dict = {
        "canonical_completeness_floor": normalized_floors.get("canonical_completeness_floor", 0.0),
        "structured_match_floor": normalized_floors.get("structured_match_floor", 0.0),
        "semantic_similarity_floor": normalized_floors.get("semantic_similarity_floor", 0.0),
        "source_agreement_floor": normalized_floors.get("source_agreement_floor", 0.0),
        "supplied_trust_floor": supplied_trust_floor,
        "supplied_trust_floor_available": supplied_trust_floor_available,
    }
    return result


def validate_evidence_usefulness_policy(value: object) -> dict:
    data = exact_mapping(value, "EvidenceUsefulnessPolicy", EVIDENCE_USEFULNESS_POLICY_FIELDS)
    result = evidence_usefulness_policy(
        data.get("canonical_completeness_floor", 0.0),
        data.get("structured_match_floor", 0.0),
        data.get("semantic_similarity_floor", 0.0),
        data.get("source_agreement_floor", 0.0),
        data.get("supplied_trust_floor", 0.0),
        data.get("supplied_trust_floor_available", False),
    )
    return result


def evidence_usefulness_policy_to_dict(value: object) -> dict:
    policy = validate_evidence_usefulness_policy(value)
    result: dict = dict(policy)
    return result


def evidence_usefulness_policy_from_dict(value: object) -> dict:
    result = validate_evidence_usefulness_policy(value)
    return result


def evaluate_evidence_usefulness(policy: object, record: object) -> dict:
    """Apply one validated content-free evidence policy to a full Proposition record."""
    validated_policy = validate_evidence_usefulness_policy(policy)
    try:
        validated_record = validate_proposition_evidence_record(record)
    except InvalidRequestError as error:
        raise InvalidRequestError("evidence usefulness requires a PropositionEvidenceRecord") from error
    values = validated_record.get("features", {}).get("values", {})
    trust = validated_record.get("trust", {})
    reasons: set[EvidenceUsefulnessReason] = set()
    exclusions: set[EvidenceUsefulnessReason] = set()

    if "canonical_completeness" not in values:
        exclusions.add(EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_UNAVAILABLE)
    elif values.get("canonical_completeness", 0.0) < validated_policy.get("canonical_completeness_floor", 0.0):
        exclusions.add(EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_BELOW_FLOOR)

    qualifying_signal = False
    signal_available = False
    if "structured_match" in values:
        signal_available = True
        if values.get("structured_match", 0.0) >= validated_policy.get("structured_match_floor", 0.0):
            qualifying_signal = True
            reasons.add(EvidenceUsefulnessReason.STRUCTURED_MATCH_QUALIFIED)
    if "semantic_similarity" in values:
        signal_available = True
        if values.get("semantic_similarity", 0.0) >= validated_policy.get("semantic_similarity_floor", 0.0):
            qualifying_signal = True
            reasons.add(EvidenceUsefulnessReason.SEMANTIC_SIMILARITY_QUALIFIED)
    if not signal_available:
        exclusions.add(EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_UNAVAILABLE)
    elif not qualifying_signal:
        exclusions.add(EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR)

    if "source_agreement" in values and values.get("source_agreement", 0.0) >= validated_policy.get("source_agreement_floor", 0.0):
        reasons.add(EvidenceUsefulnessReason.SOURCE_AGREEMENT_QUALIFIED)

    trust_floor_available = validated_policy.get("supplied_trust_floor_available", False)
    if not trust.get("supplied_trust_available", False):
        reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_UNAVAILABLE)
        if trust_floor_available:
            exclusions.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_REQUIRED_UNAVAILABLE)
    else:
        reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_AVAILABLE)
        if trust_floor_available:
            if trust.get("supplied_trust", 0.0) < validated_policy.get("supplied_trust_floor", 0.0):
                exclusions.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_BELOW_FLOOR)
            else:
                reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_FLOOR_SATISFIED)

    reasons.update(exclusions)
    result = evidence_usefulness_decision(
        validated_record.get("proposition_id", ""),
        not exclusions,
        tuple(sorted(reasons, key=lambda reason: reason.value)),
    )
    return result


def visibility_authorization(
    allowed: object,
    scope: object,
    ownership: object,
    authority_id: object,
    reason_code: object,
) -> dict:
    """Validate one trusted exact-scope decision for a non-public ownership category."""
    if not isinstance(allowed, bool):
        raise InvalidRequestError("visibility authorization allowed must be a boolean")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("visibility authorization scope must be a ScopeKey") from error
    if not isinstance(ownership, PropositionOwnership) or ownership not in {
        PropositionOwnership.COMPANY,
        PropositionOwnership.CUSTOMER,
    }:
        raise InvalidRequestError("visibility authorization ownership must be COMPANY or CUSTOMER")
    normalized_authority = require_identifier(
        authority_id, "visibility authorization authority_id", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES
    )
    normalized_reason = require_identifier(reason_code, "visibility authorization reason_code", 96)
    result: dict = {
        "allowed": allowed,
        "scope": validated_scope,
        "ownership": ownership,
        "authority_id": normalized_authority,
        "reason_code": normalized_reason,
    }
    return result


def validate_visibility_authorization(value: object) -> dict:
    data = exact_mapping(value, "VisibilityAuthorization", VISIBILITY_AUTHORIZATION_FIELDS)
    result = visibility_authorization(
        data.get("allowed", False),
        data.get("scope", {}),
        data.get("ownership", PropositionOwnership.PUBLIC),
        data.get("authority_id", ""),
        data.get("reason_code", ""),
    )
    return result


def visibility_grant(scope: object, ownership: object) -> dict:
    """Validate one exact typed scope/ownership authorization."""
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("visibility grant scope must be a ScopeKey") from error
    if not isinstance(ownership, PropositionOwnership) or ownership not in {
        PropositionOwnership.COMPANY,
        PropositionOwnership.CUSTOMER,
    }:
        raise InvalidRequestError("visibility grant ownership must be COMPANY or CUSTOMER")
    result: dict = {"scope": validated_scope, "ownership": ownership}
    return result


def validate_visibility_grant(value: object) -> dict:
    data = exact_mapping(value, "VisibilityGrant", VISIBILITY_GRANT_FIELDS)
    result = visibility_grant(data.get("scope", {}), data.get("ownership", PropositionOwnership.PUBLIC))
    return result


class ExactScopeVisibilityAuthority:
    """Configured allow-list that compares complete ScopeKey values by equality."""

    def __init__(self, authority_id: str, grants: tuple[dict, ...]) -> None:
        self.authority_id = require_identifier(
            authority_id, "visibility authority_id", maximum_bytes=MAX_PROPOSITION_IDENTIFIER_BYTES
        )
        if not isinstance(grants, tuple):
            raise InvalidRequestError("visibility grants must be a tuple of VisibilityGrant values")
        if len(grants) > MAX_VISIBILITY_GRANTS:
            raise InvalidRequestError(f"visibility grants exceeds the limit of {MAX_VISIBILITY_GRANTS}")
        try:
            validated_grants = tuple(validate_visibility_grant(grant) for grant in grants)
        except InvalidRequestError as error:
            raise InvalidRequestError("visibility grants must be a tuple of VisibilityGrant values") from error
        keys = tuple(
            (scope_key_signature(grant.get("scope", {})), grant.get("ownership", PropositionOwnership.PUBLIC))
            for grant in validated_grants
        )
        if len(keys) != len(set(keys)):
            raise InvalidRequestError("visibility grants must be unique")
        self.internal_grants = set(keys)

    def evaluate(self, scope: dict, ownership: PropositionOwnership) -> dict:
        try:
            scope = validate_scope_key(scope)
        except IdentityValidationError as error:
            raise InvalidRequestError("visibility evaluation scope must be a ScopeKey") from error
        if ownership not in {PropositionOwnership.COMPANY, PropositionOwnership.CUSTOMER}:
            raise InvalidRequestError("visibility evaluation ownership must be COMPANY or CUSTOMER")
        allowed = (scope_key_signature(scope), ownership) in self.internal_grants
        reason_code = "exact_scope_granted" if allowed else "exact_scope_denied"
        result = visibility_authorization(
            allowed,
            scope,
            ownership,
            self.authority_id,
            reason_code,
        )
        return result


def proposition_eligibility_decision(
    projection: object,
    eligible: object,
    reason: object,
    disclosure: object = EMPTY_DISCLOSURE_DECISION,
    disclosure_available: object = False,
    revalidated: object = False,
) -> dict:
    """Validate one fail-closed Proposition projection eligibility decision."""
    validated_projection = validate_proposition_projection(projection)
    if not isinstance(eligible, bool):
        raise InvalidRequestError("Proposition eligibility eligible must be a boolean")
    if not isinstance(reason, PropositionEligibilityReason):
        raise InvalidRequestError("Proposition eligibility reason must be a PropositionEligibilityReason")
    try:
        validated_disclosure = validate_disclosure_decision(disclosure)
    except InvalidRequestError as error:
        raise InvalidRequestError("Proposition eligibility disclosure must be a DisclosureDecision") from error
    if not isinstance(disclosure_available, bool):
        raise InvalidRequestError("Proposition eligibility disclosure_available must be a boolean")
    if not isinstance(revalidated, bool):
        raise InvalidRequestError("Proposition eligibility revalidated must be a boolean")
    if eligible != disclosure_available:
        raise InvalidRequestError("eligible Proposition decisions require an available disclosure decision")
    if not disclosure_available and validated_disclosure != EMPTY_DISCLOSURE_DECISION:
        raise InvalidRequestError("unavailable Proposition disclosure must use the concrete empty decision")
    eligible_reasons = {
        PropositionEligibilityReason.ELIGIBLE_PUBLIC,
        PropositionEligibilityReason.ELIGIBLE_TRUSTED_SCOPE,
    }
    if eligible != (reason in eligible_reasons):
        raise InvalidRequestError("Proposition eligibility reason conflicts with eligible state")
    result: dict = {
        "projection": validated_projection,
        "eligible": eligible,
        "reason": reason,
        "disclosure": validated_disclosure,
        "disclosure_available": disclosure_available,
        "revalidated": revalidated,
    }
    return result


def validate_proposition_eligibility_decision(value: object) -> dict:
    data = exact_mapping(value, "PropositionEligibilityDecision", PROPOSITION_ELIGIBILITY_DECISION_FIELDS)
    result = proposition_eligibility_decision(
        data.get("projection", {}),
        data.get("eligible", False),
        data.get("reason", PropositionEligibilityReason.REVALIDATION_UNAVAILABLE),
        data.get("disclosure", {}),
        data.get("disclosure_available", False),
        data.get("revalidated", False),
    )
    return result


def proposition_eligibility_decision_with_changes(value: object, changes: object) -> dict:
    decision = validate_proposition_eligibility_decision(value)
    if not isinstance(changes, dict):
        raise InvalidRequestError("Proposition eligibility decision changes must be an object")
    if not set(changes).issubset(PROPOSITION_ELIGIBILITY_DECISION_FIELDS):
        raise InvalidRequestError("Proposition eligibility decision changes contain an unknown field")
    updated: dict = dict(decision)
    updated.update(changes)
    result = validate_proposition_eligibility_decision(updated)
    return result


def proposition_validity_inputs_from_eligibility(
    decision: dict,
    frame: dict,
    *,
    trusted: bool = False,
) -> dict:
    """Derive validity inputs from an eligible publication-time decision.

    ``trusted`` skips revalidating a decision the evaluator just produced.
    """
    decision = decision if trusted else validate_proposition_eligibility_decision(decision)
    if (
        not decision.get("eligible", False)
        or not decision.get("revalidated", False)
        or not decision.get("disclosure_available", False)
    ):
        raise InvalidRequestError("Proposition validity inputs require an eligible revalidated decision")
    projection = decision.get("projection", {})
    temporal = frame.get("temporal_query", {})
    # The frame is the caller's validated QueryFrame; a missing temporal field
    # must fail rather than silently select an unconstrained request.
    if not {"operator", "axis", "start", "start_available", "end", "end_available"}.issubset(temporal):
        raise InvalidRequestError("Proposition validity inputs require a complete temporal query")
    evaluation_timestamp = frame.get("eligibility_context", {}).get("evaluation_time", "")
    evaluation_time = require_utc_datetime(evaluation_timestamp, name="Proposition eligibility time")
    system_from = projection.get("system_from", "")
    system_from_available = projection.get("system_from_available", False)
    system_to = projection.get("system_to", "")
    system_to_available = projection.get("system_to_available", False)
    valid_from = projection.get("valid_from", "")
    valid_from_available = projection.get("valid_from_available", False)
    valid_to = projection.get("valid_to", "")
    valid_to_available = projection.get("valid_to_available", False)
    invalidated_at = projection.get("invalidated_at", "")
    invalidated_at_available = projection.get("invalidated_at_available", False)
    temporal_axis = temporal.get("axis", TemporalAxis.VALID_TIME)
    effective_system_to = system_to
    effective_system_to_available = system_to_available
    if invalidated_at_available and (
        not effective_system_to_available
        or require_utc_datetime(invalidated_at, name="Proposition eligibility time")
        < require_utc_datetime(effective_system_to, name="Proposition eligibility time")
    ):
        effective_system_to = invalidated_at
        effective_system_to_available = True
    result = proposition_validity_inputs(
        evaluation_timestamp,
        not invalidated_at_available,
        interval_contains(evaluation_time, system_from, system_from_available, effective_system_to, effective_system_to_available),
        interval_contains(evaluation_time, valid_from, valid_from_available, valid_to, valid_to_available),
        valid_from=valid_from,
        valid_from_available=valid_from_available,
        valid_to=valid_to,
        valid_to_available=valid_to_available,
        temporal_operator=temporal.get("operator", TemporalQueryOperator.UNSPECIFIED),
        temporal_axis=temporal_axis,
        requested_start=temporal.get("start", ""),
        requested_start_available=temporal.get("start_available", False),
        requested_end=temporal.get("end", ""),
        requested_end_available=temporal.get("end_available", False),
        system_from=system_from,
        system_from_available=system_from_available,
        system_to=system_to,
        system_to_available=system_to_available,
        invalidated_at=invalidated_at,
        invalidated_at_available=invalidated_at_available,
        eligible_for_request=True,
        system_time_match=True,
        valid_time_match=temporal_axis == TemporalAxis.VALID_TIME,
        valid_time_match_available=temporal_axis == TemporalAxis.VALID_TIME,
    )
    return result


def interval_contains(
    point: datetime,
    lower: str,
    lower_available: bool,
    upper: str,
    upper_available: bool,
) -> bool:
    """Return half-open point containment for an interval with concrete open bounds."""
    after_lower = not lower_available or point >= require_utc_datetime(lower, name="Proposition eligibility time")
    before_upper = not upper_available or point < require_utc_datetime(upper, name="Proposition eligibility time")
    result = after_lower and before_upper
    return result


def interval_overlaps(
    requested_start: str,
    requested_start_available: bool,
    requested_end: str,
    requested_end_available: bool,
    lower: str,
    lower_available: bool,
    upper: str,
    upper_available: bool,
) -> bool:
    """Return half-open overlap without inventing values for open bounds."""
    starts_before_request_end = (
        not requested_end_available
        or not lower_available
        or require_utc_datetime(lower, name="Proposition eligibility time")
        < require_utc_datetime(requested_end, name="Proposition eligibility time")
    )
    ends_after_request_start = (
        not requested_start_available
        or not upper_available
        or require_utc_datetime(upper, name="Proposition eligibility time")
        > require_utc_datetime(requested_start, name="Proposition eligibility time")
    )
    result = starts_before_request_end and ends_after_request_start
    return result


def assertion_basis_window(frame: dict) -> dict:
    """Return the Assertion valid-time window this request's temporal view admits.

    Graph projections take each Proposition's trust and validity from its
    lowest-ID active Assertion inside this window, so discovery and by-ID
    revalidation choose the same basis and ``evaluate`` can accept it. The
    window mirrors ``evaluate``: a current request contains the evaluation
    time, ``latest`` needs a basis begun by then, ``as of`` contains the
    requested point, other valid-time requests overlap the requested interval,
    and the system-time axis leaves the basis valid time open.
    """
    context = frame.get("eligibility_context", {})
    temporal = frame.get("temporal_query", {})
    window = dict(UNCONSTRAINED_ASSERTION_BASIS)
    if not context.get("evaluation_time_available", False) or not temporal.get("resolved", False):
        return window
    evaluation_time = context.get("evaluation_time", "")
    operator = temporal.get("operator", TemporalQueryOperator.UNSPECIFIED)
    if operator in {TemporalQueryOperator.UNSPECIFIED, TemporalQueryOperator.CURRENT, TemporalQueryOperator.NOW}:
        point, point_available = evaluation_time, True
    elif temporal.get("axis", TemporalAxis.VALID_TIME) != TemporalAxis.VALID_TIME:
        return window
    elif operator == TemporalQueryOperator.LATEST:
        window.update({"basis_end": evaluation_time, "basis_end_available": True, "basis_end_inclusive": True})
        return window
    elif operator == TemporalQueryOperator.AS_OF:
        point, point_available = temporal.get("start", ""), temporal.get("start_available", False)
    else:
        window.update(
            {
                "basis_start": temporal.get("start", ""),
                "basis_start_available": temporal.get("start_available", False),
                "basis_end": temporal.get("end", ""),
                "basis_end_available": temporal.get("end_available", False),
            }
        )
        return window
    if point_available:
        window.update(
            {
                "basis_start": point,
                "basis_start_available": True,
                "basis_end": point,
                "basis_end_available": True,
                "basis_end_inclusive": True,
            }
        )
    return window


def requested_interval_match(
    operator: TemporalQueryOperator,
    requested_start: str,
    requested_start_available: bool,
    requested_end: str,
    requested_end_available: bool,
    lower: str,
    lower_available: bool,
    upper: str,
    upper_available: bool,
) -> bool:
    if operator == TemporalQueryOperator.AS_OF:
        result = interval_contains(
            require_utc_datetime(requested_start, name="Proposition eligibility time"),
            lower,
            lower_available,
            upper,
            upper_available,
        )
        return result
    result = interval_overlaps(
        requested_start,
        requested_start_available,
        requested_end,
        requested_end_available,
        lower,
        lower_available,
        upper,
        upper_available,
    )
    return result


def outside_interval_reason(
    requested_start: str,
    requested_start_available: bool,
    requested_end: str,
    requested_end_available: bool,
    lower: str,
    lower_available: bool,
    not_yet_reason: PropositionEligibilityReason,
    no_longer_reason: PropositionEligibilityReason,
) -> PropositionEligibilityReason:
    if (
        requested_end_available
        and lower_available
        and require_utc_datetime(lower, name="Proposition eligibility time")
        >= require_utc_datetime(requested_end, name="Proposition eligibility time")
    ):
        result = not_yet_reason
        return result
    if (
        requested_start_available
        and not requested_end_available
        and lower_available
        and require_utc_datetime(lower, name="Proposition eligibility time")
        > require_utc_datetime(requested_start, name="Proposition eligibility time")
    ):
        result = not_yet_reason
        return result
    result = no_longer_reason
    return result


def semantic_projection_values(projection: dict) -> dict:
    """Select the semantic fields two projections of one Proposition must share.

    A field absent from one projection and present in the other compares as a
    conflict; absent from both, as agreement.
    """
    result = {field: value for field, value in projection.items() if field in PROPOSITION_SEMANTIC_PROJECTION_FIELDS}
    return result


class PropositionEligibilityEvaluator:
    """Shared temporal and disclosure policy over strict Proposition projections."""

    def __init__(self, visibility_authority: object = ()) -> None:
        if type(visibility_authority) is tuple and not visibility_authority:
            selected_authority = ()
        elif callable(getattr(visibility_authority, "evaluate", ())):
            selected_authority = visibility_authority
        else:
            raise InvalidRequestError("visibility authority must implement evaluate")
        self.internal_visibility_authority = selected_authority

    def evaluate(self, projection: dict, frame: dict, *, trusted_frame: bool = False) -> dict:
        """Decide whether one projection may be used for this request.

        ``trusted_frame`` skips revalidating a frame the caller validated once
        for all of its projections.
        """
        projection = validate_proposition_projection(projection)
        frame = frame if trusted_frame else validate_query_frame(frame)
        context = frame.get("eligibility_context", {})
        if not context.get("evaluation_time_available", False):
            result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.EVALUATION_TIME_UNAVAILABLE)
            return result
        evaluation_time = require_utc_datetime(context.get("evaluation_time", ""), name="Proposition eligibility time")
        temporal = frame.get("temporal_query", {})
        if not temporal.get("resolved", False):
            result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.TEMPORAL_QUERY_UNRESOLVED)
            return result
        if not projection.get("system_from_available", False):
            result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.SYSTEM_TIME_UNAVAILABLE)
            return result
        operator = temporal.get("operator", TemporalQueryOperator.UNSPECIFIED)
        requested_start = temporal.get("start", "")
        requested_start_available = temporal.get("start_available", False)
        requested_end = temporal.get("end", "")
        requested_end_available = temporal.get("end_available", False)
        current_operator = operator in {
            TemporalQueryOperator.UNSPECIFIED,
            TemporalQueryOperator.CURRENT,
            TemporalQueryOperator.NOW,
        }
        if current_operator:
            if projection.get("invalidated_at_available", False):
                result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.PROPOSITION_INACTIVE)
                return result
            if evaluation_time < require_utc_datetime(projection.get("system_from", ""), name="Proposition eligibility time"):
                result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.SYSTEM_NOT_YET_CURRENT)
                return result
            if projection.get("system_to_available", False) and evaluation_time >= require_utc_datetime(
                projection.get("system_to", ""), name="Proposition eligibility time"
            ):
                result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.SYSTEM_NO_LONGER_CURRENT)
                return result
            if projection.get("valid_from_available", False) and evaluation_time < require_utc_datetime(
                projection.get("valid_from", ""), name="Proposition eligibility time"
            ):
                reason = PropositionEligibilityReason.VALID_TIME_NOT_YET_CURRENT
                result = proposition_eligibility_decision(projection, False, reason)
                return result
            if projection.get("valid_to_available", False) and evaluation_time >= require_utc_datetime(
                projection.get("valid_to", ""), name="Proposition eligibility time"
            ):
                reason = PropositionEligibilityReason.VALID_TIME_NO_LONGER_CURRENT
                result = proposition_eligibility_decision(projection, False, reason)
                return result
        elif temporal.get("axis", TemporalAxis.VALID_TIME) == TemporalAxis.VALID_TIME:
            if projection.get("invalidated_at_available", False):
                result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.PROPOSITION_INACTIVE)
                return result
            if evaluation_time < require_utc_datetime(projection.get("system_from", ""), name="Proposition eligibility time"):
                result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.SYSTEM_NOT_YET_CURRENT)
                return result
            if projection.get("system_to_available", False) and evaluation_time >= require_utc_datetime(
                projection.get("system_to", ""), name="Proposition eligibility time"
            ):
                result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.SYSTEM_NO_LONGER_CURRENT)
                return result
            if (
                operator == TemporalQueryOperator.LATEST
                and projection.get("valid_from_available", False)
                and evaluation_time < require_utc_datetime(projection.get("valid_from", ""), name="Proposition eligibility time")
            ):
                reason = PropositionEligibilityReason.VALID_TIME_NOT_YET_CURRENT
                result = proposition_eligibility_decision(projection, False, reason)
                return result
            if operator != TemporalQueryOperator.LATEST and not requested_interval_match(
                operator,
                requested_start,
                requested_start_available,
                requested_end,
                requested_end_available,
                projection.get("valid_from", ""),
                projection.get("valid_from_available", False),
                projection.get("valid_to", ""),
                projection.get("valid_to_available", False),
            ):
                reason = outside_interval_reason(
                    requested_start,
                    requested_start_available,
                    requested_end,
                    requested_end_available,
                    projection.get("valid_from", ""),
                    projection.get("valid_from_available", False),
                    PropositionEligibilityReason.VALID_TIME_NOT_YET_CURRENT,
                    PropositionEligibilityReason.VALID_TIME_NO_LONGER_CURRENT,
                )
                result = proposition_eligibility_decision(projection, False, reason)
                return result
        else:
            effective_system_to = projection.get("system_to", "")
            effective_system_to_available = projection.get("system_to_available", False)
            if projection.get("invalidated_at_available", False) and (
                not effective_system_to_available
                or require_utc_datetime(projection.get("invalidated_at", ""), name="Proposition eligibility time")
                < require_utc_datetime(effective_system_to, name="Proposition eligibility time")
            ):
                effective_system_to = projection.get("invalidated_at", "")
                effective_system_to_available = True
            if operator == TemporalQueryOperator.LATEST:
                if require_utc_datetime(projection.get("system_from", ""), name="Proposition eligibility time") > evaluation_time:
                    reason = PropositionEligibilityReason.SYSTEM_NOT_YET_CURRENT
                    result = proposition_eligibility_decision(projection, False, reason)
                    return result
            elif not requested_interval_match(
                operator,
                requested_start,
                requested_start_available,
                requested_end,
                requested_end_available,
                projection.get("system_from", ""),
                projection.get("system_from_available", False),
                effective_system_to,
                effective_system_to_available,
            ):
                reason = outside_interval_reason(
                    requested_start,
                    requested_start_available,
                    requested_end,
                    requested_end_available,
                    projection.get("system_from", ""),
                    projection.get("system_from_available", False),
                    PropositionEligibilityReason.SYSTEM_NOT_YET_CURRENT,
                    PropositionEligibilityReason.SYSTEM_NO_LONGER_CURRENT,
                )
                result = proposition_eligibility_decision(projection, False, reason)
                return result
        if not projection.get("predicate_canonical", False) or projection.get("predicate_id", "") == "generic_relation":
            result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.RETRIEVAL_ONLY)
            return result
        if (
            projection.get("polarity", "") != "positive"
            or projection.get("modality_family", "") != "none"
            or projection.get("modality_operator", "") != "none"
            or projection.get("argument_count", 0) != 2
            or projection.get("qualification_count", 0) != 0
            or projection.get("context_count", 0) != 0
            or projection.get("applicability_count", 0) != 0
        ):
            reason = PropositionEligibilityReason.SEMANTIC_MEANING_UNREPRESENTED
            result = proposition_eligibility_decision(projection, False, reason)
            return result

        ownership = PropositionOwnership(projection.get("ownership_category", PropositionOwnership.PUBLIC))
        if ownership == PropositionOwnership.PUBLIC:
            disclosure = disclosure_decision(
                ownership,
                DisclosureBasis.PUBLIC_RULE,
                frame.get("scope", {}),
            )
            result = proposition_eligibility_decision(
                projection,
                True,
                PropositionEligibilityReason.ELIGIBLE_PUBLIC,
                disclosure,
                True,
            )
            return result

        authority_method = getattr(self.internal_visibility_authority, "evaluate", ())
        if not callable(authority_method):
            reason = PropositionEligibilityReason.VISIBILITY_AUTHORITY_UNAVAILABLE
            result = proposition_eligibility_decision(projection, False, reason)
            return result
        try:
            authorization = authority_method(frame.get("scope", {}), ownership)
        except (ResolutionCancelledError, CancelledError):
            # Cancellation interrupts the request; only an authority failure excludes.
            raise
        except Exception as error:
            logger.warning("Visibility authority failed", exc_info=error)
            result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.VISIBILITY_AUTHORITY_FAILED)
            return result
        try:
            authorization = validate_visibility_authorization(authorization)
        except InvalidRequestError as error:
            logger.warning("Visibility authority returned an invalid authorization", exc_info=error)
            result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.VISIBILITY_AUTHORITY_FAILED)
            return result
        if authorization.get("scope", {}) != frame.get("scope", {}):
            result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.VISIBILITY_SCOPE_MISMATCH)
            return result
        if authorization.get("ownership", PropositionOwnership.PUBLIC) != ownership:
            reason = PropositionEligibilityReason.VISIBILITY_OWNERSHIP_MISMATCH
            result = proposition_eligibility_decision(projection, False, reason)
            return result
        if not authorization.get("allowed", False):
            result = proposition_eligibility_decision(projection, False, PropositionEligibilityReason.VISIBILITY_DENIED)
            return result
        disclosure = disclosure_decision(
            ownership,
            DisclosureBasis.TRUSTED_SCOPE_AUTHORITY,
            frame.get("scope", {}),
            authorization.get("authority_id", ""),
            True,
        )
        result = proposition_eligibility_decision(
            projection,
            True,
            PropositionEligibilityReason.ELIGIBLE_TRUSTED_SCOPE,
            disclosure,
            True,
        )
        return result

    def revalidate(
        self,
        discovered: dict,
        frame: dict,
        current_proposition_projection: object,
        *,
        trusted_frame: bool = False,
    ) -> dict:
        if not callable(current_proposition_projection):
            result = proposition_eligibility_decision(discovered, False, PropositionEligibilityReason.REVALIDATION_UNAVAILABLE)
            return result
        discovered = validate_proposition_projection(discovered)
        try:
            current = current_proposition_projection(discovered.get("proposition_id", ""), assertion_basis_window(frame))
        except (ResolutionCancelledError, CancelledError):
            # Cancellation interrupts the request; only a read failure is unavailable evidence.
            raise
        except Exception as error:
            logger.warning("Proposition revalidation read failed", exc_info=error)
            result = proposition_eligibility_decision(discovered, False, PropositionEligibilityReason.REVALIDATION_UNAVAILABLE)
            return result
        if not isinstance(current, tuple) or len(current) != 1:
            result = proposition_eligibility_decision(discovered, False, PropositionEligibilityReason.REVALIDATION_MISSING)
            return result
        try:
            projection = validate_proposition_projection(current[0])
        except InvalidRequestError as error:
            logger.warning("Proposition revalidation returned an invalid projection", exc_info=error)
            result = proposition_eligibility_decision(discovered, False, PropositionEligibilityReason.REVALIDATION_MISSING)
            return result
        conflict = PropositionEligibilityReason.REVALIDATION_IDENTITY_CONFLICT
        # A missing projection_id reads as "" and so never passes as BY_ID.
        if projection.get("projection_id", "") != PropositionProjectionQuery.BY_ID:
            result = proposition_eligibility_decision(projection, False, conflict, revalidated=True)
            return result
        discovered_identity = (
            discovered.get("proposition_id", ""),
            discovered.get("subject_entity_id", ""),
            discovered.get("predicate_id", ""),
            discovered.get("object_entity_id", ""),
        )
        current_identity = (
            projection.get("proposition_id", ""),
            projection.get("subject_entity_id", ""),
            projection.get("predicate_id", ""),
            projection.get("object_entity_id", ""),
        )
        if discovered_identity != current_identity:
            result = proposition_eligibility_decision(projection, False, conflict, revalidated=True)
            return result
        if semantic_projection_values(discovered) != semantic_projection_values(projection):
            result = proposition_eligibility_decision(projection, False, conflict, revalidated=True)
            return result
        decision = self.evaluate(projection, frame, trusted_frame=trusted_frame)
        result = proposition_eligibility_decision_with_changes(decision, {"revalidated": True})
        return result


def proposition_evidence_record(
    discovered: dict,
    decision: dict,
    frame: dict,
    source_resolver: str,
    *,
    trusted: bool = False,
) -> dict:
    """Construct one strict full record only from eligible revalidated state.

    ``trusted`` is for a resolver passing the projection it discovered and the
    decision the evaluator just returned for it; neither is revalidated.
    Every eligibility and identity check below still runs.
    """
    discovered = discovered if trusted else validate_proposition_projection(discovered)
    decision = decision if trusted else validate_proposition_eligibility_decision(decision)
    source = require_identifier(source_resolver, "Proposition evidence source_resolver", 96)
    if source not in PROPOSITION_EVIDENCE_PRODUCERS:
        raise InvalidRequestError("Proposition evidence source_resolver is not an allowed producer")
    if source == "structured_graph" and discovered.get("projection_id", PropositionProjectionQuery.BY_ID) not in {
        PropositionProjectionQuery.STRUCTURED_ENTITY,
        PropositionProjectionQuery.STRUCTURED_KEYWORD,
        PropositionProjectionQuery.RELATION_ONE_HOP,
    }:
        raise InvalidRequestError("structured Proposition evidence requires a structured discovery projection")
    if (
        source == "support_semantic"
        and discovered.get("projection_id", PropositionProjectionQuery.BY_ID) != PropositionProjectionQuery.VECTOR
    ):
        raise InvalidRequestError("semantic Proposition evidence requires a vector discovery projection")
    if (
        not decision.get("eligible", False)
        or not decision.get("revalidated", False)
        or not decision.get("disclosure_available", False)
    ):
        raise InvalidRequestError("Proposition evidence construction requires an eligible revalidated decision")
    current = decision.get("projection", {})
    discovered_identity = (
        discovered.get("proposition_id", ""),
        discovered.get("subject_entity_id", ""),
        discovered.get("predicate_id", ""),
        discovered.get("object_entity_id", ""),
    )
    proposition_id = current.get("proposition_id", "")
    subject_entity_id = current.get("subject_entity_id", "")
    predicate_id = current.get("predicate_id", "")
    object_entity_id = current.get("object_entity_id", "")
    supplied_trust = current.get("supplied_trust", 0.0)
    supplied_trust_available = current.get("supplied_trust_available", False)
    current_identity = (proposition_id, subject_entity_id, predicate_id, object_entity_id)
    if discovered_identity != current_identity:
        raise InvalidRequestError("Proposition evidence discovery and current canonical identity conflict")
    if semantic_projection_values(discovered) != semantic_projection_values(current):
        raise InvalidRequestError("Proposition evidence discovery and current semantic projection conflict")
    values = {"canonical_completeness": 1.0}
    unavailable = ["source_agreement"]
    reasons = [
        decision.get(
            "reason",
            PropositionEligibilityReason.REVALIDATION_UNAVAILABLE,
        ).value,
        "canonical_complete",
    ]
    if discovered.get("structured_match_available", False):
        values["structured_match"] = discovered.get("structured_match", 0.0)
        reasons.append("structured_match")
    else:
        unavailable.append("structured_match")
    if discovered.get("semantic_similarity_available", False):
        values["semantic_similarity"] = discovered.get("semantic_similarity", 0.0)
        reasons.append("semantic_similarity")
    else:
        unavailable.append("semantic_similarity")
    if supplied_trust_available:
        values["supplied_trust"] = supplied_trust
        reasons.append("supplied_trust_available")
    else:
        unavailable.append("supplied_trust")
        reasons.append("supplied_trust_unavailable")
    result = build_proposition_evidence_record(
        proposition_id=proposition_id,
        source_resolver=source,
        source_contributions=(source,),
        features=feature_set(values=values, unavailable=tuple(sorted(unavailable))),
        canonical_references=canonical_proposition_references(subject_entity_id, predicate_id, object_entity_id),
        validity=proposition_validity_inputs_from_eligibility(decision, frame, trusted=trusted),
        trust=proposition_trust_inputs(
            current.get("trust_category", ""),
            current.get("trust_category_available", False),
            supplied_trust,
            supplied_trust_available,
        ),
        disclosure=decision.get("disclosure", {}),
        path=(proposition_id,),
        selection_reasons=tuple(sorted(reasons)),
        # Every component above was just built by its validating constructor.
        trusted_components=True,
    )
    return result


def proposition_evidence_merge_order(record: dict) -> tuple:
    """Return the deterministic native merge-order key of one validated record.

    Records merged for one Proposition must agree on identity, validity, trust
    and disclosure, so the key orders by producer and then by the fields that
    may differ, in the order of their serialized names: features, path,
    selection reasons and contributing sources.
    """
    features = record.get("features", {})
    path_key = []
    for step in record.get("path", ()):
        if isinstance(step, str):
            path_key.append((0, step))
        else:
            path_key.append(
                (
                    1,
                    step.get("aggregation_inputs", ()),
                    step.get("filters", ()),
                    step.get("input_binding", ""),
                    step.get("object_entity_id", ""),
                    step.get("operator", ""),
                    step.get("output_binding", ""),
                    step.get("position", 0),
                    step.get("predicate_id", ""),
                    step.get("proposition_id", ""),
                    step.get("subject_entity_id", ""),
                )
            )
    result = (
        record.get("source_resolver", ""),
        tuple(sorted(features.get("unavailable", ()))),
        tuple(sorted(features.get("values", {}).items())),
        tuple(path_key),
        tuple(record.get("selection_reasons", ())),
        tuple(record.get("source_contributions", ())),
    )
    return result


def merge_proposition_evidence_group(records: tuple[dict, ...]) -> dict:
    if not records:
        raise InvalidRequestError("cannot merge an empty Proposition evidence group")
    ordered = tuple(sorted(records, key=proposition_evidence_merge_order))
    base = ordered[0]
    proposition_id = base.get("proposition_id", "")
    # The path records how one producer reached the Proposition: a direct
    # discovery's singleton ID or a composition's typed steps. Alternative
    # paths are provenance, not current state, so the merged record keeps the
    # first record's path in this deterministic order and every contributing
    # source; only identity, validity, trust and disclosure must agree.
    base_state = (base.get("validity", {}), base.get("trust", {}), base.get("disclosure", {}))
    for record in ordered[1:]:
        if record.get("canonical_references", {}) != base.get("canonical_references", {}):
            raise InvalidRequestError(f"conflicting canonical references for Proposition evidence ID: {proposition_id}")
        current_state = (record.get("validity", {}), record.get("trust", {}), record.get("disclosure", {}))
        if current_state != base_state:
            raise InvalidRequestError(f"conflicting current evidence state for Proposition evidence ID: {proposition_id}")

    sources = tuple(sorted({source for record in ordered for source in record.get("source_contributions", ())}))
    if not sources:
        raise InvalidRequestError("merged Proposition evidence sources must not be empty")
    if len(sources) > MAX_PROPOSITION_SOURCE_CONTRIBUTIONS:
        raise InvalidRequestError(f"merged Proposition evidence sources exceed the limit of {MAX_PROPOSITION_SOURCE_CONTRIBUTIONS}")
    primary_source = next(iter(sources))
    values: dict[str, float] = {}
    unavailable = set()
    reasons = set()
    for record in ordered:
        features = record.get("features", {})
        reasons.update(record.get("selection_reasons", ()))
        unavailable.update(features.get("unavailable", ()))
        for name, value in features.get("values", {}).items():
            if name in values and values.get(name, 0.0) != value:
                raise InvalidRequestError(f"conflicting measured feature {name} for Proposition evidence ID: {proposition_id}")
            values[name] = value
    if len(sources) > 1:
        if "source_agreement" in values and values.get("source_agreement", 0.0) != 1.0:
            raise InvalidRequestError(
                f"conflicting measured feature source_agreement for Proposition evidence ID: {proposition_id}"
            )
        values["source_agreement"] = 1.0
        reasons.add("source_agreement")
    elif "source_agreement" in values:
        raise InvalidRequestError(f"source_agreement requires multiple sources for Proposition evidence ID: {proposition_id}")
    unavailable.difference_update(values)
    if len(reasons) > MAX_PROPOSITION_SELECTION_REASONS:
        raise InvalidRequestError(f"merged Proposition evidence reasons exceed the limit of {MAX_PROPOSITION_SELECTION_REASONS}")
    result = proposition_evidence_record_with_changes(
        base,
        {
            "source_resolver": primary_source,
            "source_contributions": sources,
            "features": feature_set(values=values, unavailable=tuple(sorted(unavailable))),
            "selection_reasons": tuple(sorted(reasons)),
        },
    )
    return result


def canonicalize_proposition_evidence(
    records: tuple[dict, ...],
    cooperative_check: object = no_cooperative_check,
) -> tuple[dict, ...]:
    """Deterministically deduplicate and merge strict records by stable Proposition ID."""
    if not isinstance(records, tuple) or len(records) > MAX_RESOLUTION_VALUES:
        raise InvalidRequestError(f"Proposition evidence normalization requires a tuple of at most {MAX_RESOLUTION_VALUES} records")
    try:
        validated_records = tuple(validate_proposition_evidence_record(record) for record in records)
    except InvalidRequestError as error:
        raise InvalidRequestError("Proposition evidence normalization requires PropositionEvidenceRecord values") from error
    if not callable(cooperative_check):
        raise InvalidRequestError("Proposition evidence normalization cooperative_check must be callable")
    grouped: dict[str, list[dict]] = {}
    for record in validated_records:
        cooperative_check()
        grouped.setdefault(record.get("proposition_id", ""), []).append(record)
    merged = []
    for proposition_id in sorted(grouped):
        cooperative_check()
        merged.append(merge_proposition_evidence_group(tuple(grouped.get(proposition_id, []))))
    cooperative_check()
    result = tuple(merged)
    return result
