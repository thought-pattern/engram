"""Current-time disclosure eligibility for response-less Claim evidence."""

import json
import math
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TypedDict

from engram.constants import (
    CANONICAL_COMPLETENESS_FLOOR_V1,
    CLAIM_DISCLOSURE_POLICY_VERSION,
    CLAIM_ELIGIBILITY_DECISION_FIELDS,
    CLAIM_EVIDENCE_PRODUCERS,
    CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION,
    EMPTY_SCOPE_KEY,
    EVIDENCE_USEFULNESS_DECISION_FIELDS,
    EVIDENCE_USEFULNESS_POLICY_FIELDS,
    MAX_VISIBILITY_GRANTS,
    SEMANTIC_SIMILARITY_FLOOR_V1,
    SOURCE_AGREEMENT_FLOOR_V1,
    STRUCTURED_MATCH_FLOOR_V1,
    VISIBILITY_AUTHORIZATION_FIELDS,
    VISIBILITY_GRANT_FIELDS,
    ClaimEligibilityReason,
    EvidenceUsefulnessReason,
    TemporalAxis,
    TemporalQueryOperator,
)
from engram.errors import IdentityValidationError, InvalidRequestError
from engram.graph import ClaimProjection, ClaimProjectionQuery, validate_claim_projection
from engram.identity import ScopeKey, scope_key_signature, validate_scope_key
from engram.resolution import (
    MAX_CLAIM_SELECTION_REASONS,
    MAX_CLAIM_SOURCE_CONTRIBUTIONS,
    MAX_RESOLUTION_VALUES,
    ClaimEvidenceRecord,
    ClaimOwnership,
    ClaimValidityInputs,
    DisclosureBasis,
    DisclosureDecision,
    QueryFrame,
    canonical_claim_references,
    claim_evidence_record as build_claim_evidence_record,
    claim_evidence_record_to_json,
    claim_evidence_record_with_changes,
    claim_trust_inputs,
    claim_validity_inputs,
    disclosure_decision,
    feature_set,
    validate_claim_evidence_record,
    validate_disclosure_decision,
    validate_query_frame,
)


def empty_disclosure_decision() -> DisclosureDecision:
    """Return the concrete unavailable disclosure-decision value."""
    result = disclosure_decision(
        ClaimOwnership.PUBLIC,
        DisclosureBasis.PUBLIC_RULE,
        EMPTY_SCOPE_KEY,
        "unavailable",
    )
    return result


def _no_cooperative_check() -> None:
    return


def _exact_mapping(value: object, name: str, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        raise InvalidRequestError(f"{name} has invalid fields")
    result = value
    return result


EvidenceUsefulnessDecision = TypedDict(
    "EvidenceUsefulnessDecision",
    {"claim_id": str, "included": bool, "reasons": tuple[EvidenceUsefulnessReason, ...], "policy_version": str},
)


def evidence_usefulness_decision(
    claim_id: object,
    included: object,
    reasons: object,
    policy_version: object = CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION,
) -> EvidenceUsefulnessDecision:
    """Build one content-free evidence inclusion decision."""
    normalized_claim_id = _token(claim_id, "evidence usefulness claim_id")
    if not isinstance(included, bool):
        raise InvalidRequestError("evidence usefulness included must be a boolean")
    if policy_version != CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION:
        raise InvalidRequestError(f"unsupported evidence usefulness policy_version: {policy_version}")
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
    result: EvidenceUsefulnessDecision = {
        "policy_version": CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION,
        "claim_id": normalized_claim_id,
        "included": included,
        "reasons": normalized_reasons,
    }
    return result


def validate_evidence_usefulness_decision(value: object) -> EvidenceUsefulnessDecision:
    data = _exact_mapping(value, "EvidenceUsefulnessDecision", EVIDENCE_USEFULNESS_DECISION_FIELDS)
    result = evidence_usefulness_decision(data["claim_id"], data["included"], data["reasons"], data["policy_version"])
    return result


def evidence_usefulness_decision_to_dict(value: object) -> dict[str, object]:
    decision = validate_evidence_usefulness_decision(value)
    result = {
        "policy_version": decision["policy_version"],
        "claim_id": decision["claim_id"],
        "included": decision["included"],
        "reasons": [reason.value for reason in decision["reasons"]],
    }
    return result


EvidenceUsefulnessPolicy = TypedDict(
    "EvidenceUsefulnessPolicy",
    {
        "policy_version": str,
        "canonical_completeness_floor": float,
        "structured_match_floor": float,
        "semantic_similarity_floor": float,
        "source_agreement_floor": float,
        "supplied_trust_floor": float,
        "supplied_trust_floor_available": bool,
    },
)


def evidence_usefulness_policy(
    policy_version: object = CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION,
    canonical_completeness_floor: object = CANONICAL_COMPLETENESS_FLOOR_V1,
    structured_match_floor: object = STRUCTURED_MATCH_FLOOR_V1,
    semantic_similarity_floor: object = SEMANTIC_SIMILARITY_FLOOR_V1,
    source_agreement_floor: object = SOURCE_AGREEMENT_FLOOR_V1,
    supplied_trust_floor: object = 0.0,
    supplied_trust_floor_available: object = False,
) -> EvidenceUsefulnessPolicy:
    """Build the frozen hand-authored evidence policy; Section 16 owns calibration."""
    if not isinstance(policy_version, str):
        raise InvalidRequestError("evidence usefulness policy_version must be a string")
    if policy_version != CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION:
        raise InvalidRequestError(f"unsupported evidence usefulness policy_version: {policy_version}")
    frozen = {
        "canonical_completeness_floor": (canonical_completeness_floor, CANONICAL_COMPLETENESS_FLOOR_V1),
        "structured_match_floor": (structured_match_floor, STRUCTURED_MATCH_FLOOR_V1),
        "semantic_similarity_floor": (semantic_similarity_floor, SEMANTIC_SIMILARITY_FLOOR_V1),
        "source_agreement_floor": (source_agreement_floor, SOURCE_AGREEMENT_FLOOR_V1),
    }
    normalized_floors: dict[str, float] = {}
    for name, pair in frozen.items():
        value, expected = pair
        if not isinstance(value, float):
            raise InvalidRequestError(f"evidence usefulness {name} must be a float")
        if not math.isfinite(value) or value != expected:
            raise InvalidRequestError(f"evidence usefulness {name} is frozen at {expected}")
        normalized_floors[name] = value
    if not isinstance(supplied_trust_floor_available, bool):
        raise InvalidRequestError("evidence usefulness supplied_trust_floor_available must be a boolean")
    if (
        not isinstance(supplied_trust_floor, float)
        or not math.isfinite(supplied_trust_floor)
        or not 0.0 <= supplied_trust_floor <= 1.0
    ):
        raise InvalidRequestError("evidence usefulness supplied_trust_floor must be a finite float in [0, 1]")
    if not supplied_trust_floor_available and supplied_trust_floor != 0.0:
        raise InvalidRequestError("unavailable evidence usefulness supplied_trust_floor must be zero")
    result: EvidenceUsefulnessPolicy = {
        "policy_version": CLAIM_EVIDENCE_USEFULNESS_POLICY_VERSION,
        "canonical_completeness_floor": normalized_floors["canonical_completeness_floor"],
        "structured_match_floor": normalized_floors["structured_match_floor"],
        "semantic_similarity_floor": normalized_floors["semantic_similarity_floor"],
        "source_agreement_floor": normalized_floors["source_agreement_floor"],
        "supplied_trust_floor": supplied_trust_floor,
        "supplied_trust_floor_available": supplied_trust_floor_available,
    }
    return result


def validate_evidence_usefulness_policy(value: object) -> EvidenceUsefulnessPolicy:
    data = _exact_mapping(value, "EvidenceUsefulnessPolicy", EVIDENCE_USEFULNESS_POLICY_FIELDS)
    result = evidence_usefulness_policy(
        data["policy_version"],
        data["canonical_completeness_floor"],
        data["structured_match_floor"],
        data["semantic_similarity_floor"],
        data["source_agreement_floor"],
        data["supplied_trust_floor"],
        data["supplied_trust_floor_available"],
    )
    return result


def evidence_usefulness_policy_with_changes(value: object, changes: object) -> EvidenceUsefulnessPolicy:
    policy = validate_evidence_usefulness_policy(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("evidence usefulness policy changes must be an object")
    if not frozenset(changes).issubset(EVIDENCE_USEFULNESS_POLICY_FIELDS):
        raise InvalidRequestError("evidence usefulness policy changes contain an unknown field")
    updated: dict[str, object] = dict(policy)
    updated.update(changes)
    result = validate_evidence_usefulness_policy(updated)
    return result


def evidence_usefulness_policy_to_dict(value: object) -> dict[str, object]:
    policy = validate_evidence_usefulness_policy(value)
    result: dict[str, object] = dict(policy)
    return result


def evidence_usefulness_policy_from_dict(value: object) -> EvidenceUsefulnessPolicy:
    result = validate_evidence_usefulness_policy(value)
    return result


def evidence_usefulness_policy_to_json(value: object) -> str:
    payload = evidence_usefulness_policy_to_dict(value)
    result = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return result


def evidence_usefulness_policy_from_json(value: str) -> EvidenceUsefulnessPolicy:
    if not isinstance(value, str):
        raise InvalidRequestError("EvidenceUsefulnessPolicy JSON must be a string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise InvalidRequestError("EvidenceUsefulnessPolicy JSON must be valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise InvalidRequestError("EvidenceUsefulnessPolicy JSON must decode to an object")
    result = evidence_usefulness_policy_from_dict(decoded)
    return result


def evaluate_evidence_usefulness(policy: object, record: object) -> EvidenceUsefulnessDecision:
    """Apply one validated content-free evidence policy to a full Claim record."""
    validated_policy = validate_evidence_usefulness_policy(policy)
    try:
        validated_record = validate_claim_evidence_record(record)
    except InvalidRequestError as error:
        raise InvalidRequestError("evidence usefulness requires a ClaimEvidenceRecord") from error
    values = validated_record["features"]["values"]
    reasons: set[EvidenceUsefulnessReason] = set()
    exclusions: set[EvidenceUsefulnessReason] = set()

    if "canonical_completeness" not in values:
        exclusions.add(EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_UNAVAILABLE)
    elif values["canonical_completeness"] < validated_policy["canonical_completeness_floor"]:
        exclusions.add(EvidenceUsefulnessReason.CANONICAL_COMPLETENESS_BELOW_FLOOR)

    qualifying_signal = False
    signal_available = False
    if "structured_match" in values:
        signal_available = True
        if values["structured_match"] >= validated_policy["structured_match_floor"]:
            qualifying_signal = True
            reasons.add(EvidenceUsefulnessReason.STRUCTURED_MATCH_QUALIFIED)
    if "semantic_similarity" in values:
        signal_available = True
        if values["semantic_similarity"] >= validated_policy["semantic_similarity_floor"]:
            qualifying_signal = True
            reasons.add(EvidenceUsefulnessReason.SEMANTIC_SIMILARITY_QUALIFIED)
    if not signal_available:
        exclusions.add(EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_UNAVAILABLE)
    elif not qualifying_signal:
        exclusions.add(EvidenceUsefulnessReason.RETRIEVAL_SIGNAL_BELOW_FLOOR)

    if "source_agreement" in values and values["source_agreement"] >= validated_policy["source_agreement_floor"]:
        reasons.add(EvidenceUsefulnessReason.SOURCE_AGREEMENT_QUALIFIED)

    if not validated_record["trust"]["supplied_trust_available"]:
        reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_UNAVAILABLE)
        if validated_policy["supplied_trust_floor_available"]:
            exclusions.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_REQUIRED_UNAVAILABLE)
    else:
        reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_AVAILABLE)
        if validated_policy["supplied_trust_floor_available"]:
            if validated_record["trust"]["supplied_trust"] < validated_policy["supplied_trust_floor"]:
                exclusions.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_BELOW_FLOOR)
            else:
                reasons.add(EvidenceUsefulnessReason.SUPPLIED_TRUST_FLOOR_SATISFIED)

    reasons.update(exclusions)
    result = evidence_usefulness_decision(
        validated_record["claim_id"],
        not exclusions,
        tuple(sorted(reasons, key=lambda reason: reason.value)),
        validated_policy["policy_version"],
    )
    return result


def _token(value: object, name: str, maximum_bytes: int = 256) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidRequestError(f"{name} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise InvalidRequestError(f"{name} exceeds the limit of {maximum_bytes} UTF-8 bytes")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidRequestError(f"{name} must not contain whitespace or control characters")
    return value


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise InvalidRequestError("Claim eligibility time must be canonical RFC 3339 UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidRequestError("Claim eligibility time must be canonical RFC 3339 UTC") from error
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise InvalidRequestError("Claim eligibility time must use the canonical UTC representation")
    return parsed


VisibilityAuthorization = TypedDict(
    "VisibilityAuthorization",
    {
        "allowed": bool,
        "scope": ScopeKey,
        "ownership": ClaimOwnership,
        "authority_id": str,
        "policy_version": str,
        "reason_code": str,
    },
)


def visibility_authorization(
    allowed: object,
    scope: object,
    ownership: object,
    authority_id: object,
    policy_version: object,
    reason_code: object,
) -> VisibilityAuthorization:
    """Build one trusted exact-scope decision for a non-public ownership category."""
    if not isinstance(allowed, bool):
        raise InvalidRequestError("visibility authorization allowed must be a boolean")
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("visibility authorization scope must be a ScopeKey") from error
    if not isinstance(ownership, ClaimOwnership) or ownership not in {ClaimOwnership.COMPANY, ClaimOwnership.CUSTOMER}:
        raise InvalidRequestError("visibility authorization ownership must be COMPANY or CUSTOMER")
    normalized_authority = _token(authority_id, "visibility authorization authority_id")
    normalized_policy = _token(policy_version, "visibility authorization policy_version")
    normalized_reason = _token(reason_code, "visibility authorization reason_code", 96)
    result: VisibilityAuthorization = {
        "allowed": allowed,
        "scope": validated_scope,
        "ownership": ownership,
        "authority_id": normalized_authority,
        "policy_version": normalized_policy,
        "reason_code": normalized_reason,
    }
    return result


def validate_visibility_authorization(value: object) -> VisibilityAuthorization:
    data = _exact_mapping(value, "VisibilityAuthorization", VISIBILITY_AUTHORIZATION_FIELDS)
    result = visibility_authorization(
        data["allowed"],
        data["scope"],
        data["ownership"],
        data["authority_id"],
        data["policy_version"],
        data["reason_code"],
    )
    return result


VisibilityGrant = TypedDict("VisibilityGrant", {"scope": ScopeKey, "ownership": ClaimOwnership})


def visibility_grant(scope: object, ownership: object) -> VisibilityGrant:
    """Build one exact typed scope/ownership authorization."""
    try:
        validated_scope = validate_scope_key(scope)
    except IdentityValidationError as error:
        raise InvalidRequestError("visibility grant scope must be a ScopeKey") from error
    if not isinstance(ownership, ClaimOwnership) or ownership not in {ClaimOwnership.COMPANY, ClaimOwnership.CUSTOMER}:
        raise InvalidRequestError("visibility grant ownership must be COMPANY or CUSTOMER")
    result: VisibilityGrant = {"scope": validated_scope, "ownership": ownership}
    return result


def validate_visibility_grant(value: object) -> VisibilityGrant:
    data = _exact_mapping(value, "VisibilityGrant", VISIBILITY_GRANT_FIELDS)
    result = visibility_grant(data["scope"], data["ownership"])
    return result


class ExactScopeVisibilityAuthority:
    """Configured allow-list that compares complete ScopeKey values by equality."""

    def __init__(self, authority_id: str, policy_version: str, grants: tuple[VisibilityGrant, ...]) -> None:
        self.authority_id = _token(authority_id, "visibility authority_id")
        self.policy_version = _token(policy_version, "visibility policy_version")
        if not isinstance(grants, tuple):
            raise InvalidRequestError("visibility grants must be a tuple of VisibilityGrant values")
        if len(grants) > MAX_VISIBILITY_GRANTS:
            raise InvalidRequestError(f"visibility grants exceeds the limit of {MAX_VISIBILITY_GRANTS}")
        try:
            validated_grants = tuple(validate_visibility_grant(grant) for grant in grants)
        except InvalidRequestError as error:
            raise InvalidRequestError("visibility grants must be a tuple of VisibilityGrant values") from error
        keys = tuple((scope_key_signature(grant["scope"]), grant["ownership"]) for grant in validated_grants)
        if len(keys) != len(set(keys)):
            raise InvalidRequestError("visibility grants must be unique")
        self._grants = frozenset(keys)

    def evaluate(self, scope: ScopeKey, ownership: ClaimOwnership) -> VisibilityAuthorization:
        try:
            scope = validate_scope_key(scope)
        except IdentityValidationError as error:
            raise InvalidRequestError("visibility evaluation scope must be a ScopeKey") from error
        if ownership not in {ClaimOwnership.COMPANY, ClaimOwnership.CUSTOMER}:
            raise InvalidRequestError("visibility evaluation ownership must be COMPANY or CUSTOMER")
        allowed = (scope_key_signature(scope), ownership) in self._grants
        reason_code = "exact_scope_granted" if allowed else "exact_scope_denied"
        result = visibility_authorization(
            allowed,
            scope,
            ownership,
            self.authority_id,
            self.policy_version,
            reason_code,
        )
        return result


ClaimEligibilityDecision = TypedDict(
    "ClaimEligibilityDecision",
    {
        "projection": ClaimProjection,
        "eligible": bool,
        "reason": ClaimEligibilityReason,
        "disclosure": DisclosureDecision,
        "disclosure_available": bool,
        "revalidated": bool,
    },
)


def claim_eligibility_decision(
    projection: object,
    eligible: object,
    reason: object,
    disclosure: object = (),
    disclosure_available: object = False,
    revalidated: object = False,
) -> ClaimEligibilityDecision:
    """Build one fail-closed Claim projection eligibility decision."""
    validated_projection = validate_claim_projection(projection)
    if not isinstance(eligible, bool):
        raise InvalidRequestError("Claim eligibility eligible must be a boolean")
    if not isinstance(reason, ClaimEligibilityReason):
        raise InvalidRequestError("Claim eligibility reason must be a ClaimEligibilityReason")
    selected_disclosure = empty_disclosure_decision() if type(disclosure) is tuple and not disclosure else disclosure
    try:
        validated_disclosure = validate_disclosure_decision(selected_disclosure)
    except InvalidRequestError as error:
        raise InvalidRequestError("Claim eligibility disclosure must be a DisclosureDecision") from error
    if not isinstance(disclosure_available, bool):
        raise InvalidRequestError("Claim eligibility disclosure_available must be a boolean")
    if not isinstance(revalidated, bool):
        raise InvalidRequestError("Claim eligibility revalidated must be a boolean")
    if eligible != disclosure_available:
        raise InvalidRequestError("eligible Claim decisions require an available disclosure decision")
    if not disclosure_available and validated_disclosure != empty_disclosure_decision():
        raise InvalidRequestError("unavailable Claim disclosure must use the concrete empty decision")
    eligible_reasons = {
        ClaimEligibilityReason.ELIGIBLE_PUBLIC,
        ClaimEligibilityReason.ELIGIBLE_TRUSTED_SCOPE,
    }
    if eligible != (reason in eligible_reasons):
        raise InvalidRequestError("Claim eligibility reason conflicts with eligible state")
    result: ClaimEligibilityDecision = {
        "projection": validated_projection,
        "eligible": eligible,
        "reason": reason,
        "disclosure": validated_disclosure,
        "disclosure_available": disclosure_available,
        "revalidated": revalidated,
    }
    return result


def validate_claim_eligibility_decision(value: object) -> ClaimEligibilityDecision:
    data = _exact_mapping(value, "ClaimEligibilityDecision", CLAIM_ELIGIBILITY_DECISION_FIELDS)
    result = claim_eligibility_decision(
        data["projection"],
        data["eligible"],
        data["reason"],
        data["disclosure"],
        data["disclosure_available"],
        data["revalidated"],
    )
    return result


def claim_eligibility_decision_with_changes(value: object, changes: object) -> ClaimEligibilityDecision:
    decision = validate_claim_eligibility_decision(value)
    if not isinstance(changes, Mapping):
        raise InvalidRequestError("Claim eligibility decision changes must be an object")
    if not frozenset(changes).issubset(CLAIM_ELIGIBILITY_DECISION_FIELDS):
        raise InvalidRequestError("Claim eligibility decision changes contain an unknown field")
    updated: dict[str, object] = dict(decision)
    updated.update(changes)
    result = validate_claim_eligibility_decision(updated)
    return result


def claim_exclusion_decision(
    projection: ClaimProjection,
    reason: ClaimEligibilityReason,
    *,
    revalidated: bool = False,
) -> ClaimEligibilityDecision:
    """Construct one ineligible Claim decision."""
    result = claim_eligibility_decision(projection, False, reason, revalidated=revalidated)
    return result


def claim_validity_inputs_from_eligibility(
    decision: ClaimEligibilityDecision,
    frame: QueryFrame,
) -> ClaimValidityInputs:
    """Derive validity inputs from an eligible publication-time decision."""
    decision = validate_claim_eligibility_decision(decision)
    if not decision["eligible"] or not decision["revalidated"] or not decision["disclosure_available"]:
        raise InvalidRequestError("Claim validity inputs require an eligible revalidated decision")
    projection = decision["projection"]
    temporal = frame["temporal_query"]
    evaluation_time = _timestamp(frame["eligibility_context"]["evaluation_time"])
    effective_system_to = projection["system_to"]
    effective_system_to_available = projection["system_to_available"]
    if projection["invalidated_at_available"] and (
        not effective_system_to_available or _timestamp(projection["invalidated_at"]) < _timestamp(effective_system_to)
    ):
        effective_system_to = projection["invalidated_at"]
        effective_system_to_available = True
    result = claim_validity_inputs(
        frame["eligibility_context"]["evaluation_time"],
        not projection["invalidated_at_available"],
        _interval_contains(
            evaluation_time,
            projection["system_from"],
            projection["system_from_available"],
            effective_system_to,
            effective_system_to_available,
        ),
        _interval_contains(
            evaluation_time,
            projection["valid_from"],
            projection["valid_from_available"],
            projection["valid_to"],
            projection["valid_to_available"],
        ),
        valid_from=projection["valid_from"],
        valid_from_available=projection["valid_from_available"],
        valid_to=projection["valid_to"],
        valid_to_available=projection["valid_to_available"],
        temporal_operator=temporal["operator"],
        temporal_axis=temporal["axis"],
        requested_start=temporal["start"],
        requested_start_available=temporal["start_available"],
        requested_end=temporal["end"],
        requested_end_available=temporal["end_available"],
        system_from=projection["system_from"],
        system_from_available=projection["system_from_available"],
        system_to=projection["system_to"],
        system_to_available=projection["system_to_available"],
        invalidated_at=projection["invalidated_at"],
        invalidated_at_available=projection["invalidated_at_available"],
        eligible_for_request=True,
        system_time_match=True,
        valid_time_match=temporal["axis"] == TemporalAxis.VALID_TIME,
        valid_time_match_available=temporal["axis"] == TemporalAxis.VALID_TIME,
    )
    return result


def _interval_contains(
    point: datetime,
    lower: str,
    lower_available: bool,
    upper: str,
    upper_available: bool,
) -> bool:
    """Return half-open point containment for an interval with concrete open bounds."""
    after_lower = not lower_available or point >= _timestamp(lower)
    before_upper = not upper_available or point < _timestamp(upper)
    result = after_lower and before_upper
    return result


def _interval_overlaps(
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
    starts_before_request_end = not requested_end_available or not lower_available or _timestamp(lower) < _timestamp(requested_end)
    ends_after_request_start = (
        not requested_start_available or not upper_available or _timestamp(upper) > _timestamp(requested_start)
    )
    result = starts_before_request_end and ends_after_request_start
    return result


def _requested_interval_match(
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
        result = _interval_contains(_timestamp(requested_start), lower, lower_available, upper, upper_available)
        return result
    result = _interval_overlaps(
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


def _outside_interval_reason(
    requested_start: str,
    requested_start_available: bool,
    requested_end: str,
    requested_end_available: bool,
    lower: str,
    lower_available: bool,
    not_yet_reason: ClaimEligibilityReason,
    no_longer_reason: ClaimEligibilityReason,
) -> ClaimEligibilityReason:
    if requested_end_available and lower_available and _timestamp(lower) >= _timestamp(requested_end):
        result = not_yet_reason
        return result
    if (
        requested_start_available
        and not requested_end_available
        and lower_available
        and _timestamp(lower) > _timestamp(requested_start)
    ):
        result = not_yet_reason
        return result
    result = no_longer_reason
    return result


class ClaimEligibilityEvaluator:
    """Shared temporal and disclosure policy over strict Claim projections."""

    def __init__(self, visibility_authority: object = ()) -> None:
        if type(visibility_authority) is tuple and not visibility_authority:
            selected_authority = ()
        elif callable(getattr(visibility_authority, "evaluate", ())):
            selected_authority = visibility_authority
        else:
            raise InvalidRequestError("visibility authority must implement evaluate")
        self._visibility_authority = selected_authority

    def evaluate(self, projection: ClaimProjection, frame: QueryFrame) -> ClaimEligibilityDecision:
        projection = validate_claim_projection(projection)
        frame = validate_query_frame(frame)
        context = frame["eligibility_context"]
        if not context["evaluation_time_available"]:
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.EVALUATION_TIME_UNAVAILABLE)
            return result
        evaluation_time = _timestamp(context["evaluation_time"])
        temporal = frame["temporal_query"]
        if not temporal["resolved"]:
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.TEMPORAL_QUERY_UNRESOLVED)
            return result
        if not projection["system_from_available"]:
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.SYSTEM_TIME_UNAVAILABLE)
            return result
        current_operator = temporal["operator"] in {
            TemporalQueryOperator.UNSPECIFIED,
            TemporalQueryOperator.CURRENT,
            TemporalQueryOperator.NOW,
        }
        if current_operator:
            if projection["invalidated_at_available"]:
                result = claim_exclusion_decision(projection, ClaimEligibilityReason.CLAIM_INACTIVE)
                return result
            if evaluation_time < _timestamp(projection["system_from"]):
                result = claim_exclusion_decision(projection, ClaimEligibilityReason.SYSTEM_NOT_YET_CURRENT)
                return result
            if projection["system_to_available"] and evaluation_time >= _timestamp(projection["system_to"]):
                result = claim_exclusion_decision(projection, ClaimEligibilityReason.SYSTEM_NO_LONGER_CURRENT)
                return result
            if projection["valid_from_available"] and evaluation_time < _timestamp(projection["valid_from"]):
                result = claim_exclusion_decision(projection, ClaimEligibilityReason.VALID_TIME_NOT_YET_CURRENT)
                return result
            if projection["valid_to_available"] and evaluation_time >= _timestamp(projection["valid_to"]):
                result = claim_exclusion_decision(projection, ClaimEligibilityReason.VALID_TIME_NO_LONGER_CURRENT)
                return result
        elif temporal["axis"] == TemporalAxis.VALID_TIME:
            if projection["invalidated_at_available"]:
                result = claim_exclusion_decision(projection, ClaimEligibilityReason.CLAIM_INACTIVE)
                return result
            if evaluation_time < _timestamp(projection["system_from"]):
                result = claim_exclusion_decision(projection, ClaimEligibilityReason.SYSTEM_NOT_YET_CURRENT)
                return result
            if projection["system_to_available"] and evaluation_time >= _timestamp(projection["system_to"]):
                result = claim_exclusion_decision(projection, ClaimEligibilityReason.SYSTEM_NO_LONGER_CURRENT)
                return result
            if (
                temporal["operator"] == TemporalQueryOperator.LATEST
                and projection["valid_from_available"]
                and evaluation_time < _timestamp(projection["valid_from"])
            ):
                result = claim_exclusion_decision(projection, ClaimEligibilityReason.VALID_TIME_NOT_YET_CURRENT)
                return result
            if temporal["operator"] != TemporalQueryOperator.LATEST and not _requested_interval_match(
                temporal["operator"],
                temporal["start"],
                temporal["start_available"],
                temporal["end"],
                temporal["end_available"],
                projection["valid_from"],
                projection["valid_from_available"],
                projection["valid_to"],
                projection["valid_to_available"],
            ):
                reason = _outside_interval_reason(
                    temporal["start"],
                    temporal["start_available"],
                    temporal["end"],
                    temporal["end_available"],
                    projection["valid_from"],
                    projection["valid_from_available"],
                    ClaimEligibilityReason.VALID_TIME_NOT_YET_CURRENT,
                    ClaimEligibilityReason.VALID_TIME_NO_LONGER_CURRENT,
                )
                result = claim_exclusion_decision(projection, reason)
                return result
        else:
            effective_system_to = projection["system_to"]
            effective_system_to_available = projection["system_to_available"]
            if projection["invalidated_at_available"] and (
                not effective_system_to_available or _timestamp(projection["invalidated_at"]) < _timestamp(effective_system_to)
            ):
                effective_system_to = projection["invalidated_at"]
                effective_system_to_available = True
            if temporal["operator"] == TemporalQueryOperator.LATEST:
                if _timestamp(projection["system_from"]) > evaluation_time:
                    result = claim_exclusion_decision(projection, ClaimEligibilityReason.SYSTEM_NOT_YET_CURRENT)
                    return result
            elif not _requested_interval_match(
                temporal["operator"],
                temporal["start"],
                temporal["start_available"],
                temporal["end"],
                temporal["end_available"],
                projection["system_from"],
                projection["system_from_available"],
                effective_system_to,
                effective_system_to_available,
            ):
                reason = _outside_interval_reason(
                    temporal["start"],
                    temporal["start_available"],
                    temporal["end"],
                    temporal["end_available"],
                    projection["system_from"],
                    projection["system_from_available"],
                    ClaimEligibilityReason.SYSTEM_NOT_YET_CURRENT,
                    ClaimEligibilityReason.SYSTEM_NO_LONGER_CURRENT,
                )
                result = claim_exclusion_decision(projection, reason)
                return result
        if not projection["predicate_canonical"] or projection["predicate_id"] == "generic_relation":
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.RETRIEVAL_ONLY)
            return result

        ownership = ClaimOwnership(projection["ownership_category"])
        if ownership == ClaimOwnership.PUBLIC:
            disclosure = disclosure_decision(
                ownership,
                DisclosureBasis.PUBLIC_RULE,
                frame["scope"],
                CLAIM_DISCLOSURE_POLICY_VERSION,
            )
            result = claim_eligibility_decision(
                projection,
                True,
                ClaimEligibilityReason.ELIGIBLE_PUBLIC,
                disclosure,
                True,
            )
            return result

        authority_method = getattr(self._visibility_authority, "evaluate", ())
        if not callable(authority_method):
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.VISIBILITY_AUTHORITY_UNAVAILABLE)
            return result
        try:
            authorization = authority_method(frame["scope"], ownership)
        except Exception:
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.VISIBILITY_AUTHORITY_FAILED)
            return result
        try:
            authorization = validate_visibility_authorization(authorization)
        except InvalidRequestError:
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.VISIBILITY_AUTHORITY_FAILED)
            return result
        if authorization["scope"] != frame["scope"]:
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.VISIBILITY_SCOPE_MISMATCH)
            return result
        if authorization["ownership"] != ownership:
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.VISIBILITY_OWNERSHIP_MISMATCH)
            return result
        if not authorization["allowed"]:
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.VISIBILITY_DENIED)
            return result
        disclosure = disclosure_decision(
            ownership,
            DisclosureBasis.TRUSTED_SCOPE_AUTHORITY,
            frame["scope"],
            authorization["policy_version"],
            authorization["authority_id"],
            True,
        )
        result = claim_eligibility_decision(
            projection,
            True,
            ClaimEligibilityReason.ELIGIBLE_TRUSTED_SCOPE,
            disclosure,
            True,
        )
        return result

    def revalidate(
        self,
        discovered: ClaimProjection,
        frame: QueryFrame,
        current_claim_projection: Callable[[str], tuple[ClaimProjection, ...]],
    ) -> ClaimEligibilityDecision:
        if not callable(current_claim_projection):
            result = claim_exclusion_decision(discovered, ClaimEligibilityReason.REVALIDATION_UNAVAILABLE)
            return result
        discovered = validate_claim_projection(discovered)
        try:
            current = current_claim_projection(discovered["claim_id"])
        except Exception:
            result = claim_exclusion_decision(discovered, ClaimEligibilityReason.REVALIDATION_UNAVAILABLE)
            return result
        if not isinstance(current, tuple) or len(current) != 1:
            result = claim_exclusion_decision(discovered, ClaimEligibilityReason.REVALIDATION_MISSING)
            return result
        try:
            projection = validate_claim_projection(current[0])
        except InvalidRequestError:
            result = claim_exclusion_decision(discovered, ClaimEligibilityReason.REVALIDATION_MISSING)
            return result
        if projection["projection_id"] != ClaimProjectionQuery.BY_ID_V1:
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.REVALIDATION_IDENTITY_CONFLICT, revalidated=True)
            return result
        discovered_identity = (
            discovered["claim_id"],
            discovered["subject_entity_id"],
            discovered["predicate_id"],
            discovered["object_entity_id"],
        )
        current_identity = (
            projection["claim_id"],
            projection["subject_entity_id"],
            projection["predicate_id"],
            projection["object_entity_id"],
        )
        if discovered_identity != current_identity:
            result = claim_exclusion_decision(projection, ClaimEligibilityReason.REVALIDATION_IDENTITY_CONFLICT, revalidated=True)
            return result
        decision = self.evaluate(projection, frame)
        result = claim_eligibility_decision_with_changes(decision, {"revalidated": True})
        return result


def revalidate_claims(
    projections: tuple[ClaimProjection, ...],
    frame: QueryFrame,
    evaluator: ClaimEligibilityEvaluator,
    current_claim_projection: Callable[[str], tuple[ClaimProjection, ...]],
    cooperative_check: Callable[[], object] = _no_cooperative_check,
) -> tuple[ClaimEligibilityDecision, ...]:
    """Revalidate a bounded projection batch immediately before package construction."""
    if not isinstance(projections, tuple) or len(projections) > 1_000:
        raise InvalidRequestError("Claim revalidation projections must be a tuple of at most 1000 values")
    validated_projections = tuple(validate_claim_projection(projection) for projection in projections)
    if not isinstance(evaluator, ClaimEligibilityEvaluator):
        raise InvalidRequestError("Claim revalidation evaluator must be ClaimEligibilityEvaluator")
    if not callable(cooperative_check):
        raise InvalidRequestError("Claim revalidation cooperative_check must be callable")
    decisions = []
    for projection in validated_projections:
        cooperative_check()
        decisions.append(evaluator.revalidate(projection, frame, current_claim_projection))
    cooperative_check()
    result = tuple(decisions)
    return result


def claim_evidence_record(
    discovered: ClaimProjection,
    decision: ClaimEligibilityDecision,
    frame: QueryFrame,
    source_resolver: str,
) -> ClaimEvidenceRecord:
    """Construct one strict full record only from eligible revalidated state."""
    discovered = validate_claim_projection(discovered)
    decision = validate_claim_eligibility_decision(decision)
    source = _token(source_resolver, "Claim evidence source_resolver", 96)
    if source not in CLAIM_EVIDENCE_PRODUCERS:
        raise InvalidRequestError("Claim evidence source_resolver is not an allowed producer")
    if source == "structured_graph" and discovered["projection_id"] not in {
        ClaimProjectionQuery.STRUCTURED_ENTITY_V1,
        ClaimProjectionQuery.STRUCTURED_KEYWORD_V1,
        ClaimProjectionQuery.RELATION_ONE_HOP_V1,
    }:
        raise InvalidRequestError("structured Claim evidence requires a structured discovery projection")
    if source == "support_semantic" and discovered["projection_id"] != ClaimProjectionQuery.VECTOR_V1:
        raise InvalidRequestError("semantic Claim evidence requires a vector discovery projection")
    if not decision["eligible"] or not decision["revalidated"] or not decision["disclosure_available"]:
        raise InvalidRequestError("Claim evidence construction requires an eligible revalidated decision")
    current = decision["projection"]
    discovered_identity = (
        discovered["claim_id"],
        discovered["subject_entity_id"],
        discovered["predicate_id"],
        discovered["object_entity_id"],
    )
    current_identity = (
        current["claim_id"],
        current["subject_entity_id"],
        current["predicate_id"],
        current["object_entity_id"],
    )
    if discovered_identity != current_identity:
        raise InvalidRequestError("Claim evidence discovery and current canonical identity conflict")
    values = {"canonical_completeness": 1.0}
    unavailable = ["source_agreement"]
    reasons = [decision["reason"].value, "canonical_complete"]
    if discovered["structured_match_available"]:
        values["structured_match"] = discovered["structured_match"]
        reasons.append("structured_match")
    else:
        unavailable.append("structured_match")
    if discovered["semantic_similarity_available"]:
        values["semantic_similarity"] = discovered["semantic_similarity"]
        reasons.append("semantic_similarity")
    else:
        unavailable.append("semantic_similarity")
    if current["supplied_trust_available"]:
        values["supplied_trust"] = current["supplied_trust"]
        reasons.append("supplied_trust_available")
    else:
        unavailable.append("supplied_trust")
        reasons.append("supplied_trust_unavailable")
    result = build_claim_evidence_record(
        claim_id=current["claim_id"],
        source_resolver=source,
        source_contributions=(source,),
        features=feature_set(values=values, unavailable=tuple(sorted(unavailable))),
        canonical_references=canonical_claim_references(
            current["subject_entity_id"],
            current["predicate_id"],
            current["object_entity_id"],
        ),
        validity=claim_validity_inputs_from_eligibility(decision, frame),
        trust=claim_trust_inputs(
            current["trust_category"],
            current["trust_category_available"],
            current["supplied_trust"],
            current["supplied_trust_available"],
            current["supplied_trust_version"],
            current["supplied_trust_version_available"],
        ),
        disclosure=decision["disclosure"],
        path=(current["claim_id"],),
        selection_reasons=tuple(sorted(reasons)),
    )
    return result


def _merge_claim_evidence_group(records: tuple[ClaimEvidenceRecord, ...]) -> ClaimEvidenceRecord:
    if not records:
        raise InvalidRequestError("cannot merge an empty Claim evidence group")
    ordered = tuple(sorted(records, key=lambda record: (record["source_resolver"], claim_evidence_record_to_json(record))))
    base = ordered[0]
    for record in ordered[1:]:
        if record["canonical_references"] != base["canonical_references"]:
            raise InvalidRequestError(f"conflicting canonical references for Claim evidence ID: {base['claim_id']}")
        current_state = (record["validity"], record["trust"], record["disclosure"], record["path"])
        base_state = (base["validity"], base["trust"], base["disclosure"], base["path"])
        if current_state != base_state:
            raise InvalidRequestError(f"conflicting current evidence state for Claim evidence ID: {base['claim_id']}")

    sources = tuple(sorted({source for record in ordered for source in record["source_contributions"]}))
    if not sources:
        raise InvalidRequestError("merged Claim evidence sources must not be empty")
    if len(sources) > MAX_CLAIM_SOURCE_CONTRIBUTIONS:
        raise InvalidRequestError(f"merged Claim evidence sources exceed the limit of {MAX_CLAIM_SOURCE_CONTRIBUTIONS}")
    primary_source = next(iter(sources))
    values: dict[str, float] = {}
    unavailable = set()
    reasons = set()
    for record in ordered:
        reasons.update(record["selection_reasons"])
        unavailable.update(record["features"]["unavailable"])
        for name, value in record["features"]["values"].items():
            if name in values and values[name] != value:
                raise InvalidRequestError(f"conflicting measured feature {name} for Claim evidence ID: {base['claim_id']}")
            values[name] = value
    if len(sources) > 1:
        if "source_agreement" in values and values["source_agreement"] != 1.0:
            raise InvalidRequestError(f"conflicting measured feature source_agreement for Claim evidence ID: {base['claim_id']}")
        values["source_agreement"] = 1.0
        reasons.add("source_agreement")
    elif "source_agreement" in values:
        raise InvalidRequestError(f"source_agreement requires multiple sources for Claim evidence ID: {base['claim_id']}")
    unavailable.difference_update(values)
    if len(reasons) > MAX_CLAIM_SELECTION_REASONS:
        raise InvalidRequestError(f"merged Claim evidence reasons exceed the limit of {MAX_CLAIM_SELECTION_REASONS}")
    result = claim_evidence_record_with_changes(
        base,
        {
            "source_resolver": primary_source,
            "source_contributions": sources,
            "features": feature_set(values=values, unavailable=tuple(sorted(unavailable))),
            "selection_reasons": tuple(sorted(reasons)),
        },
    )
    return result


def canonicalize_claim_evidence(
    records: tuple[ClaimEvidenceRecord, ...],
    cooperative_check: Callable[[], object] = _no_cooperative_check,
) -> tuple[ClaimEvidenceRecord, ...]:
    """Deterministically deduplicate and merge strict records by stable Claim ID."""
    if not isinstance(records, tuple) or len(records) > MAX_RESOLUTION_VALUES:
        raise InvalidRequestError(f"Claim evidence normalization requires a tuple of at most {MAX_RESOLUTION_VALUES} records")
    try:
        validated_records = tuple(validate_claim_evidence_record(record) for record in records)
    except InvalidRequestError as error:
        raise InvalidRequestError("Claim evidence normalization requires ClaimEvidenceRecord values") from error
    if not callable(cooperative_check):
        raise InvalidRequestError("Claim evidence normalization cooperative_check must be callable")
    grouped: dict[str, list[ClaimEvidenceRecord]] = {}
    for record in validated_records:
        cooperative_check()
        grouped.setdefault(record["claim_id"], []).append(record)
    merged = []
    for claim_id in sorted(grouped):
        cooperative_check()
        merged.append(_merge_claim_evidence_group(tuple(grouped[claim_id])))
    cooperative_check()
    result = tuple(merged)
    return result
